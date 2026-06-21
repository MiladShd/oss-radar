"""The hard validation gate: a retrained growth model is promoted/served only if it clears the
leak-free / beats-baseline / generalises bar, and a failed candidate auto-rolls-back to the
last-good champion. These tests pin that behaviour so the daily loop can't silently ship a leak.
"""

import numpy as np
import pandas as pd

from oss_radar.config import get_settings
from oss_radar.features import DOWNLOAD_FEATURES, GROWTH_TARGET_COLUMN
from oss_radar.models.validation_gate import growth_gate
from oss_radar.registry import ModelRegistry
from oss_radar.warehouse.duckdb_backend import DuckDBWarehouse


def _ds(seed: int, target_fn, n: int = 500, n_pkgs: int = 20) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({f: rng.normal(0, 1, n) for f in DOWNLOAD_FEATURES})
    df["name"] = [f"pkg{i % n_pkgs}" for i in range(n)]
    df["feature_date"] = pd.date_range("2026-01-01", periods=n).date
    df[GROWTH_TARGET_COLUMN] = target_fn(df, rng)
    return df


def _failed(gate):
    return {c["name"] for c in gate.checks if not c["passed"]}


def test_gate_passes_clean_generalisable_signal():
    # a genuine, moderate-strength signal shared across packages: real skill, not too good.
    df = _ds(1, lambda d, r: 1.0 + 0.5 * d["mom_7v7"] + r.normal(0, 0.4, len(d)))
    gate = growth_gate(df, DOWNLOAD_FEATURES, get_settings())
    assert gate.passed is True and gate.skipped is False
    assert 0.0 < gate.metrics["same_split_r2"] < 0.90
    assert gate.metrics["oof_spearman"] >= 0.05


def test_gate_fails_pure_noise():
    # no relationship between features and target -> no skill -> blocked.
    gate = growth_gate(_ds(2, lambda d, r: r.normal(0, 0.5, len(d))), DOWNLOAD_FEATURES, get_settings())
    assert gate.passed is False
    assert "has_skill_spearman" in _failed(gate) or "has_skill_r2" in _failed(gate)


def test_gate_fails_leak_ceiling():
    # an implausibly high R^2 on this noisy target is the fingerprint of a re-introduced leak.
    df = _ds(3, lambda d, r: 1.0 + 0.5 * d["mom_7v7"] + r.normal(0, 0.01, len(d)))
    gate = growth_gate(df, DOWNLOAD_FEATURES, get_settings())
    assert gate.passed is False
    assert "not_leaky_ceiling" in _failed(gate)
    assert gate.metrics["same_split_r2"] > 0.90


def test_gate_skips_on_insufficient_data():
    gate = growth_gate(_ds(4, lambda d, r: r.normal(0, 1, len(d)), n=50), DOWNLOAD_FEATURES, get_settings())
    assert gate.passed is True and gate.skipped is True


# --- promotion gate + auto-rollback plumbing (no LightGBM needed) -------------------------------

class _Stub:
    """Minimal model artifact: save() writes a tag, load() reads it back."""
    model = "fitted"

    def __init__(self, tag: str = "champ"):
        self.tag = tag

    def save(self, path: str) -> None:
        from pathlib import Path
        Path(path).write_text(self.tag)

    @classmethod
    def load(cls, path: str):
        from pathlib import Path
        return cls(tag=Path(path).read_text())


def _wh(tmp_path):
    wh = DuckDBWarehouse(path=str(tmp_path / "t.duckdb"))
    wh.init_schema()
    return wh


def test_failed_gate_blocks_promotion(tmp_path):
    reg = ModelRegistry(get_settings())
    # a great primary metric must NOT win promotion when the validation gate failed.
    champ, rows = reg.persist(_wh(tmp_path), "r1", "growth", _Stub(),
                              {"spearman": 0.9, "n_train": 100, "n_test": 20}, {}, gate_passed=False)
    assert champ is False
    assert any("BLOCKED by validation gate" in r["notes"] for r in rows)


def test_passed_gate_promotes_first_champion(tmp_path):
    reg = ModelRegistry(get_settings())
    champ, _ = reg.persist(_wh(tmp_path), "r2", "growth", _Stub(),
                           {"spearman": 0.5, "n_train": 100, "n_test": 20}, {}, gate_passed=True)
    assert champ is True


def test_auto_rollback_loads_last_good_champion(tmp_path):
    wh = _wh(tmp_path)
    reg = ModelRegistry(get_settings())
    champ, rows = reg.persist(wh, "r1", "growth", _Stub("good-v1"),
                              {"spearman": 0.5, "n_train": 100, "n_test": 20}, {}, gate_passed=True)
    assert champ is True
    wh.insert_rows("model_runs", rows)
    model, version = reg.load_champion(wh, "growth", _Stub)
    assert model is not None and model.tag == "good-v1" and version == "growth-r1"

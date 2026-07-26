"""The hard validation gate: a retrained growth model is promoted/served only if it clears the
leak-free / beats-baseline / generalises bar, and a failed candidate auto-rolls-back to the
last-good champion. These tests pin that behaviour so the daily loop can't silently ship a leak.
"""

import numpy as np
import pandas as pd
import pytest

from oss_radar.config import get_settings
from oss_radar.features import DOWNLOAD_FEATURES, GROWTH_TARGET_COLUMN
from oss_radar.models.validation_gate import growth_gate
from oss_radar.registry import ModelRegistry
from oss_radar.warehouse.duckdb_backend import DuckDBWarehouse


@pytest.fixture(autouse=True)
def _isolate_registry_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


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


def test_gate_fails_when_expected_metrics_are_nan():
    gate = growth_gate(_ds(5, lambda d, r: np.zeros(len(d))), DOWNLOAD_FEATURES, get_settings())
    assert gate.passed is False and gate.skipped is False
    assert "has_date_grouped_evidence" in _failed(gate)
    assert "has_group_evidence" in _failed(gate)


def test_gate_rejects_unreviewed_forward_looking_feature():
    df = _ds(6, lambda d, r: 0.5 * d["mom_7v7"] + r.normal(0, 0.3, len(d)))
    df["future_downloads"] = df[GROWTH_TARGET_COLUMN]
    gate = growth_gate(df, [*DOWNLOAD_FEATURES, "future_downloads"], get_settings())
    assert gate.passed is False
    assert "causal_feature_contract" in _failed(gate)


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


def test_held_challenger_serves_existing_champion(tmp_path):
    wh = _wh(tmp_path)
    reg = ModelRegistry(get_settings())
    _champ, rows = reg.persist(wh, "r1", "growth", _Stub("good-v1"),
                               {"spearman": 0.5, "n_train": 100}, {}, gate_passed=True)
    wh.insert_rows("model_runs", rows)
    held = _Stub("worse-v2")
    model, version = reg.select_for_serving(wh, "growth", held, False, _Stub)
    assert model.tag == "good-v1" and version == "growth-r1"


def test_newly_promoted_candidate_is_served_before_rows_are_written(tmp_path):
    reg = ModelRegistry(get_settings())
    candidate = _Stub("new-v1")
    candidate.version = "growth-r1"
    model, version = reg.select_for_serving(_wh(tmp_path), "growth", candidate, True, _Stub)
    assert model is candidate and version == "growth-r1"


def test_registry_persists_serving_and_evaluation_provenance(tmp_path):
    model = _Stub("with-provenance")
    model.eval_provenance = {
        "schema_version": "cohort-eval-v1",
        "label_version": "growth-v1",
        "feature_set_hash": "features",
        "benchmark_kind": "closed",
        "split_version": "split-v1",
        "benchmark_hash": "abc123",
    }
    champion, rows = ModelRegistry(get_settings()).persist(
        _wh(tmp_path), "r3", "growth", model,
        {"spearman": 0.5, "n_train": 100, "n_test": 20}, {}, gate_passed=True,
    )
    assert champion is True
    assert {row["served_version"] for row in rows} == {"growth-r3"}
    assert all(row["eval_provenance"]["benchmark_hash"] == "abc123" for row in rows)


def _provenance(benchmark_hash):
    return {
        "schema_version": "cohort-eval-v1",
        "label_version": "growth-v1",
        "feature_set_hash": "features",
        "benchmark_kind": "closed",
        "split_version": "split-v1",
        "benchmark_hash": benchmark_hash,
    }


def test_registry_never_promotes_across_unmatched_cohorts(tmp_path):
    warehouse = _wh(tmp_path)
    registry = ModelRegistry(get_settings())
    first = _Stub("first")
    first.eval_provenance = _provenance("cohort-a")
    champion, rows = registry.persist(
        warehouse, "r1", "growth", first,
        {"spearman": 0.4, "n_train": 100, "n_test": 20}, {}, gate_passed=True,
    )
    assert champion is True
    warehouse.insert_rows("model_runs", rows)

    candidate = _Stub("candidate")
    candidate.eval_provenance = _provenance("cohort-b")
    promoted, candidate_rows = registry.persist(
        warehouse, "r2", "growth", candidate,
        {"spearman": 0.9, "n_train": 100, "n_test": 20}, {}, gate_passed=True,
    )
    assert promoted is False
    primary = next(row for row in candidate_rows if row["metric_name"] == "spearman")
    assert primary["comparison_mode"] == "not-comparable"
    assert "no matched incumbent evaluation" in primary["notes"]


def test_registry_can_compare_rescored_incumbent_on_current_cohort(tmp_path):
    warehouse = _wh(tmp_path)
    registry = ModelRegistry(get_settings())
    first = _Stub("first")
    first.eval_provenance = _provenance("cohort-a")
    _, rows = registry.persist(
        warehouse, "r1", "growth", first,
        {"spearman": 0.4, "n_train": 100, "n_test": 20}, {}, gate_passed=True,
    )
    warehouse.insert_rows("model_runs", rows)

    candidate = _Stub("candidate")
    candidate.eval_provenance = _provenance("cohort-b")
    promoted, candidate_rows = registry.persist(
        warehouse, "r2", "growth", candidate,
        {"spearman": 0.6, "n_train": 100, "n_test": 20}, {}, gate_passed=True,
        incumbent_metric=0.5,
        incumbent_version="growth-r1",
        comparison_provenance=_provenance("cohort-b"),
    )
    assert promoted is True
    primary = next(row for row in candidate_rows if row["metric_name"] == "spearman")
    assert primary["comparison_mode"] == "incumbent-rescored-current-benchmark"
    assert primary["comparison_metric_value"] == 0.5
    assert primary["metric_value"] == 0.6


def test_registry_rejects_rescore_from_a_different_evaluation_lineage(tmp_path):
    warehouse = _wh(tmp_path)
    candidate = _Stub("candidate")
    candidate.eval_provenance = _provenance("same-cohort")
    incompatible = {
        **_provenance("same-cohort"),
        "label_version": "growth-log14d-trailing28-v1",
    }

    promoted, rows = ModelRegistry(get_settings()).persist(
        warehouse,
        "r2",
        "growth",
        candidate,
        {"spearman": 0.6, "n_train": 100, "n_test": 20},
        {},
        gate_passed=True,
        incumbent_metric=0.5,
        incumbent_version="growth-old",
        comparison_provenance=incompatible,
        compatible_incumbent_available=True,
    )

    assert promoted is False
    primary = next(row for row in rows if row["metric_name"] == "spearman")
    assert primary["comparison_mode"] == "not-comparable"


def test_missing_compatible_artifact_can_rebootstrap_lineage(tmp_path):
    warehouse = _wh(tmp_path)
    provenance = _provenance("cohort-a")
    warehouse.insert_rows("model_runs", [{
        "run_id": "old",
        "model_name": "growth",
        "trained_at": pd.Timestamp("2026-01-01"),
        "version": "growth-old",
        "metric_name": "spearman",
        "metric_value": 0.9,
        "is_champion": True,
        "gcs_uri": "/missing/artifact.pkl",
        "eval_provenance": provenance,
    }])
    candidate = _Stub("replacement")
    candidate.eval_provenance = provenance

    promoted, rows = ModelRegistry(get_settings()).persist(
        warehouse,
        "new",
        "growth",
        candidate,
        {"spearman": 0.5, "n_train": 100, "n_test": 20},
        {},
        gate_passed=True,
        compatible_incumbent_available=False,
    )

    assert promoted is True
    primary = next(row for row in rows if row["metric_name"] == "spearman")
    assert primary["comparison_mode"] == "first-champion-in-lineage"


def test_honest_risk_candidate_never_falls_back_to_legacy_auc_champion(tmp_path):
    warehouse = _wh(tmp_path)
    registry = ModelRegistry(get_settings())
    legacy = _Stub("legacy-row-random")
    champion, rows = registry.persist(
        warehouse, "r1", "risk", legacy,
        {"group_auc": 0.8, "n_train": 100}, {},
    )
    assert champion is True
    warehouse.insert_rows("model_runs", rows)

    honest_candidate = _Stub("honest-but-held")
    honest_candidate.eval_provenance = {
        "schema_version": "cohort-eval-v1",
        "label_version": "risk-escalation-exact14d-v1",
        "feature_set_hash": "features",
        "benchmark_kind": "package-disjoint-cross-validation-cohort",
        "benchmark_hash": "current",
    }
    served, version = registry.select_for_serving(
        warehouse, "risk", honest_candidate, False, _Stub
    )
    assert served is None
    assert version is None


def test_new_evaluation_lineage_can_bootstrap_past_legacy_champion(tmp_path):
    warehouse = _wh(tmp_path)
    registry = ModelRegistry(get_settings())
    legacy = _Stub("legacy-row-random")
    champion, rows = registry.persist(
        warehouse, "r1", "risk", legacy,
        {"group_auc": 0.99, "n_train": 100}, {},
    )
    assert champion is True
    warehouse.insert_rows("model_runs", rows)

    honest_candidate = _Stub("package-disjoint")
    honest_candidate.eval_provenance = {
        "schema_version": "cohort-eval-v1",
        "label_version": "risk-escalation-exact14d-v1",
        "feature_set_hash": "features",
        "benchmark_kind": "package-disjoint-cross-validation-cohort",
        "benchmark_hash": "current",
    }
    promoted, candidate_rows = registry.persist(
        warehouse, "r2", "risk", honest_candidate,
        {"group_auc": 0.60, "n_train": 100}, {},
    )

    assert promoted is True
    primary = next(row for row in candidate_rows if row["metric_name"] == "group_auc")
    assert primary["comparison_mode"] == "first-champion-in-lineage"
    assert "first champion in evaluation lineage" in primary["notes"]


def test_risk_lineage_cannot_bootstrap_below_absolute_auc_floor(tmp_path):
    candidate = _Stub("worse-than-random")
    candidate.eval_provenance = {
        "schema_version": "cohort-eval-v1",
        "label_version": "risk-escalation-exact14d-v1",
        "benchmark_kind": "stable-package-disjoint-holdout",
        "split_version": "stable-package-sha256-mod5-v1",
        "benchmark_hash": "current",
    }

    promoted, rows = ModelRegistry(get_settings()).persist(
        _wh(tmp_path),
        "r-low",
        "risk",
        candidate,
        {"group_auc": 0.49, "n_train": 100, "n_test": 20},
        {},
    )

    assert promoted is False
    primary = next(row for row in rows if row["metric_name"] == "group_auc")
    assert primary["comparison_mode"] == "bootstrap-floor"
    assert "absolute floor" in primary["notes"]


def test_cloud_candidate_cannot_promote_without_durable_artifact(tmp_path, monkeypatch):
    from oss_radar.config import Settings

    registry = ModelRegistry(Settings(backend="bigquery", gcp_project="test-project"))
    monkeypatch.setattr(registry, "_upload_gcs", lambda *args, **kwargs: None)
    model = _Stub("not-durable")
    model.eval_provenance = _provenance("cohort-a")

    promoted, rows = registry.persist(
        _wh(tmp_path), "r-cloud", "growth", model,
        {"spearman": 0.8, "n_train": 100, "n_test": 20}, {}, gate_passed=True,
    )

    assert promoted is False
    assert {row["gcs_uri"] for row in rows} == {""}
    assert {row["served_version"] for row in rows} == {""}
    assert all("artifact persistence failure" in row["notes"] for row in rows)

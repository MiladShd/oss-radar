"""Self-healing (ingest retry/carry-forward) and self-improvement (feature experiments)."""

from datetime import date

import numpy as np
import pandas as pd

from oss_radar.config.active_features import active_download_features, with_candidate
from oss_radar.features import DOWNLOAD_FEATURES
from oss_radar.ingest.healing import _carry_forward, identify_failures
from oss_radar.models.experiment import best_candidate, evaluate_candidates
from oss_radar.warehouse.duckdb_backend import DuckDBWarehouse


# --- self-improvement: feature experiments ---
def _train_df(n=400):
    rng = np.random.default_rng(7)
    signal = rng.normal(0, 1, n)
    return pd.DataFrame({
        "feature_date": pd.date_range("2026-01-01", periods=n).date,
        "log_d7": rng.normal(0, 1, n),
        "velocity": rng.normal(0, 1, n),
        "recent_share": signal,                     # informative candidate
        "dow_volatility_7": rng.normal(0, 1, n),    # noise candidate
        "growth_target_7d": signal * 0.5 + rng.normal(0, 0.05, n),
    })


def test_experiment_detects_useful_feature():
    df = _train_df()
    results = evaluate_candidates(df, ["log_d7", "velocity"], ["recent_share", "dow_volatility_7"], seed=1)
    by = {r["candidate"]: r for r in results}
    assert by["recent_share"]["delta"] > by["dow_volatility_7"]["delta"]
    winner = best_candidate(results, margin=0.05)
    assert winner is not None and winner["candidate"] == "recent_share"


def test_no_proposal_when_nothing_helps():
    rng = np.random.default_rng(3)
    n = 300
    df = pd.DataFrame({
        "feature_date": pd.date_range("2026-01-01", periods=n).date,
        "log_d7": rng.normal(0, 1, n), "velocity": rng.normal(0, 1, n),
        "dow_volatility_7": rng.normal(0, 1, n),
        "growth_target_7d": rng.normal(0, 1, n),  # pure noise target
    })
    results = evaluate_candidates(df, ["log_d7", "velocity"], ["dow_volatility_7"], seed=1)
    assert best_candidate(results, margin=0.05) is None


def test_active_features_default_and_candidate_toggle():
    assert active_download_features() == list(DOWNLOAD_FEATURES)  # json ships with defaults
    cfg = with_candidate("recent_share")
    assert "recent_share" in cfg["download"]
    assert cfg["download"][:len(DOWNLOAD_FEATURES)] == list(DOWNLOAD_FEATURES)


# --- self-healing ---
def test_identify_failures():
    snaps = [{"name": "a", "downloads_7d": 100}, {"name": "b", "downloads_7d": None},
             {"name": "c", "downloads_7d": 5}]
    assert identify_failures(snaps) == ["b"]


def test_carry_forward_restores_last_good(tmp_path):
    wh = DuckDBWarehouse(path=str(tmp_path / "h.duckdb"))
    wh.init_schema()
    wh.insert_rows("snapshots", [{
        "run_id": "r0", "snapshot_date": date(2026, 6, 1), "name": "pkg", "category": "llm",
        "downloads_7d": 500, "stars": 100, "vuln_count": 2,
    }])
    snapshots = [{"run_id": "r1", "snapshot_date": date(2026, 6, 15), "name": "pkg",
                  "category": "llm", "downloads_7d": None}]
    healed = _carry_forward(wh, "r1", ["pkg"], snapshots, {"pkg": 0})
    assert healed == 1
    assert snapshots[0]["downloads_7d"] == 500   # carried forward from last good
    assert snapshots[0]["stars"] == 100
    assert snapshots[0]["run_id"] == "r1"          # identity is the current run

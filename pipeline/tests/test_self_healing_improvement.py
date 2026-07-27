"""Self-healing (ingest retry/carry-forward) and self-improvement (feature experiments)."""

import json
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd

from oss_radar.config.active_features import CONFIG_PATH, active_download_features, with_candidate
from oss_radar.features import ALL_DOWNLOAD_FEATURES, GROWTH_TARGET_COLUMN
from oss_radar.ingest.healing import _carry_forward, identify_failures
from oss_radar.models.evaluation import date_grouped_train_validation_test
from oss_radar.models.experiment import (
    _development_frame,
    best_candidate,
    evaluate_candidates,
    validate_feature_candidate,
)
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
        GROWTH_TARGET_COLUMN: signal * 0.5 + rng.normal(0, 0.05, n),
    })


def test_experiment_detects_useful_feature():
    df = _train_df()
    results = evaluate_candidates(df, ["log_d7", "velocity"], ["recent_share", "dow_volatility_7"], seed=1)
    by = {r["candidate"]: r for r in results}
    assert by["recent_share"]["delta"] > by["dow_volatility_7"]["delta"]
    winner = best_candidate(results, margin=0.05)
    assert winner is not None and winner["candidate"] == "recent_share"
    assert winner["selection"] == "nested-development-only"


def test_experiment_never_reads_outer_governance_test_dates():
    df = _train_df()
    outer = date_grouped_train_validation_test(df)
    development = _development_frame(df)

    assert set(development["feature_date"]).isdisjoint(set(outer.test["feature_date"]))
    assert len(development) == len(outer.train) + len(outer.validation)


def test_no_proposal_when_nothing_helps():
    rng = np.random.default_rng(3)
    n = 300
    df = pd.DataFrame({
        "feature_date": pd.date_range("2026-01-01", periods=n).date,
        "log_d7": rng.normal(0, 1, n), "velocity": rng.normal(0, 1, n),
        "dow_volatility_7": rng.normal(0, 1, n),
        GROWTH_TARGET_COLUMN: rng.normal(0, 1, n),  # pure noise target
    })
    results = evaluate_candidates(df, ["log_d7", "velocity"], ["dow_volatility_7"], seed=1)
    assert best_candidate(results, margin=0.05) is None


def test_feature_pr_validation_reproduces_one_allowlisted_addition():
    frame = _train_df()
    base = {"download": ["log_d7", "velocity"], "risk": ["log_stars"]}
    head = {"download": ["log_d7", "velocity", "recent_share"], "risk": ["log_stars"]}

    result = validate_feature_candidate(frame, base, head, margin=0.05, seed=1)

    assert result is not None
    assert result["candidate"] == "recent_share"


def test_feature_pr_validation_refuses_to_skip_on_thin_data():
    frame = _train_df(n=100)
    base = {"download": ["log_d7", "velocity"], "risk": ["log_stars"]}
    head = {"download": ["log_d7", "velocity", "recent_share"], "risk": ["log_stars"]}

    try:
        validate_feature_candidate(frame, base, head, margin=0.01, seed=1)
    except ValueError as exc:
        assert "refusing to skip" in str(exc)
    else:
        raise AssertionError("thin feature validation unexpectedly passed")


def test_active_features_config_and_candidate_toggle():
    configured = json.loads(CONFIG_PATH.read_text())["download"]
    expected = [f for f in configured if f in ALL_DOWNLOAD_FEATURES]
    assert active_download_features() == expected
    cfg = with_candidate("recent_share")
    assert "recent_share" in cfg["download"]
    assert cfg["download"][:len(expected)] == expected


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
        "source_status": {"pypi_downloads": True, "github": True},
        "ingested_at": datetime(2026, 6, 1, 12, tzinfo=UTC),
    }])
    attempted_at = datetime(2026, 6, 15, 12, tzinfo=UTC)
    failed_status = {"pypi_downloads": False, "github": False}
    snapshots = [{"run_id": "r1", "snapshot_date": date(2026, 6, 15), "name": "pkg",
                  "category": "llm", "downloads_7d": None,
                  "source_status": failed_status, "ingested_at": attempted_at}]
    healed = _carry_forward(wh, "r1", ["pkg"], snapshots, {"pkg": 0})
    assert healed == 1
    assert snapshots[0]["downloads_7d"] == 500   # carried forward from last good
    assert snapshots[0]["stars"] == 100
    assert snapshots[0]["run_id"] == "r1"          # identity is the current run
    assert snapshots[0]["source_status"] == failed_status
    assert snapshots[0]["ingested_at"] == attempted_at

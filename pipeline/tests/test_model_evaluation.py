"""Operational model-evaluation contracts: temporal isolation and persisted provenance."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from oss_radar.features import DOWNLOAD_FEATURES, GROWTH_TARGET_COLUMN, RISK_FEATURES
from oss_radar.models.evaluation import (
    GROWTH_LABEL_VERSION,
    date_grouped_train_validation_test,
    growth_evaluation_provenance,
    stable_frame_hash,
)
from oss_radar.models.growth import GrowthModel
from oss_radar.models.risk import RiskModel
from oss_radar.orchestrator.pipeline import _persist_features


def _growth_frame(n_dates: int = 80, packages_per_date: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(12)
    dates = pd.date_range("2025-01-01", periods=n_dates).date
    rows = []
    for feature_date in dates:
        for package_index in range(packages_per_date):
            features = {feature: float(rng.normal()) for feature in DOWNLOAD_FEATURES}
            features.update({
                "name": f"pkg{package_index}",
                "feature_date": feature_date,
                GROWTH_TARGET_COLUMN: 0.5 * features["mom_7v7"] + rng.normal(0, 0.1),
            })
            rows.append(features)
    return pd.DataFrame(rows)


def test_temporal_split_keeps_each_date_in_exactly_one_partition():
    frame = _growth_frame().sample(frac=1.0, random_state=7)
    split = date_grouped_train_validation_test(frame)
    date_sets = [
        set(part["feature_date"])
        for part in (split.train, split.validation, split.test)
    ]
    assert date_sets[0].isdisjoint(date_sets[1])
    assert date_sets[0].isdisjoint(date_sets[2])
    assert date_sets[1].isdisjoint(date_sets[2])
    assert max(date_sets[0]) < min(date_sets[1]) < min(date_sets[2])
    assert sum(len(part) for part in (split.train, split.validation, split.test)) == len(frame)


def test_growth_reports_unclipped_test_outcomes():
    frame = _growth_frame()
    split = date_grouped_train_validation_test(frame)
    test_mask = frame["feature_date"].isin(set(split.test["feature_date"]))
    frame.loc[test_mask, GROWTH_TARGET_COLUMN] = np.linspace(10.0, 10.1, int(test_mask.sum()))
    model = GrowthModel(seed=3)
    metrics = model.fit(frame)
    assert metrics["test_clip_rate"] == 1.0
    assert metrics["mae"] > 5.0  # would be much smaller if test labels were clipped to 3.0
    assert metrics["n_validation"] > 0
    assert model.eval_provenance["early_stopping_partition"] == "validation"
    assert model.eval_provenance["test_target_clipped"] is False


def test_benchmark_hash_versions_the_exact_closed_cohort():
    frame = _growth_frame()
    split = date_grouped_train_validation_test(frame)
    first = growth_evaluation_provenance(frame, split, DOWNLOAD_FEATURES)
    changed = frame.copy()
    test_index = split.test.index[0]
    changed.loc[test_index, GROWTH_TARGET_COLUMN] += 0.25
    changed_split = date_grouped_train_validation_test(changed)
    second = growth_evaluation_provenance(changed, changed_split, DOWNLOAD_FEATURES)
    assert first["label_version"] == GROWTH_LABEL_VERSION
    assert first["benchmark_hash"] != second["benchmark_hash"]
    assert first["feature_set_hash"] == second["feature_set_hash"]


def test_stable_frame_hash_normalizes_missing_and_nonfinite_values():
    frame = pd.DataFrame({
        "name": ["a", "b", "c", "d"],
        "value": [1.0, np.nan, np.inf, -np.inf],
        "when": [pd.Timestamp("2026-01-01"), pd.NaT, pd.Timestamp("2026-01-03"), pd.NaT],
    })

    first = stable_frame_hash(frame, ["name", "value", "when"])
    second = stable_frame_hash(
        frame.sample(frac=1.0, random_state=9), ["name", "value", "when"]
    )

    assert len(first) == 64
    assert first == second


def test_risk_model_handles_live_shaped_missing_features(tmp_path):
    rng = np.random.default_rng(18)
    frame = pd.DataFrame({
        feature: rng.normal(size=100)
        for feature in RISK_FEATURES
    })
    frame["name"] = [f"pkg{i}" for i in range(100)]
    frame["feature_date"] = date(2026, 1, 1)
    frame["outcome_date"] = date(2026, 1, 15)
    frame["at_risk_label"] = [i % 2 for i in range(100)]
    frame["label_version"] = "risk-escalation-exact14d-v1"
    frame.loc[::3, RISK_FEATURES[0]] = np.nan
    frame.loc[::4, RISK_FEATURES[1]] = np.inf
    # Production coercion turns non-finite connector values into missing values before fitting.
    frame[RISK_FEATURES[1]] = frame[RISK_FEATURES[1]].replace([np.inf, -np.inf], np.nan)

    model = RiskModel(seed=4)
    metrics = model.fit(frame)

    assert model.model is not None
    assert model.calibrator is not None
    assert metrics["group_auc"] == metrics["group_auc"]
    assert metrics["brier"] == metrics["brier"]
    assert metrics["probability_calibration"] == "platt-package-disjoint-oof"
    assert metrics["n_test_packages"] > 0
    assert model.eval_provenance["benchmark_kind"] == "stable-package-disjoint-holdout"

    artifact = tmp_path / "risk.pkl"
    before = model.predict_proba(frame.iloc[:8])
    model.save(str(artifact))
    restored = RiskModel.load(str(artifact))
    assert restored.calibrator is not None
    assert np.allclose(before, restored.predict_proba(frame.iloc[:8]))


class _CaptureWarehouse:
    def __init__(self):
        self.rows = []

    def insert_rows(self, table, rows):
        assert table == "features"
        self.rows.extend(rows)


def test_persisted_download_features_are_counts_not_log_values():
    score = pd.DataFrame([{
        "name": "pkg",
        "feature_date": date(2026, 1, 1),
        "log_d7": math.log1p(700),
        "log_d28": math.log1p(2800),
        "velocity": 100.0,
    }])
    snapshots = pd.DataFrame([{"name": "pkg", "category": "llm"}])
    risk = pd.DataFrame([{"name": "pkg", "at_risk_label": 0}])
    warehouse = _CaptureWarehouse()
    _persist_features(warehouse, "run", score, risk, snapshots)
    row = warehouse.rows[0]
    assert row["downloads_7d"] == pytest.approx(700)
    assert row["downloads_28d"] == pytest.approx(2800)
    assert row["download_velocity"] == 100.0

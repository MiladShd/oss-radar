"""Feature engineering: supervised growth rows + cross-sectional risk frame."""

from datetime import date, timedelta

import pandas as pd

from oss_radar.features import (
    GROWTH_TARGET_COLUMN,
    build_growth_scoring,
    build_growth_training,
    build_risk_frame,
)


def _synthetic_history(n_days=220, base=1000, growth=1.01):
    start = date(2026, 1, 1)
    rows = []
    val = base
    for i in range(n_days):
        rows.append({"name": "demo", "date": start + timedelta(days=i), "downloads": int(val)})
        val *= growth
    return pd.DataFrame(rows)


def test_growth_training_has_labels_and_features():
    hist = _synthetic_history()
    train = build_growth_training(hist, horizon=70, stride=3, min_history=84)
    assert not train.empty
    for col in ("log_d7", "velocity", "mom_7v7", "trend_slope_28", GROWTH_TARGET_COLUMN, "feature_date"):
        assert col in train.columns
    # steady upward series => non-trivial positive momentum signal
    assert train["mom_7v7"].mean() > 0.9
    assert train[GROWTH_TARGET_COLUMN].notna().all()


def test_growth_scoring_one_row_per_package():
    hist = _synthetic_history()
    score = build_growth_scoring(hist)
    assert len(score) == 1
    assert score.iloc[0]["name"] == "demo"
    assert score.iloc[0]["log_d7"] > 0


def test_risk_frame_handles_missing_values():
    snaps = pd.DataFrame([
        {"name": "a", "category": "llm", "stars": 1000, "bus_factor": 0.9,
         "vuln_new_28d": 0, "archived": False, "days_since_last_release": 10},
        # missing/NaN-heavy row must not raise
        {"name": "b", "category": "llm", "stars": None, "bus_factor": None,
         "max_severity": None, "archived": None, "days_since_last_release": None,
         "vuln_new_28d": None, "dependent_repos_count": None},
        {"name": "c", "category": "agents", "archived": True, "dependent_repos_count": 5},
    ])
    rf = build_risk_frame(snaps)
    assert list(rf["name"]) == ["a", "b", "c"]
    assert set(rf["at_risk_label"].unique()).issubset({0, 1})
    # archived package is flagged at-risk
    assert int(rf[rf["name"] == "c"]["at_risk_label"].iloc[0]) == 1

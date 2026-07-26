"""Self-improvement machinery: drift detection + forward-outcome relabeling."""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from oss_radar.features.forward import build_forward_risk_labels, choose_risk_training
from oss_radar.models.drift import _psi, compute_prediction_drift
from oss_radar.models.evaluation import risk_holdout_mask
from oss_radar.models.risk import risk_cv


def _preds(scores, labels):
    return pd.DataFrame({
        "name": [f"p{i}" for i in range(len(scores))],
        "momentum_score": scores, "risk_score": scores,
        "momentum_label": labels, "risk_level": labels,
    })


def test_psi_zero_for_identical():
    rng = np.random.default_rng(0)
    x = rng.normal(50, 10, 500)
    assert _psi(x, x) < 1e-6


def test_drift_unavailable_without_prior():
    d = compute_prediction_drift(None, _preds([50, 60], ["normal", "high"]))
    assert d["available"] is False


def test_drift_detects_shift():
    base = _preds([50] * 40, ["normal"] * 40)
    same = compute_prediction_drift(base, base)
    assert same["severity"] == "low"
    shifted = _preds([85] * 40, ["high"] * 40)  # big distribution + label change
    moved = compute_prediction_drift(base, shifted)
    assert moved["available"] is True
    assert moved["label_churn"] == 1.0
    assert moved["severity"] == "high"


def _history(n=30, span_days=20, n_escalating=10):
    """n packages, each with a t0 and a tN snapshot span_days apart."""
    t0 = date(2026, 6, 1)
    rows = []
    for i in range(n):
        esc = i < n_escalating
        for d, vulns in [(t0, 1), (t0 + timedelta(days=span_days), 4 if esc else 1)]:
            rows.append({
                "name": f"pkg{i}", "category": "llm", "snapshot_date": d,
                "stars": 1000, "forks": 100, "open_issues": 10,
                "dependent_repos_count": 500, "dependent_packages_count": 50,
                "bus_factor": 0.5, "release_cadence_days": 30, "dependency_count": 5,
                "scorecard_overall": 7, "commit_count_4w": 20, "prs_merged_7d": 5,
                "issues_opened_7d": 3, "rank_average": 5.0, "downloads_7d": 10000,
                "days_since_last_release": 10, "archived": False, "status": None,
                "vuln_count": vulns, "vuln_new_28d": 0, "max_severity": None,
            })
    return pd.DataFrame(rows)


def test_forward_labels_capture_escalation():
    hist = _history(n=30, span_days=14, n_escalating=10)
    fwd = build_forward_risk_labels(hist, horizon_days=14)
    assert len(fwd) == 30
    assert int(fwd["at_risk_label"].sum()) == 10  # exactly the vuln-increasing packages


def test_forward_skipped_when_history_too_short():
    hist = _history(n=30, span_days=5)  # < 14-day horizon
    assert build_forward_risk_labels(hist, horizon_days=14).empty


def test_forward_labels_require_an_exact_horizon_snapshot():
    # A day-15 observation must not silently become a variable-length "14-day" outcome.
    hist = _history(n=30, span_days=15, n_escalating=10)
    assert build_forward_risk_labels(hist, horizon_days=14).empty


def test_forward_labels_roll_across_every_eligible_day_at_fixed_horizon():
    start = date(2026, 1, 1)
    rows = []
    for package, escalation_day in (("safe", None), ("risky", 16)):
        for offset in range(20):
            rows.append({
                "name": package, "category": "llm", "snapshot_date": start + timedelta(days=offset),
                "vuln_count": int(escalation_day is not None and offset >= escalation_day),
                "downloads_7d": 1000, "days_since_last_release": 10,
                "archived": False, "status": None,
            })
    frame = build_forward_risk_labels(pd.DataFrame(rows), horizon_days=14)
    # Days 0..5 each have a day >= anchor+14, yielding six labels per package rather than one.
    assert len(frame) == 12
    assert frame.groupby("name").size().to_dict() == {"risky": 6, "safe": 6}
    # Only risky anchors whose fixed 14-day outcome reaches day 16 should escalate.
    assert int(frame["at_risk_label"].sum()) == 4


def test_choose_risk_training_switches_mode():
    heuristic = pd.DataFrame({"name": ["a"], "at_risk_label": [0]})
    short = _history(n=30, span_days=5)
    frame, mode = choose_risk_training(heuristic, short, horizon_days=14, min_rows=25)
    assert mode == "heuristic" and frame is heuristic

    long = _history(n=30, span_days=14, n_escalating=10)
    frame2, mode2 = choose_risk_training(heuristic, long, horizon_days=14, min_rows=25)
    assert mode2 == "forward-outcome" and len(frame2) == 30
    assert frame2["label_version"].nunique() == 1
    assert set(
        pd.to_datetime(frame2["outcome_date"]) - pd.to_datetime(frame2["feature_date"])
    ) == {pd.Timedelta(days=14)}


def test_risk_validation_keeps_each_package_in_one_fold():
    groups = np.array([f"pkg{i}" for i in range(10) for _ in range(3)])
    labels = np.array([i % 2 for i in range(10) for _ in range(3)])
    cv, cv_groups = risk_cv(labels, groups, seed=7)
    assert cv_groups is groups
    for train_idx, test_idx in cv.split(np.zeros(len(labels)), labels, cv_groups):
        assert set(groups[train_idx]).isdisjoint(groups[test_idx])


def test_risk_holdout_is_stable_and_package_disjoint():
    frame = pd.DataFrame({
        "name": [f"pkg{i}" for i in range(100) for _ in range(2)],
        "at_risk_label": [i % 2 for i in range(100) for _ in range(2)],
    })
    first = risk_holdout_mask(frame)
    shuffled = frame.sample(frac=1.0, random_state=5)
    second = risk_holdout_mask(shuffled)

    reserved = set(frame.loc[first, "name"])
    assert 10 <= len(reserved) <= 30
    assert reserved == set(shuffled.loc[second, "name"])
    assert reserved.isdisjoint(set(frame.loc[~first, "name"]))

"""Self-improvement machinery: drift detection + forward-outcome relabeling."""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from oss_radar.features.forward import build_forward_risk_labels, choose_risk_training
from oss_radar.models.drift import _psi, compute_prediction_drift


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
    hist = _history(n=30, span_days=20, n_escalating=10)
    fwd = build_forward_risk_labels(hist, horizon_days=14)
    assert len(fwd) == 30
    assert int(fwd["at_risk_label"].sum()) == 10  # exactly the vuln-increasing packages


def test_forward_skipped_when_history_too_short():
    hist = _history(n=30, span_days=5)  # < 14-day horizon
    assert build_forward_risk_labels(hist, horizon_days=14).empty


def test_choose_risk_training_switches_mode():
    heuristic = pd.DataFrame({"name": ["a"], "at_risk_label": [0]})
    short = _history(n=30, span_days=5)
    frame, mode = choose_risk_training(heuristic, short, horizon_days=14, min_rows=25)
    assert mode == "heuristic" and frame is heuristic

    long = _history(n=30, span_days=20, n_escalating=10)
    frame2, mode2 = choose_risk_training(heuristic, long, horizon_days=14, min_rows=25)
    assert mode2 == "forward-outcome" and len(frame2) == 30

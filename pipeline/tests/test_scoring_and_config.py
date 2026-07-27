"""Scoring transforms, watchlist integrity, and repo parsing."""

import pandas as pd

from oss_radar.config.packages import CATEGORIES, get_watchlist
from oss_radar.features.engineering import _at_risk_label
from oss_radar.ingest.pypi_metadata import _discover_repo, parse_owner_repo
from oss_radar.models.scoring import (
    build_predictions,
    momentum_from_pred,
    persistence_growth_predictions,
    risk_composite,
)


def test_momentum_monotonic_and_bounded():
    lo, _ = momentum_from_pred(-1.0)
    mid, _ = momentum_from_pred(0.0)
    hi, _ = momentum_from_pred(1.0)
    assert 0 <= lo < mid < hi <= 100
    assert abs(mid - 50) < 0.001  # zero growth -> 50


def test_risk_composite_handles_nan():
    row = pd.Series({"max_severity": None, "vuln_new_28d": float("nan"), "bus_factor": float("nan"),
                     "days_since_last_release": float("nan"), "scorecard_overall": float("nan"),
                     "archived": None, "status": None, "issues_opened_7d": None, "prs_merged_7d": None})
    score, reasons = risk_composite(row)
    assert 0 <= score <= 100  # must not be NaN
    assert isinstance(reasons, list)


def test_risk_composite_flags_critical_vuln():
    safe = risk_composite(pd.Series({"max_severity": None, "vuln_new_28d": 0, "bus_factor": 0.9,
                                     "days_since_last_release": 5, "scorecard_overall": 9}))[0]
    risky = risk_composite(pd.Series({"max_severity": "CRITICAL", "vuln_new_28d": 3, "bus_factor": 0.1,
                                      "days_since_last_release": 600, "scorecard_overall": 2}))[0]
    assert risky > safe


def test_categorical_risk_hazards_have_high_safety_floors():
    archived, archived_reasons = risk_composite(pd.Series({
        "archived": True,
        "status": None,
        "max_severity": None,
        "vuln_new_28d": 0,
    }))
    critical, critical_reasons = risk_composite(pd.Series({
        "archived": False,
        "status": None,
        "max_severity": "CRITICAL",
        "max_severity_new_28d": "CRITICAL",
        "vuln_new_28d": 1,
    }))

    assert archived >= 85
    assert critical >= 75
    assert archived_reasons[0] == "archived / removed"
    assert critical_reasons[0] == "recent critical vulnerability signal"


def test_lifetime_severity_without_a_recent_vulnerability_does_not_trigger_floor():
    score, reasons = risk_composite(pd.Series({
        "archived": False,
        "status": None,
        "max_severity": "CRITICAL",
        "max_severity_new_28d": None,
        "vuln_new_28d": 0,
        "bus_factor": 0.9,
        "days_since_last_release": 5,
        "scorecard_overall": 9,
    }))

    assert score < 66
    assert "recent critical vulnerability signal" not in reasons


def test_risk_label_requires_recent_severity_not_lifetime_severity():
    old_critical = pd.Series({
        "vuln_new_28d": 1,
        "max_severity": "CRITICAL",
        "max_severity_new_28d": None,
        "archived": False,
        "status": None,
        "days_since_last_release": 10,
        "bus_factor": 0.5,
        "dependent_repos_count": 50,
    })
    recent_high = old_critical.copy()
    recent_high["max_severity_new_28d"] = "HIGH"

    assert _at_risk_label(old_critical) == 0
    assert _at_risk_label(recent_high) == 1


def test_classifier_cannot_dilute_a_critical_vulnerability_below_high():
    class ZeroRiskModel:
        @staticmethod
        def predict_proba(frame):
            return [0.0] * len(frame)

    predictions = build_predictions(
        "run",
        pd.DataFrame({"name": ["critical-package"], "mom_56v56": [1.0]}),
        pd.DataFrame([{
            "name": "critical-package",
            "category": "framework",
            "max_severity": "CRITICAL",
            "max_severity_new_28d": "CRITICAL",
            "vuln_new_28d": 1,
            "archived": False,
            "status": None,
        }]),
        pd.DataFrame({"name": ["critical-package"]}),
        growth_model=None,
        risk_model=ZeroRiskModel(),
    )
    row = predictions.iloc[0]

    assert row["risk_composite_score"] >= 75
    assert row["risk_score"] >= 75
    assert row["risk_level"] == "high"
    assert row["risk_reasons"][0] == "recent critical vulnerability signal"


def test_growth_persistence_fallback_is_neutral_at_flat_momentum():
    predictions, reasons = persistence_growth_predictions(pd.DataFrame({
        "mom_56v56": [1.0, 2.0, 0.5],
    }))

    assert predictions[0] == 0.0
    assert predictions[1] > 0
    assert predictions[2] < 0
    assert reasons[0][0][0] == "mom_56v56"


def test_watchlist_integrity():
    wl = get_watchlist()
    names = [p["name"] for p in wl]
    assert len(names) == len(set(names)), "watchlist has duplicate package names"
    assert len(wl) >= 80
    for p in wl:
        assert p["category"] in CATEGORIES
        assert p["name"] == p["name"].lower()
    assert len(get_watchlist(limit=5)) == 5


def test_repo_parsing():
    assert parse_owner_repo("https://github.com/vllm-project/vllm") == ("vllm-project", "vllm")
    assert parse_owner_repo("https://github.com/psf/requests.git") == ("psf", "requests")
    assert parse_owner_repo("https://pypi.org/project/foo") is None
    info = {"home_page": None, "project_urls": {"Homepage": "https://x.io",
            "Source": "https://github.com/run-llama/llama_index"}}
    assert "github.com/run-llama/llama_index" in _discover_repo(info)

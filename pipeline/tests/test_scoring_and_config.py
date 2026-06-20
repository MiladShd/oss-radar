"""Scoring transforms, watchlist integrity, and repo parsing."""

import pandas as pd

from oss_radar.config.packages import CATEGORIES, get_watchlist
from oss_radar.ingest.pypi_metadata import _discover_repo, parse_owner_repo
from oss_radar.models.scoring import momentum_from_pred, risk_composite


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

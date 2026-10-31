"""Wording helpers: operational text must be short, plain, and free of raw identifiers."""

from __future__ import annotations

import pytest

from oss_radar.textfmt import (
    age_phrase,
    count,
    duration,
    join_and,
    metric_label,
    percent,
    source_label,
    stage_label,
    version_label,
)


@pytest.mark.parametrize("seconds,expected", [
    (0.2, "under 1 s"), (45, "45 s"), (60, "1 min"), (444, "7 min 24 s"),
    (3600, "1 h"), (4320, "1 h 12 min"), (86400 * 15, "15 days"), (None, "—"), (-3, "—"), ("x", "—"),
])
def test_duration(seconds, expected):
    assert duration(seconds) == expected


@pytest.mark.parametrize("hours,expected", [
    (0.0, "just now"), (0.5, "30 minutes ago"), (1, "1 hour ago"), (3, "3 hours ago"),
    (47, "47 hours ago"), (360, "15 days ago"), (None, "at an unknown time"),
])
def test_age_phrase(hours, expected):
    assert age_phrase(hours) == expected


def test_numbers_and_lists():
    assert count(23114) == "23,114"
    assert count(None) == "—"
    assert percent(0.413) == "41%"
    assert percent("bad") == "n/a"
    assert join_and([]) == ""
    assert join_and(["a"]) == "a"
    assert join_and(["a", "b"]) == "a and b"
    assert join_and(["a", "b", "c"]) == "a, b and c"


def test_labels_hide_internal_names():
    assert source_label("ecosystems_pkg") == "ecosyste.ms packages"
    assert source_label("unknown_thing") == "unknown thing"
    assert metric_label("spearman") == "Spearman rank correlation"
    assert metric_label("group_auc") == "package-holdout AUC"
    assert stage_label("self_audit") == "Self-audit"


def test_version_label_turns_ids_into_dates():
    assert version_label("growth-20261005T093330Z") == "trained 5 Oct 2026"
    assert version_label("growth-r1") == "version growth-r1"
    assert version_label(None) == "an unknown model"

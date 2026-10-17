"""Human-readable wording helpers shared by the pipeline agents and the dashboard.

Operational text is read by people deciding whether something needs attention, so it follows a few rules:
lead with the outcome, say what it means, and keep identifiers (run ids, hashes) out of the sentence.
Pure functions, no dependencies, so the lean dashboard image can import this module.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

SOURCE_LABELS = {
    "depsdev": "deps.dev",
    "ecosystems_pkg": "ecosyste.ms packages",
    "ecosystems_repo": "ecosyste.ms repositories",
    "github": "GitHub",
    "osv": "OSV",
    "pypi_downloads": "PyPI downloads",
    "pypi_metadata": "PyPI metadata",
}

METRIC_LABELS = {
    "spearman": "Spearman rank correlation",
    "auc": "AUC",
    "roc_auc": "AUC",
    "group_auc": "package-holdout AUC",
    "r2": "R²",
    "mae": "MAE",
}

STAGE_LABELS = {
    "ingest": "Data ingestion",
    "features": "Feature building",
    "train": "Model training",
    "score": "Scoring",
    "self_audit": "Self-audit",
    "agents": "Agent reports",
}

LABEL_MODE_LABELS = {
    "forward-outcome": "realized outcomes",
    "heuristic": "heuristic rules",
}

SOURCE_MODE_LABELS = {
    "live": "live data sources",
    "smoke": "the offline smoke fixture",
    "fixture": "a fixed fixture",
}

_VERSION_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})T\d{6}Z")


def count(value) -> str:
    """Thousands-separated integer, or an em dash when the value is not numeric."""
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "—"


def percent(rate, digits: int = 0) -> str:
    try:
        return f"{float(rate) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "n/a"


def duration(seconds) -> str:
    """'7 min 24 s', '45 s', '1 h 12 min', '15 days'; an em dash when unknown."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(s) or s < 0:
        return "—"
    if s < 1:
        return "under 1 s"
    if s < 60:
        return f"{round(s)} s"
    if s < 3600:
        minutes, rest = divmod(int(round(s)), 60)
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    if s < 86400:
        hours, rest = divmod(int(round(s / 60)), 60)
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    days = s / 86400
    return f"{round(days)} day{'s' if round(days) != 1 else ''}"


def age_phrase(hours) -> str:
    """Relative time such as '3 hours ago' or '15 days ago'."""
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return "at an unknown time"
    if h < 0.02:
        return "just now"
    if h < 1:
        return f"{round(h * 60)} minutes ago"
    if h < 48:
        n = round(h)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = round(h / 24)
    return f"{n} days ago"


def source_label(key: str) -> str:
    return SOURCE_LABELS.get(key, str(key).replace("_", " "))


def metric_label(name: str | None) -> str:
    return METRIC_LABELS.get(str(name), str(name or "metric").replace("_", " "))


def stage_label(name: str) -> str:
    return STAGE_LABELS.get(name, str(name).replace("_", " ").capitalize())


def label_mode(mode: str | None) -> str:
    return LABEL_MODE_LABELS.get(str(mode), str(mode))


def source_mode(mode: str | None) -> str:
    return SOURCE_MODE_LABELS.get(str(mode), str(mode))


def version_label(version: str | None) -> str:
    """'growth-20261005T093330Z' -> 'trained 5 Oct 2026'; unknown formats pass through."""
    if not version:
        return "an unknown model"
    match = _VERSION_DATE.search(str(version))
    if not match:
        return f"version {version}"
    when = datetime(int(match[1]), int(match[2]), int(match[3]))
    return f"trained {when.day} {when:%b %Y}"


def join_and(items: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]

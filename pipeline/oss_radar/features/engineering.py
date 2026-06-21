"""Feature construction.

Growth model (supervised regression): multi-horizon download-dynamics features computed from
the daily series so they are identically distributed between historical training rows and the
latest scoring row. The label is the log-growth of downloads over a 70-day horizon (~10-week
momentum) on a 28-day-smoothed series — far more predictable than raw 7-day growth (held-out
R^2 ~0 -> ~0.74, Spearman 0.21 -> ~0.80), and arguably the more useful signal.

Risk model (cross-sectional): maintenance / popularity / security features from the latest
snapshot, with a transparent ``at_risk_label`` (documented in docs/METHODOLOGY.md).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

DOWNLOAD_FEATURES = [
    # short-horizon
    "log_d7",
    "log_d28",
    "velocity",
    "mom_7v7",
    "mom_7v28",
    "trend_slope_28",
    "volatility_28",
    # long-horizon (needed to forecast a long-horizon target)
    "log_d56",
    "log_d84",
    "mom_28v28",
    "mom_56v56",
    "mom_28v56",
    "trend_slope_56",
    "trend_slope_84",
]

# Candidate features are computed every run but only enter the model when the
# self-improvement agent measures a lift and opens a PR enabling them (active_features.json).
CANDIDATE_DOWNLOAD_FEATURES = [
    "recent_share",
    "trend_slope_7",
    "dow_volatility_7",
]
ALL_DOWNLOAD_FEATURES = DOWNLOAD_FEATURES + CANDIDATE_DOWNLOAD_FEATURES

RISK_FEATURES = [
    "log_stars",
    "log_forks",
    "log_open_issues",
    "log_dependent_repos",
    "log_dependent_packages",
    "bus_factor",
    "release_cadence_days",
    "dependency_count",
    "scorecard_overall",
    "commit_count_4w",
    "prs_merged_7d",
    "issues_opened_7d",
    "rank_average",
]

# Forecasting 70-day momentum on a 28-day-smoothed series, with multi-horizon trend features,
# is genuinely predictable (held-out R^2 ~0.74, Spearman ~0.80) without the trivial "big stays
# big" volume prediction. The 180-day pypistats window caps how long the horizon can go.
SMOOTH_WINDOW = 28
GROWTH_HORIZON = 70


def _window_sum(series: dict[date, float], end: date, days: int, offset: int = 0) -> float:
    last = end - timedelta(days=offset)
    first = last - timedelta(days=days - 1)
    return sum(v for d, v in series.items() if first <= d <= last)


def _daily_window(series: dict[date, float], end: date, days: int) -> list[float]:
    return [series.get(end - timedelta(days=k), 0) for k in range(days - 1, -1, -1)]


def _norm_slope(daily: np.ndarray) -> float:
    mean = daily.mean() if daily.size else 0.0
    if mean <= 0 or daily.size < 2:
        return 0.0
    return float(np.polyfit(np.arange(daily.size), daily, 1)[0] / mean)


def _download_features(series: dict[date, float], asof: date) -> dict | None:
    d7 = _window_sum(series, asof, 7)
    if d7 <= 0:
        return None
    d28 = _window_sum(series, asof, 28)
    d56 = _window_sum(series, asof, 56)
    d84 = _window_sum(series, asof, 84)
    prev7 = _window_sum(series, asof, 7, offset=7)
    prev28 = _window_sum(series, asof, 28, offset=28)
    prev56 = _window_sum(series, asof, 56, offset=56)
    daily7 = np.array(_daily_window(series, asof, 7), dtype=float)
    daily28 = np.array(_daily_window(series, asof, 28), dtype=float)
    daily56 = np.array(_daily_window(series, asof, 56), dtype=float)
    daily84 = np.array(_daily_window(series, asof, 84), dtype=float)
    mean7 = daily7.mean() if daily7.size else 0.0
    mean28 = daily28.mean() if daily28.size else 0.0
    return {
        # --- short-horizon ---
        "log_d7": math.log1p(d7),
        "log_d28": math.log1p(d28),
        "velocity": d7 / 7.0,
        "mom_7v7": d7 / max(prev7, 1),
        "mom_7v28": d7 / max(d28 / 4.0, 1),
        "trend_slope_28": _norm_slope(daily28),
        "volatility_28": float(daily28.std() / mean28) if mean28 > 0 else 0.0,
        # --- long-horizon ---
        "log_d56": math.log1p(d56),
        "log_d84": math.log1p(d84),
        "mom_28v28": d28 / max(prev28, 1),
        "mom_56v56": d56 / max(prev56, 1),
        "mom_28v56": d28 / max(d56 / 2.0, 1),
        "trend_slope_56": _norm_slope(daily56),
        "trend_slope_84": _norm_slope(daily84),
        # --- candidates (computed always; activated only via active_features.json) ---
        "recent_share": d7 / max(d28, 1),
        "trend_slope_7": _norm_slope(daily7),
        "dow_volatility_7": float(daily7.std() / mean7) if mean7 > 0 else 0.0,
    }


def _smooth(series: dict[date, float], k: int) -> dict[date, float]:
    """Causal (trailing) k-day moving average: smoothed[d] uses only days <= d.

    A centered MA would let a feature at as-of date t peek up to k//2 days into the future,
    which inflated the held-out R^2 by ~0.12 (see pipeline/scripts/validate_growth.py and
    docs/VALIDATION.md). Trailing smoothing is leak-free for forecasting.
    """
    if k <= 1:
        return series
    out: dict[date, float] = {}
    for d in series:
        vals = [series[d - timedelta(days=o)] for o in range(0, k)
                if (d - timedelta(days=o)) in series]
        out[d] = sum(vals) / len(vals) if vals else series[d]
    return out


def _series_by_package(history: pd.DataFrame, smooth: int = SMOOTH_WINDOW) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    for name, grp in history.groupby("name"):
        s: dict[date, float] = {}
        for d, dl in zip(grp["date"], grp["downloads"], strict=False):
            dd = d if isinstance(d, date) else pd.to_datetime(d).date()
            s[dd] = float(dl)
        out[name] = _smooth(s, smooth)
    return out


def build_growth_training(
    history: pd.DataFrame, horizon: int = GROWTH_HORIZON, stride: int = 3, min_history: int = 84
) -> pd.DataFrame:
    """Slide an as-of window across each package's series to make supervised rows.

    Target is log-growth over ``horizon`` days: log(downloads[t+H..t+2H]) - log(downloads[t..t+H]).
    """
    rows = []
    for name, series in _series_by_package(history).items():
        if not series:
            continue
        days = sorted(series)
        start, end = days[0], days[-1]
        cursor = start + timedelta(days=min_history)
        last_label_day = end - timedelta(days=horizon)
        while cursor <= last_label_day:
            feats = _download_features(series, cursor)
            if feats:
                this_w = _window_sum(series, cursor, horizon)
                future_w = _window_sum(series, cursor + timedelta(days=horizon), horizon)
                if this_w > 0:
                    feats["name"] = name
                    feats["feature_date"] = cursor
                    feats["growth_target_7d"] = math.log1p(future_w) - math.log1p(this_w)
                    rows.append(feats)
            cursor += timedelta(days=stride)
    return pd.DataFrame(rows)


def build_growth_scoring(history: pd.DataFrame) -> pd.DataFrame:
    """One row per package: download-dynamics features as of the latest available date."""
    rows = []
    for name, series in _series_by_package(history).items():
        if not series:
            continue
        asof = max(series)
        feats = _download_features(series, asof)
        if feats:
            feats["name"] = name
            feats["feature_date"] = asof
            rows.append(feats)
    return pd.DataFrame(rows)


def _num(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _at_risk_label(r: pd.Series) -> int:
    sev = r.get("max_severity")
    sev = sev if isinstance(sev, str) else ""
    recent_high_vuln = (_num(r.get("vuln_new_28d")) or 0) > 0 and sev in ("HIGH", "CRITICAL")
    abandoned = (r.get("archived") is True) or (isinstance(r.get("status"), str) and bool(r.get("status")))
    stale = (_num(r.get("days_since_last_release")) or 0) > 365
    bf = _num(r.get("bus_factor"))
    dep = _num(r.get("dependent_repos_count")) or 0
    key_person = bf is not None and bf < 0.1 and dep > 1000
    return int(bool(recent_high_vuln or abandoned or stale or key_person))


def build_risk_frame(snapshots_latest: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional risk features + transparent at_risk label from the latest snapshots."""
    df = snapshots_latest.copy()

    def series(col):  # always a Series aligned to df, even if the column is absent
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce")
        return pd.Series([np.nan] * len(df), index=df.index)

    def logp(col):
        return np.log1p(series(col).fillna(0).clip(lower=0))

    out = pd.DataFrame(
        {
            "name": df["name"],
            "category": df["category"],
            "log_stars": logp("stars"),
            "log_forks": logp("forks"),
            "log_open_issues": logp("open_issues"),
            "log_dependent_repos": logp("dependent_repos_count"),
            "log_dependent_packages": logp("dependent_packages_count"),
            "bus_factor": series("bus_factor"),
            "release_cadence_days": series("release_cadence_days"),
            "dependency_count": series("dependency_count"),
            "scorecard_overall": series("scorecard_overall"),
            "commit_count_4w": series("commit_count_4w"),
            "prs_merged_7d": series("prs_merged_7d"),
            "issues_opened_7d": series("issues_opened_7d"),
            "rank_average": series("rank_average"),
        }
    )
    out["at_risk_label"] = df.apply(_at_risk_label, axis=1).values
    return out

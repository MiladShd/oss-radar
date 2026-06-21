"""Feature construction.

Growth model (supervised regression): features are pure download-dynamics computed from
the daily series so they are identical in distribution between historical training rows and
the latest scoring row. The label is next-7-day relative growth of weekly downloads. The
180-day backfill yields thousands of (package, as-of-date) training rows on the very first run.

Risk model (cross-sectional): maintenance / popularity / security features from the latest
snapshot, with a transparent ``at_risk_label`` (documented in docs/METHODOLOGY.md).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

DOWNLOAD_FEATURES = [
    "log_d7",
    "log_d28",
    "velocity",
    "mom_7v7",
    "mom_7v28",
    "trend_slope_28",
    "volatility_28",
]

# Candidate features are computed every run but only enter the model when the
# self-improvement agent measures a lift and opens a PR enabling them (active_features.json).
CANDIDATE_DOWNLOAD_FEATURES = [
    "mom_28v28",
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


def _window_sum(series: dict[date, int], end: date, days: int, offset: int = 0) -> int:
    last = end - timedelta(days=offset)
    first = last - timedelta(days=days - 1)
    return sum(v for d, v in series.items() if first <= d <= last)


def _daily_window(series: dict[date, int], end: date, days: int) -> list[int]:
    return [series.get(end - timedelta(days=k), 0) for k in range(days - 1, -1, -1)]


def _norm_slope(daily: np.ndarray) -> float:
    mean = daily.mean() if daily.size else 0.0
    if mean <= 0 or daily.size < 2:
        return 0.0
    return float(np.polyfit(np.arange(daily.size), daily, 1)[0] / mean)


def _download_features(series: dict[date, int], asof: date) -> dict | None:
    d7 = _window_sum(series, asof, 7)
    if d7 <= 0:
        return None
    d28 = _window_sum(series, asof, 28)
    d28_prev = _window_sum(series, asof, 28, offset=28)
    prev7 = _window_sum(series, asof, 7, offset=7)
    daily28 = np.array(_daily_window(series, asof, 28), dtype=float)
    daily7 = np.array(_daily_window(series, asof, 7), dtype=float)
    mean28 = daily28.mean() if daily28.size else 0.0
    mean7 = daily7.mean() if daily7.size else 0.0
    volatility = daily28.std() / mean28 if mean28 > 0 else 0.0
    return {
        # --- active (base) features ---
        "log_d7": math.log1p(d7),
        "log_d28": math.log1p(d28),
        "velocity": d7 / 7.0,
        "mom_7v7": d7 / max(prev7, 1),
        "mom_7v28": d7 / max(d28 / 4.0, 1),
        "trend_slope_28": _norm_slope(daily28),
        "volatility_28": float(volatility),
        # --- candidate features (computed always; activated only via active_features.json) ---
        "mom_28v28": d28 / max(d28_prev, 1),
        "recent_share": d7 / max(d28, 1),
        "trend_slope_7": _norm_slope(daily7),
        "dow_volatility_7": float(daily7.std() / mean7) if mean7 > 0 else 0.0,
    }


# Growth target/feature config. Forecasting 14-day momentum on a 7-day-smoothed series is
# substantially more predictable than raw 7-day growth (R^2 ~0 -> ~0.07, Spearman 0.21 -> 0.37
# on the held-out backtest), so the growth model uses these.
SMOOTH_WINDOW = 7
GROWTH_HORIZON = 14


def _smooth(series: dict[date, float], k: int) -> dict[date, float]:
    if k <= 1:
        return series
    half = k // 2
    out: dict[date, float] = {}
    for d in series:
        vals = [series[d + timedelta(days=o)] for o in range(-half, half + 1)
                if (d + timedelta(days=o)) in series]
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
    history: pd.DataFrame, horizon: int = GROWTH_HORIZON, stride: int = 3, min_history: int = 28
) -> pd.DataFrame:
    """Slide an as-of window across each package's series to make supervised rows."""
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
                feats["name"] = name
                feats["feature_date"] = cursor
                feats["growth_target_7d"] = future_w / max(this_w, 1) - 1.0
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

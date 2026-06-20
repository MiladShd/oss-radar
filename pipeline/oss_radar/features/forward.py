"""Forward-outcome relabeling for the risk model.

On day one there is no history, so the risk model trains on a transparent heuristic label
(see engineering._at_risk_label). As the daily ``snapshots`` table accumulates, this module
relabels each package by what *actually happened* between an early snapshot and a later one
— a new vulnerability appeared, the repo was archived, downloads collapsed, releases went
stale — and trains on those realized outcomes instead. The pipeline switches automatically
once enough history spans the horizon; until then it falls back to the heuristic.
"""

from __future__ import annotations

import pandas as pd

from oss_radar.features.engineering import _num, build_risk_frame


def _escalated(t0: pd.Series, tN: pd.Series) -> int:
    v0, vN = _num(t0.get("vuln_count")) or 0, _num(tN.get("vuln_count")) or 0
    newly_archived = (tN.get("archived") is True) and (t0.get("archived") is not True)
    newly_removed = isinstance(tN.get("status"), str) and bool(tN.get("status")) and not (
        isinstance(t0.get("status"), str) and bool(t0.get("status"))
    )
    dl0, dlN = _num(t0.get("downloads_7d")), _num(tN.get("downloads_7d"))
    downloads_collapsed = bool(dl0 and dlN is not None and dlN < 0.7 * dl0)
    rel0 = _num(t0.get("days_since_last_release")) or 0
    relN = _num(tN.get("days_since_last_release")) or 0
    went_stale = relN > 365 >= rel0
    return int(bool(vN > v0 or newly_archived or newly_removed or downloads_collapsed or went_stale))


def build_forward_risk_labels(snapshot_history: pd.DataFrame, horizon_days: int = 14) -> pd.DataFrame:
    """Realized-outcome risk training rows; empty if history doesn't yet span the horizon."""
    if snapshot_history.empty or "snapshot_date" not in snapshot_history:
        return pd.DataFrame()
    df = snapshot_history.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce")
    df = df.dropna(subset=["snapshot_date"])

    anchors, labels = [], []
    for _name, g in df.groupby("name"):
        g = g.sort_values("snapshot_date")
        t0, tN = g.iloc[0], g.iloc[-1]
        span = (tN["snapshot_date"] - t0["snapshot_date"]).days
        if span < horizon_days:
            continue
        anchors.append(t0)
        labels.append(_escalated(t0, tN))

    if not anchors:
        return pd.DataFrame()
    frame = build_risk_frame(pd.DataFrame(anchors))
    frame["at_risk_label"] = labels
    return frame


def choose_risk_training(
    heuristic_frame: pd.DataFrame, snapshot_history: pd.DataFrame,
    horizon_days: int = 14, min_rows: int = 25,
) -> tuple[pd.DataFrame, str]:
    """Use realized-outcome labels once there are enough of them; else the heuristic."""
    forward = build_forward_risk_labels(snapshot_history, horizon_days)
    if not forward.empty and len(forward) >= min_rows and forward["at_risk_label"].nunique() > 1:
        return forward, "forward-outcome"
    return heuristic_frame, "heuristic"

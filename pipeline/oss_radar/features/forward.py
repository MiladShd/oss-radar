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
from oss_radar.models.evaluation import risk_forward_label_version


def _escalated(t0: pd.Series, tN: pd.Series) -> int:
    def flag(value) -> bool:
        return bool(value) if value is not None and not pd.isna(value) else False

    v0, vN = _num(t0.get("vuln_count")) or 0, _num(tN.get("vuln_count")) or 0
    newly_archived = flag(tN.get("archived")) and not flag(t0.get("archived"))
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
    """Build one realized-outcome row per eligible package/date anchor.

    Each anchor is paired only with a snapshot on the exact fixed outcome date. This turns daily
    snapshot accumulation into additional supervised examples while avoiding both the moving-label
    bug caused by comparing first/latest and a variable-horizon bug when collection days are
    missing.
    """
    if snapshot_history.empty or "snapshot_date" not in snapshot_history:
        return pd.DataFrame()
    df = snapshot_history.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce")
    df = df.dropna(subset=["snapshot_date"])

    # A retried run can create more than one snapshot for a package/day.  Prefer the most recently
    # ingested value, then collapse to the natural package-day key before creating labels.
    order = ["name", "snapshot_date"]
    if "ingested_at" in df:
        df["ingested_at"] = pd.to_datetime(df["ingested_at"], errors="coerce")
        order.append("ingested_at")
    df = (df.sort_values(order)
          .drop_duplicates(subset=["name", "snapshot_date"], keep="last"))

    anchors, labels = [], []
    for _name, g in df.groupby("name"):
        g = g.sort_values("snapshot_date").reset_index(drop=True)
        by_date = {value.normalize(): idx for idx, value in enumerate(g["snapshot_date"])}
        for _anchor_idx, t0 in g.iterrows():
            target_date = (t0["snapshot_date"] + pd.Timedelta(days=horizon_days)).normalize()
            outcome_idx = by_date.get(target_date)
            if outcome_idx is None:
                continue
            tN = g.iloc[outcome_idx]
            anchors.append(t0)
            labels.append(_escalated(t0, tN))

    if not anchors:
        return pd.DataFrame()
    frame = build_risk_frame(pd.DataFrame(anchors))
    frame["feature_date"] = [row["snapshot_date"].date() for row in anchors]
    frame["outcome_date"] = [
        (row["snapshot_date"] + pd.Timedelta(days=horizon_days)).date() for row in anchors
    ]
    frame["label_horizon_days"] = horizon_days
    frame["label_version"] = risk_forward_label_version(horizon_days)
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

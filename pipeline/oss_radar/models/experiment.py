"""Offline feature experiments for the self-improvement agent.

Trains the growth model with the active feature set vs. active+candidate on the *same*
held-out split, and reports the Spearman lift. This is what lets the system measure — not
guess — whether a new feature is worth proposing.
"""

from __future__ import annotations

import pandas as pd

from oss_radar.models.growth import GrowthModel


def _spearman(train_df: pd.DataFrame, features: list[str], seed: int) -> float:
    if any(f not in train_df.columns for f in features):
        return float("nan")
    m = GrowthModel(features=list(features), seed=seed)
    metrics = m.fit(train_df)
    return float(metrics.get("spearman", float("nan")))


def evaluate_candidates(
    train_df: pd.DataFrame, active: list[str], candidates: list[str], seed: int = 42,
) -> list[dict]:
    """Return [{candidate, base, new, delta}], best lift first."""
    base = _spearman(train_df, active, seed)
    out = []
    for c in candidates:
        if c in active or c not in train_df.columns:
            continue
        new = _spearman(train_df, active + [c], seed)
        delta = (new - base) if (new == new and base == base) else float("nan")
        out.append({"candidate": c, "base": round(base, 4), "new": round(new, 4),
                    "delta": round(delta, 4) if delta == delta else None})
    out.sort(key=lambda r: (r["delta"] is not None, r["delta"] or -1), reverse=True)
    return out


def best_candidate(results: list[dict], margin: float) -> dict | None:
    for r in results:
        if r["delta"] is not None and r["delta"] >= margin:
            return r
    return None

"""Nested, development-only feature experiments for the self-improvement agent.

Candidate selection must never inspect the outer test dates used by model governance. The
experiment first removes that outer test partition, then compares active vs. active+candidate
inside a fresh date-grouped split of the remaining development dates.
"""

from __future__ import annotations

import pandas as pd

from oss_radar.features import CANDIDATE_DOWNLOAD_FEATURES
from oss_radar.models.evaluation import date_grouped_train_validation_test
from oss_radar.models.growth import GrowthModel


def _development_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return outer train+validation dates, leaving the operational test dates untouched."""
    outer = date_grouped_train_validation_test(frame)
    return pd.concat([outer.train, outer.validation], ignore_index=True)


def _selection_spearman(development_df: pd.DataFrame, features: list[str], seed: int) -> float:
    if any(feature not in development_df.columns for feature in features):
        return float("nan")
    model = GrowthModel(features=list(features), seed=seed)
    # GrowthModel creates another chronological 70/15/15 split here. Its test partition is an
    # inner selection cohort, not the outer governance test removed by _development_frame.
    metrics = model.fit(development_df)
    return float(metrics.get("spearman", float("nan")))


def evaluate_candidates(
    train_df: pd.DataFrame, active: list[str], candidates: list[str], seed: int = 42,
) -> list[dict]:
    """Return nested-selection lifts, best first, without reading the outer test cohort."""
    try:
        development = _development_frame(train_df)
    except ValueError:
        return []
    base = _selection_spearman(development, active, seed)
    out = []
    for candidate in candidates:
        if candidate in active or candidate not in development.columns:
            continue
        new = _selection_spearman(development, active + [candidate], seed)
        delta = (new - base) if (new == new and base == base) else float("nan")
        out.append({
            "candidate": candidate,
            "base": round(base, 4),
            "new": round(new, 4),
            "delta": round(delta, 4) if delta == delta else None,
            "selection": "nested-development-only",
            "selection_rows": len(development),
        })
    out.sort(key=lambda r: (r["delta"] is not None, r["delta"] or -1), reverse=True)
    return out


def best_candidate(results: list[dict], margin: float) -> dict | None:
    for r in results:
        if r["delta"] is not None and r["delta"] >= margin:
            return r
    return None


def validate_feature_candidate(
    train_df: pd.DataFrame,
    base_config: dict,
    head_config: dict,
    *,
    margin: float,
    seed: int = 42,
) -> dict | None:
    """Independently reproduce an exact one-feature config change.

    Ordinary PRs return ``None``. A feature-changing PR must append one allowlisted candidate,
    leave the risk set untouched, and reproduce the configured nested-selection lift.
    """
    if base_config == head_config:
        return None
    if base_config.get("risk") != head_config.get("risk"):
        raise ValueError("feature PR changed the risk feature set")

    base = list(base_config.get("download") or [])
    head = list(head_config.get("download") or [])
    if len(head) != len(base) + 1 or head[:len(base)] != base:
        raise ValueError("feature PR must append exactly one download feature")
    candidate = head[-1]
    if candidate not in CANDIDATE_DOWNLOAD_FEATURES:
        raise ValueError(f"feature is not in the allowlisted candidate catalog: {candidate}")
    if len(train_df) < 200:
        raise ValueError(
            f"only {len(train_df)} training rows; refusing to skip independent feature validation"
        )

    results = evaluate_candidates(train_df, base, [candidate], seed=seed)
    if len(results) != 1 or results[0]["delta"] is None:
        raise ValueError(f"feature lift could not be evaluated: {candidate}")
    result = results[0]
    if result["delta"] < margin:
        raise ValueError(
            f"{candidate} reproduced delta {result['delta']:+.4f}, below +{margin:.4f}"
        )
    return result

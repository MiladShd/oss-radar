"""Shared evaluation contracts for reproducible, cohort-aware model comparisons."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

import pandas as pd

from oss_radar.features import ALL_DOWNLOAD_FEATURES, GROWTH_TARGET_COLUMN, RISK_FEATURES

EVALUATION_SCHEMA_VERSION = "cohort-eval-v1"
GROWTH_LABEL_VERSION = "growth-log70d-trailing28-v1"
RISK_FORWARD_LABEL_VERSION = "risk-escalation-exact14d-v1"
RISK_HEURISTIC_LABEL_VERSION = "risk-heuristic-cross-sectional-v1"
TEMPORAL_SPLIT_VERSION = "date-grouped-70-15-15-v1"
RISK_HOLDOUT_VERSION = "stable-package-sha256-mod5-v1"


@dataclass(frozen=True)
class TemporalSplit:
    """A chronological split whose forecast-origin dates never cross partitions."""

    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame
    metadata: dict[str, Any]


def date_grouped_train_validation_test(
    df: pd.DataFrame,
    *,
    date_column: str = "feature_date",
    validation_fraction: float = 0.15,
    test_fraction: float = 0.15,
) -> TemporalSplit:
    """Split chronologically by distinct dates, never by individual rows.

    Row-wise splitting lets packages from one forecast origin leak across train/evaluation
    partitions whenever package counts differ or rows are reordered. Partitioning the sorted
    origin dates first makes that impossible.
    """
    if date_column not in df.columns:
        raise ValueError(f"missing evaluation date column: {date_column}")
    if not 0 < validation_fraction < 1 or not 0 < test_fraction < 1:
        raise ValueError("validation/test fractions must be between zero and one")
    if validation_fraction + test_fraction >= 1:
        raise ValueError("validation/test fractions leave no training partition")

    work = df.copy()
    date_key = pd.to_datetime(work[date_column], errors="coerce").dt.normalize()
    if date_key.isna().any():
        raise ValueError(f"{date_column} contains invalid dates")
    work["_evaluation_date"] = date_key
    dates = list(pd.Series(date_key.unique()).sort_values())
    if len(dates) < 3:
        raise ValueError("at least three distinct feature dates are required")

    n_dates = len(dates)
    n_test = max(1, int(round(n_dates * test_fraction)))
    n_validation = max(1, int(round(n_dates * validation_fraction)))
    while n_dates - n_validation - n_test < 1:
        if n_test >= n_validation and n_test > 1:
            n_test -= 1
        elif n_validation > 1:
            n_validation -= 1
        else:
            raise ValueError("not enough distinct feature dates for three partitions")

    train_dates = set(dates[: n_dates - n_validation - n_test])
    validation_dates = set(dates[n_dates - n_validation - n_test : n_dates - n_test])
    test_dates = set(dates[n_dates - n_test :])

    def partition(selected: set[pd.Timestamp]) -> pd.DataFrame:
        return work.loc[work["_evaluation_date"].isin(selected)].drop(
            columns=["_evaluation_date"]
        )

    train = partition(train_dates)
    validation = partition(validation_dates)
    test = partition(test_dates)
    if train.empty or validation.empty or test.empty:
        raise ValueError("date-grouped split produced an empty partition")

    def span(selected: set[pd.Timestamp]) -> dict[str, Any]:
        ordered = sorted(selected)
        return {
            "start": ordered[0].date().isoformat(),
            "end": ordered[-1].date().isoformat(),
            "n_dates": len(ordered),
        }

    metadata = {
        "split_version": TEMPORAL_SPLIT_VERSION,
        "date_column": date_column,
        "train": {**span(train_dates), "n_rows": len(train)},
        "validation": {**span(validation_dates), "n_rows": len(validation)},
        "test": {**span(test_dates), "n_rows": len(test)},
    }
    return TemporalSplit(train=train, validation=validation, test=test, metadata=metadata)


def stable_frame_hash(df: pd.DataFrame, columns: list[str]) -> str:
    """Return a deterministic SHA-256 over a sorted, JSON-normalized frame."""
    available = [column for column in columns if column in df.columns]
    if not available:
        return hashlib.sha256(b"[]").hexdigest()
    # Mapping ``None`` into a numeric pandas Series coerces it straight back to NaN. Build plain
    # Python records after sorting so every NaN/NaT/infinity is normalized before strict JSON.
    normalized = df[available].sort_values(
        available, kind="mergesort", na_position="first"
    )
    records = [
        {column: _json_scalar(row[column]) for column in available}
        for row in normalized.to_dict("records")
    ]
    payload = json.dumps(records, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def feature_set_hash(features: list[str]) -> str:
    payload = json.dumps(sorted(features), separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def growth_evaluation_provenance(
    full_frame: pd.DataFrame,
    split: TemporalSplit,
    features: list[str],
    horizon_days: int = 70,
) -> dict[str, Any]:
    """Describe the exact closed test cohort used for a growth metric."""
    cohort_columns = ["name", "feature_date", GROWTH_TARGET_COLUMN]
    benchmark_columns = cohort_columns + [
        feature for feature in ALL_DOWNLOAD_FEATURES if feature in full_frame.columns
    ]
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "label_version": growth_label_version(horizon_days),
        "label_horizon_days": horizon_days,
        "feature_set_hash": feature_set_hash(features),
        "dataset_hash": stable_frame_hash(full_frame, benchmark_columns),
        "benchmark_hash": stable_frame_hash(split.test, benchmark_columns),
        "benchmark_kind": "closed-date-grouped-origin-test-cohort",
        # Origin dates are grouped and ordered, but the current 180-day source depth cannot
        # support a 70-day embargo between all three partitions. Package-disjoint evidence is
        # tracked separately by the growth gate; this field prevents temporal overclaiming.
        "embargo_days": 0,
        "independent_temporal_evidence": False,
        "outcome_windows_overlap_partitions": True,
        **split.metadata,
    }


def growth_label_version(horizon_days: int) -> str:
    return f"growth-log{int(horizon_days)}d-trailing28-v1"


def risk_forward_label_version(horizon_days: int) -> str:
    return f"risk-escalation-exact{int(horizon_days)}d-v1"


def risk_evaluation_provenance(
    frame: pd.DataFrame,
    features: list[str],
    *,
    label_version: str | None = None,
    benchmark_frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Describe the stable package-disjoint holdout used for risk promotion."""
    version = label_version or _risk_label_version(frame)
    benchmark = benchmark_frame if benchmark_frame is not None else frame
    cohort_columns = ["name", "feature_date", "outcome_date", "at_risk_label"]
    benchmark_columns = cohort_columns + [
        feature for feature in RISK_FEATURES if feature in frame.columns
    ]
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "label_version": version,
        "feature_set_hash": feature_set_hash(features),
        "dataset_hash": stable_frame_hash(frame, benchmark_columns),
        "benchmark_hash": stable_frame_hash(benchmark, benchmark_columns),
        "benchmark_kind": "stable-package-disjoint-holdout",
        "split_version": RISK_HOLDOUT_VERSION,
        "n_rows": len(frame),
        "n_packages": int(frame["name"].nunique()) if "name" in frame else 0,
        "benchmark_rows": len(benchmark),
        "benchmark_packages": (
            int(benchmark["name"].nunique()) if "name" in benchmark else 0
        ),
        "start": _date_bound(frame, "feature_date", "min"),
        "end": _date_bound(frame, "feature_date", "max"),
    }


def risk_holdout_mask(frame: pd.DataFrame) -> pd.Series:
    """Reserve a stable ~20% package set that no risk champion trains on."""
    if "name" not in frame:
        raise ValueError("risk evaluation requires package names")

    def reserved(name: Any) -> bool:
        digest = hashlib.sha256(str(name).encode()).digest()
        return int.from_bytes(digest[:8], "big") % 5 == 0

    return frame["name"].map(reserved).astype(bool)


def _risk_label_version(frame: pd.DataFrame) -> str:
    if "label_version" in frame:
        versions = sorted(str(v) for v in frame["label_version"].dropna().unique())
        if len(versions) == 1:
            return versions[0]
    return RISK_HEURISTIC_LABEL_VERSION


def _date_bound(frame: pd.DataFrame, column: str, operation: str) -> str | None:
    if column not in frame or frame.empty:
        return None
    dates = pd.to_datetime(frame[column], errors="coerce").dropna()
    if dates.empty:
        return None
    value = dates.min() if operation == "min" else dates.max()
    return value.date().isoformat()


def _json_scalar(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return float(f"{value:.12g}")
    return value

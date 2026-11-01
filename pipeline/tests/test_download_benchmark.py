"""Protect availability timing and calendar integrity in the offline experiment."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from oss_radar.research.download_benchmark import (
    audit_history,
    features,
    make_rows,
    seasonal_naive,
    training_at,
)

PROTOCOL = json.loads((Path(__file__).parents[1]/"research/download_protocol_v1.json").read_text())


def history(n=180):
    return pd.DataFrame({"name": "demo", "date": pd.date_range("2026-01-01", periods=n),
                         "downloads": np.arange(n)+1})


def test_rejects_gaps_duplicates_and_negative_counts():
    data = history()
    assert audit_history(data)["rows"] == 180
    for bad in [data.drop(index=50), pd.concat([data, data.iloc[:1]])]:
        with pytest.raises(ValueError):
            audit_history(bad)
    data.loc[10, "downloads"] = -1
    with pytest.raises(ValueError):
        audit_history(data)


def test_target_starts_after_issuance_not_after_last_available_observation():
    data = history()
    rows, _ = make_rows(data, PROTOCOL)
    row = rows.iloc[0]
    assert row.cutoff == data.date.iloc[83]
    assert row.outcome_end == data.date.iloc[112]
    # Index 84 is the unobserved issuance day; it is not a scored outcome.
    assert row.actual_downloads == data.downloads.iloc[85:113].sum()
    assert row.reference == data.downloads.iloc[56:84].sum()


def test_future_outcomes_cannot_change_training_or_origin_features():
    data = history()
    cutoff = data.date.iloc[140]
    rows, _ = make_rows(data, PROTOCOL)
    changed = data.copy()
    changed.loc[changed.date > cutoff, "downloads"] *= 10
    revised, _ = make_rows(changed, PROTOCOL)
    original_train = training_at(rows, cutoff).reset_index(drop=True)
    assert not original_train.empty
    pd.testing.assert_frame_equal(original_train, training_at(revised, cutoff).reset_index(drop=True))
    pd.testing.assert_series_equal(rows.loc[rows.cutoff == cutoff, PROTOCOL["feature_set"]].iloc[0],
                                   revised.loc[revised.cutoff == cutoff, PROTOCOL["feature_set"]].iloc[0])
    assert original_train.outcome_end.max() <= cutoff


def test_seasonal_naive_preserves_weekday_alignment_across_bridge_day():
    x = np.tile(np.arange(1, 8), 12)
    forecast = seasonal_naive(x, 29)
    np.testing.assert_array_equal(forecast[:8], [1, 2, 3, 4, 5, 6, 7, 1])
    assert len(forecast[1:]) == 28
    assert sum(forecast[1:]) == 4*28
    assert set(features(x, pd.Timestamp("2026-03-25"))) == set(PROTOCOL["feature_set"])


def test_development_outcomes_complete_before_final_selection_and_final_windows_disjoint():
    end = max(pd.Timestamp(d)+pd.Timedelta(days=29) for d in PROTOCOL["development_cutoffs"])
    final = [pd.Timestamp(d) for d in PROTOCOL["final_cutoffs"]]
    assert end <= min(final)
    assert final[0]+pd.Timedelta(days=29) < final[1]+pd.Timedelta(days=2)

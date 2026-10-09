"""Drift-aware conformal intervals: statistical contracts, drift behaviour, and model integration."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from oss_radar.features import DOWNLOAD_FEATURES, GROWTH_TARGET_COLUMN
from oss_radar.models.conformal import (
    DriftAwareConformal,
    forward_chain,
    recency_weights,
    weighted_conformal_quantile,
)
from oss_radar.models.growth import GrowthModel


def test_unweighted_quantile_matches_split_conformal_rank():
    # Classic split conformal: with n scores the level-(1-a) quantile is the ceil((n+1)(1-a))-th smallest.
    scores = np.arange(1.0, 100.0)  # n = 99
    q = weighted_conformal_quantile(scores, np.ones(99), 0.9)
    assert q == scores[math.ceil(100 * 0.9) - 1]  # 90th smallest


def test_quantile_is_infinite_when_calibration_too_small_for_level():
    assert math.isinf(weighted_conformal_quantile(np.array([1.0, 2.0]), np.ones(2), 0.9))
    assert math.isinf(weighted_conformal_quantile(np.array([]), np.array([]), 0.8))


def test_recency_weights_halve_every_half_life():
    w = recency_weights(np.array([4, 2, 0]), newest=4, half_life=2.0)
    assert np.allclose(w, [1.0, 0.5, 0.25])


def _iid_residuals(n_dates: int = 40, per_date: int = 50, seed: int = 0):
    rng = np.random.default_rng(seed)
    date_index = np.repeat(np.arange(n_dates), per_date)
    return np.abs(rng.normal(0, 1.0, n_dates * per_date)), date_index


def test_coverage_is_near_nominal_without_drift():
    r, d = _iid_residuals()
    result = forward_chain(r, d, alpha=0.2, half_life=1e9, gamma=0.0)
    assert 0.77 <= result.coverage <= 0.86


def test_drift_breaks_plain_conformal_and_drift_awareness_repairs_it():
    # Error scale grows 3% per origin date, mirroring the production residual drift.
    rng = np.random.default_rng(1)
    n_dates, per_date = 40, 60
    d = np.repeat(np.arange(n_dates), per_date)
    r = np.abs(rng.normal(0, 1.0, d.size)) * (1.03**d)
    plain = forward_chain(r, d, alpha=0.2, half_life=1e9, gamma=0.0)
    adaptive = forward_chain(r, d, alpha=0.2, half_life=2.0, gamma=0.2)
    assert plain.coverage < 0.76  # under-covers (nominal 0.80)
    assert adaptive.coverage > plain.coverage + 0.03
    assert abs(adaptive.coverage - 0.8) < abs(plain.coverage - 0.8)


def test_aci_widens_after_misses_and_narrows_after_over_coverage():
    r, d = _iid_residuals(n_dates=20)
    wide = forward_chain(r * 0.01, d, alpha=0.2, half_life=2.0, gamma=0.2)  # tiny residuals, always hit
    assert wide.final_alpha >= 0.2  # alpha_t drifts up (narrower) when coverage is too high


def test_calibrator_roundtrip_and_interval_symmetry():
    r, d = _iid_residuals(n_dates=12)
    cal, evidence = DriftAwareConformal.calibrate(r, d, alpha=0.2)
    clone = DriftAwareConformal.from_dict(cal.to_dict())
    assert clone.half_width() == cal.half_width() > 0
    lo, hi = clone.interval(np.array([0.0, 1.0]))
    assert np.allclose(hi - lo, 2 * cal.half_width())
    assert evidence.n_dates_scored == 9


def _growth_frame(n_dates: int = 40, packages: int = 12, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=n_dates).date
    rows = []
    for k, feature_date in enumerate(dates):
        for package in range(packages):
            row = {f: float(rng.normal()) for f in DOWNLOAD_FEATURES}
            row.update({
                "name": f"pkg{package}",
                "feature_date": feature_date,
                GROWTH_TARGET_COLUMN: 0.4 * row["mom_7v7"] + rng.normal(0, 0.05 * (1 + k / 10)),
            })
            rows.append(row)
    return pd.DataFrame(rows)


def test_growth_model_produces_ordered_intervals_and_persists_them(tmp_path):
    frame = _growth_frame()
    model = GrowthModel(seed=3)
    metrics = model.fit(frame)
    for tag in (80, 90):
        assert 0.0 <= metrics[f"conformal_coverage_{tag}"] <= 1.0
        assert metrics[f"conformal_width_{tag}"] > 0
    lo, hi = model.predict_interval(frame.tail(20), level=0.8)
    wide_lo, wide_hi = model.predict_interval(frame.tail(20), level=0.9)
    assert np.all(lo < hi)
    assert np.all(wide_hi - wide_lo >= hi - lo)  # 90% interval is never narrower than 80%

    path = str(tmp_path / "growth.joblib")
    model.save(path)
    reloaded = GrowthModel.load(path)
    r_lo, r_hi = reloaded.predict_interval(frame.tail(20), level=0.8)
    assert np.allclose(lo, r_lo) and np.allclose(hi, r_hi)


def test_legacy_artifact_without_conformal_state_degrades_to_no_interval():
    model = GrowthModel(seed=3)
    model.fit(_growth_frame())
    model.conformal = {}
    assert model.predict_interval(_growth_frame().tail(5)) is None

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
    """Controlled hit/miss streams: the working alpha must move in the stated direction."""
    n_dates, per = 14, 30
    d = np.repeat(np.arange(n_dates), per)
    growing = np.exp(0.4 * d)    # each date is larger than all history -> every scored date misses
    shrinking = np.exp(-0.4 * d)  # each date is smaller than all history -> every scored date is covered
    miss = forward_chain(growing, d, alpha=0.2, half_life=1e9, gamma=0.2)
    assert miss.coverage == 0.0
    assert miss.final_alpha < 0.2  # persistent misses => lower alpha => wider next interval
    cover = forward_chain(shrinking, d, alpha=0.2, half_life=1e9, gamma=0.2)
    assert cover.coverage == 1.0
    assert cover.final_alpha > 0.2  # persistent over-coverage => higher alpha => narrower interval
    # With adaptation switched off alpha cannot move at all (this is what the old test could not tell)
    frozen = forward_chain(growing, d, alpha=0.2, half_life=1e9, gamma=0.0)
    assert frozen.final_alpha == 0.2


def test_unsupported_level_is_unavailable_not_silently_widened():
    # 2 calibration points cannot support a 90% level; the quantile is infinite and must stay so.
    assert math.isinf(weighted_conformal_quantile(np.array([1.0, 2.0]), np.ones(2), 0.9))
    cal = DriftAwareConformal(alpha=0.1, alpha_t=0.1, residuals=[1.0, 2.0], date_index=[0, 1])
    assert math.isinf(cal.half_width())


def test_validation_labels_cannot_influence_earlier_calibration_residuals():
    """Regression for the early-stopping leak: changing the LAST validation date's labels must not move
    residuals on earlier validation dates (the model's stopping rule may not see those labels)."""
    from oss_radar.models.evaluation import date_grouped_train_validation_test

    frame = _growth_frame(n_dates=40, packages=20)
    split = date_grouped_train_validation_test(frame)
    train, val, test = split.train, split.validation.copy(), split.test
    base, idx = GrowthModel(seed=3).calibration_residuals(train, val, test)
    last_val_date = max(pd.to_datetime(val["feature_date"]))
    val.loc[pd.to_datetime(val["feature_date"]) == last_val_date, GROWTH_TARGET_COLUMN] += 5.0
    changed, idx2 = GrowthModel(seed=3).calibration_residuals(train, val, test)
    earlier = idx < idx[: len(val)].max()  # all pooled rows before the last validation date
    assert np.allclose(base[earlier], changed[earlier]), "earlier residuals moved: labels leaked"
    assert not np.allclose(base[~earlier & (idx == idx[: len(val)].max())],
                           changed[~earlier & (idx == idx[: len(val)].max())])


def test_ninety_percent_interval_is_never_narrower_than_eighty():
    model = GrowthModel(seed=3)
    frame = _growth_frame()
    model.fit(frame)
    # force the independent adaptive states to cross: make the 90% state look narrower than the 80% one
    model.conformal["0.9"]["residuals"] = [0.001] * len(model.conformal["0.9"]["residuals"])
    lo8, hi8 = model.predict_interval(frame.tail(10), level=0.8)
    lo9, hi9 = model.predict_interval(frame.tail(10), level=0.9)
    assert np.all((hi9 - lo9) >= (hi8 - lo8) - 1e-12)


def test_borrowing_requires_matching_features_and_records_provenance():
    frame = _growth_frame()
    challenger = GrowthModel(seed=3)
    challenger.fit(frame)
    different = GrowthModel(features=list(challenger.features)[:-1], seed=3)
    assert different.adopt_calibration(challenger) is False
    other_horizon = GrowthModel(seed=3, horizon_days=28)
    assert other_horizon.adopt_calibration(challenger) is False
    same = GrowthModel(seed=9)
    assert same.calibration_source == "own"
    assert same.adopt_calibration(challenger) is True
    assert same.calibration_source == "borrowed"


def test_calibrator_roundtrip_and_interval_symmetry():
    r, d = _iid_residuals(n_dates=12)
    cal, evidence = DriftAwareConformal.calibrate(r, d, alpha=0.2)
    clone = DriftAwareConformal.from_dict(cal.to_dict())
    assert clone.half_width() == cal.half_width() > 0
    lo, hi = clone.interval(np.array([0.0, 1.0]))
    assert np.allclose(hi - lo, 2 * cal.half_width())
    assert evidence.n_dates_scored == 9


def _growth_frame(n_dates: int = 40, packages: int = 40, seed: int = 5) -> pd.DataFrame:
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


def test_champion_without_calibration_borrows_the_challengers_but_not_vice_versa():
    frame = _growth_frame()
    challenger = GrowthModel(seed=3)
    challenger.fit(frame)
    champion = GrowthModel(seed=9)
    champion.fit(frame)
    champion.conformal = {}  # legacy artifact trained before calibration existed
    assert champion.predict_interval(frame.tail(5)) is None

    assert champion.adopt_calibration(challenger) is True
    lo, hi = champion.predict_interval(frame.tail(5), level=0.8)
    assert np.all(lo < hi)
    # the borrowed state is a copy: mutating it must not change the challenger
    champion.conformal["0.8"]["alpha_t"] = 0.49
    assert challenger.conformal["0.8"]["alpha_t"] != 0.49
    # already calibrated, self-adoption and uncalibrated sources are all no-ops
    assert champion.adopt_calibration(challenger) is False
    assert challenger.adopt_calibration(challenger) is False
    assert GrowthModel(seed=1).adopt_calibration(GrowthModel(seed=2)) is False

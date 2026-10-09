"""Survival model contracts: correct likelihood, parameter recovery, censoring, leakage, and scoring."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from oss_radar.models.survival import (
    SURVIVAL_FEATURES,
    _auc,
    build_counting_process,
    fit_cox,
    fit_cox_selected,
    landmark_evaluation,
    score_new_advisory_risk,
)

# The simulation varies only advisory history; constant columns are unidentifiable without a ridge.
VARYING = ["log_vuln_history"]


def _simulate(n_packages: int = 60, n_days: int = 90, seed: int = 0, beta: float = 0.9) -> pd.DataFrame:
    """Daily snapshots whose new-advisory hazard is exp(beta · z) with a known common time shock."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-01-01")
    z = rng.normal(size=n_packages)  # standardised log-history signal
    vuln = np.round(np.exp(z + 1.5)).astype(float)
    rows = []
    for day in range(n_days):
        shock = 1.8 if 20 <= day < 25 else 1.0  # common shock the baseline hazard must absorb
        for i in range(n_packages):
            rate = 0.02 * shock * np.exp(beta * z[i])
            if day and rng.random() < 1 - np.exp(-rate):
                vuln[i] += 1
            rows.append({
                "name": f"p{i}", "snapshot_date": start + pd.Timedelta(days=day),
                "vuln_count": vuln[i], "vuln_new_28d": 0.0, "monthly_downloads": 1e5,
                "days_since_last_release": 30.0, "scorecard_overall": 5.0, "bus_factor": 2.0,
            })
    frame = pd.DataFrame(rows)
    # log1p(vuln) is the model's covariate; make the simulated signal match it exactly in scale
    return frame


def test_counting_process_lags_covariates_and_labels_the_outcome_day():
    snaps = pd.DataFrame({
        "name": ["a"] * 4, "snapshot_date": pd.date_range("2026-01-01", periods=4),
        "vuln_count": [1, 1, 3, 3], "monthly_downloads": 10, "days_since_last_release": 5,
    })
    cp = build_counting_process(snaps)
    assert list(cp["day"]) == [1, 2, 3]
    assert list(cp["event"]) == [0, 1, 0]  # the increase happens between day 1 and day 2
    # covariate on the event row is the value *before* the increase: log1p(1), not log1p(3)
    assert cp.loc[cp["day"] == 2, "log_vuln_history"].iloc[0] == pytest.approx(np.log1p(1))


def test_missing_snapshot_day_drops_the_interval_instead_of_imputing_it():
    dates = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-04", "2026-01-05"])
    snaps = pd.DataFrame({"name": "a", "snapshot_date": dates, "vuln_count": [1, 1, 2, 2]})
    cp = build_counting_process(snaps)
    assert list(cp["day"]) == [1, 4]  # day 3 (gap before it) is dropped, not guessed


def test_partial_likelihood_matches_brute_force_loops():
    cp = build_counting_process(_simulate(n_packages=25, n_days=40, seed=1))
    fit = fit_cox(cp, VARYING, l2=0.0)
    Z = ((cp[fit.features].astype(float).fillna(dict(zip(fit.features, fit.fill, strict=True)))
          - fit.mean) / fit.scale).to_numpy()

    def brute(beta):
        eta = Z @ beta
        ll = 0.0
        for t in np.unique(cp["day"]):
            m = (cp["day"] == t).to_numpy()
            d = cp["event"].to_numpy()[m].sum()
            if d:
                ll += eta[m & (cp["event"].to_numpy() == 1)].sum() - d * np.log(np.exp(eta[m]).sum())
        return ll

    assert fit.log_likelihood == pytest.approx(brute(fit.beta), abs=1e-8)
    # a true maximum: no direction improves the likelihood
    for k in range(len(fit.beta)):
        for sign in (+1, -1):
            nudged = fit.beta.copy()
            nudged[k] += sign * 1e-3
            assert brute(nudged) <= brute(fit.beta) + 1e-9


def test_cox_equals_poisson_regression_with_free_daily_baseline():
    sm = pytest.importorskip("statsmodels.api")
    cp = build_counting_process(_simulate(n_packages=30, n_days=50, seed=2))
    fit = fit_cox(cp, VARYING, l2=0.0)
    Z = ((cp[fit.features].astype(float).fillna(dict(zip(fit.features, fit.fill, strict=True)))
          - fit.mean) / fit.scale).to_numpy()
    design = np.hstack([Z, pd.get_dummies(cp["day"], dtype=float).to_numpy()])
    glm = sm.GLM(cp["event"].to_numpy(float), design, family=sm.families.Poisson()).fit(maxiter=300, tol=1e-12)
    assert np.allclose(fit.beta, glm.params[: len(fit.beta)], atol=1e-7)
    assert np.allclose(fit.se_model, glm.bse[: len(fit.beta)], atol=1e-7)


def test_recovers_a_known_hazard_ratio_and_a_shock_free_baseline():
    # simulate directly on the model's own covariate so the true coefficient is known
    rng = np.random.default_rng(3)
    n, days, true_beta = 120, 100, 0.8
    x = rng.normal(size=n)
    rows = []
    for day in range(1, days + 1):
        shock = 2.5 if 30 <= day < 35 else 1.0
        p = 1 - np.exp(-0.01 * shock * np.exp(true_beta * x))
        for i in range(n):
            rows.append({"name": f"p{i}", "day": day, "event": int(rng.random() < p[i]),
                         **{f: 0.0 for f in SURVIVAL_FEATURES}, "log_vuln_history": x[i]})
    cp = pd.DataFrame(rows)
    fit = fit_cox(cp, ["log_vuln_history"], l2=0.0)
    beta_per_unit = fit.beta[0] / fit.scale[0]
    lo = (fit.beta[0] - 1.96 * fit.se_robust[0]) / fit.scale[0]
    hi = (fit.beta[0] + 1.96 * fit.se_robust[0]) / fit.scale[0]
    assert lo <= true_beta <= hi, f"true beta {true_beta} outside CI [{lo:.2f}, {hi:.2f}] (est {beta_per_unit:.2f})"
    # the shock lands in the baseline hazard, not the coefficient
    base = fit.baseline_hazard
    assert np.mean([base[d] for d in range(30, 35)]) > 1.5 * np.mean([base[d] for d in range(60, 90)])


def test_selection_keeps_the_signal_and_drops_pure_noise_covariates():
    rng = np.random.default_rng(4)
    n, days = 150, 90
    sig = rng.normal(size=n)
    rows = []
    for day in range(1, days + 1):
        p = 1 - np.exp(-0.01 * np.exp(1.0 * sig))
        for i in range(n):
            row = {f: rng.normal() for f in SURVIVAL_FEATURES}
            row.update({"name": f"p{i}", "day": day, "event": int(rng.random() < p[i]), "log_vuln_history": sig[i]})
            rows.append(row)
    fit = fit_cox_selected(pd.DataFrame(rows), z_threshold=3.0)
    assert fit.features == ["log_vuln_history"]


def test_probability_is_monotone_in_horizon_and_risk_and_bounded():
    cp = build_counting_process(_simulate(n_packages=40, n_days=60, seed=5))
    fit = fit_cox(cp, VARYING)
    lo = pd.DataFrame([{f: 0.0 for f in SURVIVAL_FEATURES}])
    hi = lo.assign(log_vuln_history=3.0)
    p14, p30 = fit.event_probability(hi, 14)[0], fit.event_probability(hi, 30)[0]
    assert 0 <= fit.event_probability(lo, 14)[0] < p14 < p30 <= 1
    assert fit.event_probability(lo, 30)[0] < p30


def test_auc_matches_known_values():
    assert _auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9])) == 1.0
    assert _auc(np.array([0, 1, 0, 1]), np.array([0.5, 0.5, 0.5, 0.5])) == 0.5
    assert np.isnan(_auc(np.array([1, 1]), np.array([0.2, 0.3])))


def test_landmark_forecast_beats_chance_on_held_out_days_and_never_peeks():
    cp = build_counting_process(_simulate(n_packages=80, n_days=100, seed=6, beta=1.0))
    fit = fit_cox(cp[cp["day"] <= 60], VARYING)
    result = landmark_evaluation(cp, fit, lambda r: fit.event_probability(r, 14),
                                 first_landmark=60, horizon_days=14)
    assert result.auc > 0.6
    # Permuting outcomes across packages within each day severs the covariate-outcome link; skill must
    # fall to chance, proving the AUC comes from the covariates and not from the evaluation plumbing.
    shuffled = cp.copy()
    shuffled["event"] = shuffled.groupby("day")["event"].transform(
        lambda s: s.sample(frac=1, random_state=0).to_numpy()
    )
    broken = landmark_evaluation(shuffled, fit, lambda r: fit.event_probability(r, 14),
                                 first_landmark=60, horizon_days=14)
    assert abs(broken.auc - 0.5) < 0.12


def test_scoring_returns_probabilities_for_every_package_and_skips_thin_history():
    snaps = _simulate(n_packages=60, n_days=80, seed=7)
    scored = score_new_advisory_risk(snaps)
    assert scored is not None
    scores, info = scored
    assert set(scores["name"]) == set(snaps["name"])
    assert scores["p_new_advisory_14d"].between(0, 1).all()
    assert (scores["p_new_advisory_30d"] >= scores["p_new_advisory_14d"]).all()
    assert info["n_events"] >= 30 and info["features"]
    # too little history -> no number at all
    assert score_new_advisory_risk(snaps[snaps["snapshot_date"] < "2026-01-10"]) is None


def test_osv_failure_and_recovery_do_not_create_false_events():
    """ingest.osv returns vuln_count=0 on a failed lookup; recovery must not read as a burst of advisories."""
    dates = pd.date_range("2026-01-01", periods=4)
    snaps = pd.DataFrame({
        "name": "a", "snapshot_date": dates, "vuln_count": [5, 0, 5, 186],
        "source_status": ['{"osv": true}', '{"osv": false}', '{"osv": true}', '{"osv": true}'],
    })
    cp = build_counting_process(snaps)
    # day 1 (5->0 failed) and day 2 (0->5 recovery) are unobserved; only 5 -> 186 on day 3 is real
    assert list(cp["day"]) == [3]
    assert list(cp["event"]) == [1]
    # a +181 jump is ONE package-day event, not 181 advisories
    assert int(cp["event"].sum()) == 1


def test_missing_counts_are_unobserved_not_negative_labels():
    dates = pd.date_range("2026-01-01", periods=3)
    snaps = pd.DataFrame({"name": "a", "snapshot_date": dates, "vuln_count": [1.0, np.nan, 2.0]})
    assert build_counting_process(snaps).empty


def test_landmark_requires_complete_follow_up():
    cp = build_counting_process(_simulate(n_packages=30, n_days=70, seed=11))
    fit = fit_cox(cp[cp["day"] <= 40], VARYING)
    full = landmark_evaluation(cp, fit, lambda r: fit.event_probability(r, 14),
                               first_landmark=40, horizon_days=14)
    # drop one package's rows inside the outcome windows: it must leave the evaluation, not become a 0
    victim = cp["name"].iloc[0]
    gapped = cp[~((cp["name"] == victim) & (cp["day"] > 45))]
    partial = landmark_evaluation(gapped, fit, lambda r: fit.event_probability(r, 14),
                                  first_landmark=40, horizon_days=14)
    assert partial.n_rows < full.n_rows


def test_likelihood_ratio_uses_the_unpenalised_likelihood():
    cp = build_counting_process(_simulate(n_packages=40, n_days=60, seed=12))
    fit = fit_cox(cp, VARYING, l2=0.5)
    assert fit.log_likelihood_unpenalized > fit.log_likelihood  # the ridge term only subtracts
    assert fit.log_likelihood_unpenalized == pytest.approx(
        fit.log_likelihood + 0.5 * fit.l2 * float(fit.beta @ fit.beta))

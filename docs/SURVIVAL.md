# Survival analysis for dependency risk

The risk classifier asks a yes/no question: *will something bad happen within a fixed 14 days?* That discards
**when** it happens, treats a package watched for 14 days like one watched for 110, and cannot say how risk
changes with the horizon. A survival model answers a better question: **how long until a package receives a new
security advisory, and how does that depend on what we know about it?**

Reproduce the evaluation with `python pipeline/scripts/survival_analysis.py --snapshots-csv <export>` (output:
[`survival_results.json`](survival_results.json)) and the current-snapshot figures with
`docs/blog/charts/build_evidence.py` (output: [`blog/charts/evidence.json`](blog/charts/evidence.json), which
records a SHA-256 of the exact export used). The implementation is `pipeline/oss_radar/models/survival.py`
(NumPy only) and is covered by `pipeline/tests/test_survival.py`.

## 1. Setup

- **Event.** The package's known-advisory count (`vuln_count`) increased since the previous daily snapshot.
  The count is cumulative on this data (1 decrease in 10,101 package-days,
  0 snapshots with a failed OSV lookup), so increases are most plausibly new advisories
  appearing. Count increases alone cannot tell a new publication from a backfill or a change in how a package
  is matched, so this is an **observed advisory-count increase**, not a verified disclosure date, and it is
  *disclosure*, not "the package became unsafe" (section 6). A burst of many advisories on one day counts as one
  package-day event.
- **Unobserved days are not "no event".** A failed OSV lookup is stored by the collector as `vuln_count = 0`,
  so intervals whose endpoints were not both valid, finite and on consecutive days are dropped rather than
  labelled. Landmark evaluation likewise only scores packages followed for the whole outcome window.
- **Data.** 91 packages × 111 daily snapshots = 10,101 package-days,
  **168 events on 46 distinct days** in the 2026-06-20 to 2026-10-09
  export, 42 packages with at least one event. Events recur, so a package stays at risk after an event
  (Andersen–Gill).
- **Time axis.** Calendar days since the first snapshot. This matters: on 2026-06-30,
  2026-07-08 and 2026-07-14,
  21, 26
  and 22 packages respectively gained advisories on the
  same day, which looks like bulk publication. A shared baseline hazard absorbs such common shocks; a model
  without one would blame them on package features.
- **Covariates** are the *previous* day's snapshot, so nothing from the outcome day leaks in (tested).
  They are log-transformed and standardised: advisory history, advisories in the last 28 days, monthly
  downloads, days since last release, OpenSSF scorecard, bus factor.

## 2. The model: Cox proportional hazards

The hazard (instantaneous event rate) for package `i` on day `t` is

```
λᵢ(t) = λ₀(t) · exp( xᵢ(t−1)ᵀ β )
```

`λ₀(t)` is an unspecified baseline hazard shared by all packages; `exp(βₖ)` is the **hazard ratio**: how much
the rate is multiplied for a one-standard-deviation increase in covariate `k`.

### Fitting without ever modelling λ₀(t)

On each event day `t`, let `D_t` be the packages with an event (`d_t = |D_t|`) and `R_t` all packages at risk.
Conditioning on "an event happened today", the chance it was these packages and not the others is a ratio of
risks, in which `λ₀(t)` cancels. Multiplying over days gives the (Breslow) partial likelihood

```
ℓ(β) = Σ_t [ Σ_{i∈D_t} xᵢᵀβ  −  d_t · log Σ_{j∈R_t} exp(xⱼᵀβ) ]
```

Define `S0_t = Σ_{R_t} e^{xⱼᵀβ}`, `S1_t = Σ_{R_t} xⱼ e^{xⱼᵀβ}`, `S2_t = Σ_{R_t} xⱼxⱼᵀ e^{xⱼᵀβ}`. Then

```
gradient   U(β) = Σ_t [ Σ_{i∈D_t} xᵢ  −  d_t · S1_t / S0_t ]
Hessian    H(β) = −Σ_t d_t · [ S2_t / S0_t  −  (S1_t / S0_t)(S1_t / S0_t)ᵀ ]
```

The log-likelihood is concave, so Newton–Raphson (with a backtracking line search and a tiny ridge, 10⁻³) finds
the unique maximum. The Hessian is minus a weighted covariance of the covariates in each risk set, which is why
a covariate with no variation cannot be estimated.

### Standard errors that respect clustering

Rows from one package are not independent, so uncertainty uses a sandwich estimator clustered by package:
`V = H⁻¹ ( Σ_g U_g U_gᵀ ) H⁻¹`, where `U_g` sums the score residuals
`(xᵢₜ − x̄_t)(δᵢₜ − e^{xᵢₜᵀβ} d_t / S0_t)` over package `g`.

### The baseline hazard and a forecast

After fitting, the Breslow estimate is `λ̂₀(t) = d_t / S0_t`. The chance of **at least one** new advisory in the
next `H` days, holding covariates and the baseline at their current level, is

```
P(event within H days | x) = 1 − exp( −λ̄₀ · H · exp(xᵀβ) )
```

where `λ̄₀` is the mean baseline hazard over the **most recent 28 days** rather than the whole history, because
the early period contained the bulk-publication shocks (the same drift lesson as the growth intervals, see
[CONFORMAL.md](CONFORMAL.md)).

### Feature selection without leakage

`fit_cox_selected` fits every covariate, keeps those with robust `|z| > 2`, and refits. Because the rule sees
only the rows it is given, applying it inside a training window cannot use test information. Inside the three
training windows it chose advisory history and days since release. On the full history it also keeps advisories
in the last 28 days, so the model scored today has three covariates; its advisory-history hazard ratio is
2.29 per SD (95% interval 1.77 to
2.96), against 2.38
in the all-covariate fit shown in section 4.

## 3. Verifying the implementation

A hand-written optimiser is only credible if it is checked against something independent:

| Check | Result |
|---|---|
| Brute-force loop likelihood (`for each day: …`) vs the vectorised one | equal to 1e-8; gradient is zero at the solution |
| **Cox (Breslow) ≡ Poisson regression with one free baseline per day** (a known identity), fitted with `statsmodels` GLM | coefficients agree to **1.0e-14** (`evidence.json`) |
| Robust SE vs `statsmodels` cluster-robust | differ by a constant 1.1%, consistent with `statsmodels`' built-in small-sample correction (`G/(G−1)` with 91 clusters, plus a degrees-of-freedom term) |
| Known hazard ratio recovered from simulation (β = 0.8) | 95% CI contains the truth; an injected 2.5× shock lands in `λ₀(t)`, not in β |
| `statsmodels.PHReg` with entry times | did **not** converge on this data (non-zero gradient), so it was not used as the reference |

## 4. What the data say

Hazard ratios per one standard deviation, with 95% clustered-robust intervals (all six covariates, full sample):

| Covariate | HR | 95% CI | z |
|---|--:|--:|--:|
| Advisory history (log) | **2.38** | 1.85–3.07 | 6.7 |
| Advisories in last 28 days (log) | **1.23** | 1.05–1.44 | 2.6 |
| Bus factor | 1.07 | 0.78–1.46 | 0.4 |
| Monthly downloads (log) | 0.93 | 0.79–1.09 | -0.9 |
| OpenSSF scorecard | 0.90 | 0.69–1.18 | -0.7 |
| Days since last release (log) | **0.75** | 0.61–0.94 | -2.6 |

Likelihood-ratio χ² = 281 on 6 degrees of freedom, computed from the *unpenalised*
log-likelihoods (the fitted objective carries a tiny ridge term). The hazard ratios describe the full fitted
sample and are associations, not causal effects.

**Proportional hazards is only informally checked.** A Spearman trend of Schoenfeld residuals against time is
non-significant for most covariates, but is flagged for recent advisories (ρ = 0.34, p = 0.023)
and weak for advisory history (ρ = 0.23, p = 0.12). With
46 event days this is a low-power diagnostic: it does not establish proportional hazards, and the
recent-advisories effect should be read as an average over time.

## 5. Does it forecast better? (temporal landmark test)

Train on days ≤ T₀, then at weekly landmarks after T₀ forecast every package's chance of a new advisory in the
next `H` days from covariates known that day, and compare with what happened. Only packages followed for the
whole outcome window are scored. Results for T₀ = 70 and H = 14 (364 package-landmarks, 30 positives):

| Method | AUC | Brier | Mean predicted (actual 8.2%) |
|---|--:|--:|--:|
| **Survival model (Cox, selected covariates)** | 0.822 | 0.061 | 6.6% |
| Cox, all covariates | 0.809 | 0.061 | 6.6% |
| Cox, selected, whole-history baseline | 0.822 | 0.085 | 20.9% |
| Standard fixed-horizon classifier | 0.789 | 0.112 | 22.4% |
| Classifier, intercept anchored to recent rate | 0.789 | 0.073 | 8.1% |
| Constant: recent observed rate (no features) | 0.500 | 0.077 | 4.5% |
| Constant: training average rate | 0.500 | 0.083 | 16.6% |
| Advisory history alone (ranking only) | 0.847 | n/a | n/a |
| Existing composite risk heuristic (ranking only) | 0.670 | n/a | n/a |

Brier score is the mean squared error of the probability (lower is better). It rewards ranking *and* agreement
with the observed rate, so it is shown together with the mean forecast. It is not a calibration test: nothing
here assesses calibration at individual probability levels, and no decision threshold has been validated.

Across the three training windows (14-day horizon), the shipped model's Brier score is
0.060 / 0.061 / 0.054
(T₀ = 60 / 70 / 80), with mean forecasts of 6.6% / 6.6% / 7.3%
against actual rates of 8.2% / 8.2% / 7.7%. The full tables for 14 and
28 days and all three windows are in `survival_results.json`.

What this does and does not show:

1. **Anchoring to the recent event rate is what fixed the probabilities, not survival modelling as such.**
   The standard classifier forecasts 22.4% against 8.2% observed (T₀ = 70), and so does
   the *same Cox model* when its baseline hazard is the whole training history instead of the last 28 days
   (20.9%; Brier 0.085, no better than guessing the training average
   0.083). Training windows that contain bulk-publication spikes teach any model a rate that is too
   high for the period that follows. The recency treatment is what removes that.
2. **With the same anchoring, the classifier gets close in two of three windows.** Its anchored Brier score is
   0.122 / 0.073 / 0.059 against the survival model's
   0.060 / 0.061 / 0.054. The survival model has the lowest Brier score in every window and at both
   horizons, and ranks best of the model-based forecasts (AUC 0.82–0.84). The margins are
   modest, the samples are small, and no confidence intervals are estimated.
3. **A constant recent rate is a strong baseline.** Using no features at all gives Brier
   0.076 / 0.077 / 0.072; the features improve on it by roughly 0.015–0.02.
4. **It does not beat the single best feature at ranking.** Advisory history alone reaches AUC
   0.85 against 0.82 (T₀ = 70). The extra covariates add probabilities on a recent-rate scale and
   per-factor hazard ratios, not better ordering.
5. **The composite heuristic ranks this outcome poorly** (AUC 0.63–0.67).
   It was designed to flag general dependency risk, not to predict new advisories, so this is a statement about
   fit to this outcome, not a verdict on the heuristic overall.

## 6. Caveats

- **Outcome ≠ danger.** Days since release has HR ≈ 0.75: *recently released* packages show higher rates of
  advisory-count increases, the opposite of the heuristic's assumption that staleness is risk. A scrutiny or
  disclosure-rate explanation is plausible but **untested** here. The model predicts advisory *arrival*, not
  exploitability or severity.
- **Short history.** 111 days, 46 event days, bulk-ingest shocks early on; evaluation windows hold 91–546 package-landmarks
  and 14–45 positives, from overlapping weekly landmarks. AUCs are noisy and their uncertainty was not
  estimated.
- **Covariates are held at today's values** for the forecast horizon, and the baseline at its recent level. A new
  bulk-publication shock would make every probability too low; a quiet period too high.
- **Data-quality dependence.** Event labels depend on the OSV lookup. Failed lookups are dropped, but silent
  changes in how advisories are attributed to a package would appear as events.
- **Informational only.** Survival probabilities are shown beside the risk score on each package; they do **not**
  feed `risk_score`, the champion/challenger registry, or any gate. A failure while *fitting* the survival
  model is caught and the run continues without advisory probabilities.
- **Thin-data guard.** With fewer than 30 events or 28 days of history no probability is produced, and the
  dashboard shows nothing instead of a number.

## 7. Implementation map

| Piece | Location |
|---|---|
| Counting-process builder, Cox fit, selection, landmark evaluation, scoring | `pipeline/oss_radar/models/survival.py` |
| Daily scoring hook (failure-isolated) | `pipeline/oss_radar/orchestrator/pipeline.py` |
| `p_new_advisory_14d`, `p_new_advisory_30d` on every prediction | `models/scoring.py`, `warehouse/schema.py` |
| Package drawer line | `dashboard/app/static/index.html` |
| Evidence harness and results | `pipeline/scripts/survival_analysis.py`, `docs/survival_results.json` |
| Tests (unit and statistical + API + browser) | `pipeline/tests/test_survival.py`, `dashboard/tests/` |

## References

- Cox (1972). *Regression models and life-tables.* JRSS B.
- Breslow (1974). *Covariance analysis of censored survival data.* Biometrics.
- Andersen & Gill (1982). *Cox's regression model for counting processes.* Annals of Statistics.
- Lin & Wei (1989). *The robust inference for the Cox proportional hazards model.* JASA.
- Grambsch & Therneau (1994). *Proportional hazards tests and diagnostics based on weighted residuals.* Biometrika.
- Heagerty & Zheng (2005). *Survival model predictive accuracy and ROC curves.* Biometrics. (landmark evaluation)

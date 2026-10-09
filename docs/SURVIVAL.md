# Survival analysis for dependency risk

The risk classifier asks a yes/no question: *will something bad happen within a fixed 14 days?* That discards
**when** it happens, treats a package watched for 14 days like one watched for 110, and cannot say how risk
changes with the horizon. A survival model answers a better question: **how long until a package receives a new
security advisory, and how does that depend on what we know about it?**

Reproduce every number here with `python pipeline/scripts/survival_analysis.py` (output:
[`survival_results.json`](survival_results.json)). The implementation is `pipeline/oss_radar/models/survival.py`
(NumPy only) and is covered by `pipeline/tests/test_survival.py`.

## 1. Setup

- **Event.** The package's known-advisory count (`vuln_count`) increased since the previous daily snapshot.
  On this data the count is cumulative (one decrease in 10,101 package-days, no day-to-day flapping), so
  increases are genuine new advisories, not ingestion noise. This is *disclosure*, not "the package became
  unsafe" (see section 6).
- **Data.** 91 packages × 110 daily intervals = 10,010 package-days, **167 events on 45 distinct days**, 42
  packages with at least one event. Events recur, so a package stays at risk after an event (Andersen–Gill).
- **Time axis.** Calendar days since the first snapshot. This matters: on 30 Jun, 8 Jul and 14 Jul, 21–26
  packages each gained advisories on the same day (bulk publication). A shared baseline hazard absorbs such
  common shocks; a model without one would blame them on package features.
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
only the rows it is given, applying it inside a training window cannot use test information. It chose the same
two covariates (advisory history, days since release) in all three training windows.

## 3. Verifying the implementation

A hand-written optimiser is only credible if it is checked against something independent:

| Check | Result |
|---|---|
| Brute-force loop likelihood (`for each day: …`) vs the vectorised one | equal to 1e-8; gradient is zero at the solution |
| **Cox (Breslow) ≡ Poisson regression with one free baseline per day** (a known identity), fitted with `statsmodels` GLM | coefficients agree to **1.8e-14**, model SEs to 1.6e-15 |
| Robust SE vs `statsmodels` cluster-robust | differ by a constant 1.1%, consistent with `statsmodels`' built-in small-sample correction (`G/(G−1)` with 91 clusters, plus a degrees-of-freedom term) |
| Known hazard ratio recovered from simulation (β = 0.8) | 95% CI contains the truth; an injected 2.5× shock lands in `λ₀(t)`, not in β |
| `statsmodels.PHReg` with entry times | did **not** converge on this data (non-zero gradient), so it was not used as the reference |

## 4. What the data say

Hazard ratios per one standard deviation, with 95% clustered-robust intervals (full sample):

| Covariate | HR | 95% CI | z |
|---|--:|--:|--:|
| Advisory history (log) | **2.40** | 1.85–3.09 | 6.7 |
| Advisories in last 28 days (log) | 1.22 | 1.05–1.42 | 2.6 |
| Days since last release (log) | **0.75** | 0.61–0.94 | −2.6 |
| Monthly downloads (log) | 0.93 | 0.79–1.09 | −0.9 |
| OpenSSF scorecard | 0.91 | 0.69–1.18 | −0.7 |
| Bus factor | 1.06 | 0.78–1.45 | 0.4 |

Likelihood-ratio χ² = 276 on 6 degrees of freedom. Proportional hazards holds up: the Schoenfeld-residual trend
is non-significant for every covariate (closest: recent advisories, ρ = 0.29, p = 0.055).

## 5. Does it forecast better? (temporal landmark test)

Train on days ≤ T₀, then at weekly landmarks after T₀ forecast every package's chance of a new advisory in the
next `H` days from covariates known that day, and compare with what happened. Results for T₀ = 70 (the other
windows tell the same story, see the JSON):

| Method | AUC @14d | Brier @14d | mean predicted | observed |
|---|--:|--:|--:|--:|
| **Cox, selected covariates** | 0.822 | **0.061** | 6.6% | 8.2% |
| Cox, all covariates | 0.809 | 0.061 | 6.6% | 8.2% |
| Fixed-horizon logistic, same covariates | 0.789 | 0.112 | 22.4% | 8.2% |
| Existing composite risk heuristic | 0.670 | n/a | n/a | 8.2% |
| Advisory history alone | **0.847** | n/a | n/a | 8.2% |
| Base rate (no information) | 0.500 | 0.083 | 16.6% | 8.2% |

At 28 days: Cox AUC 0.782 / Brier 0.088; logistic 0.764 / 0.192; heuristic 0.643; history alone 0.819.

What this does and does not show:

1. **Calibration is the real win.** The logistic classifier ranks nearly as well but forecasts 22% against 8%
   observed and scores *worse than guessing the base rate*: trained on a window full of bulk-publication spikes,
   it cannot tell a shock from a feature effect. The Cox model's calendar-time baseline and 28-day recent-hazard
   window avoid that.
2. **It does not beat the single best feature at ranking.** Advisory history alone gets AUC 0.85 against 0.82.
   The extra covariates add calibrated probabilities, an uncertainty-aware hazard ratio per factor, and
   horizon-consistent forecasts, not better ordering. That is a legitimate finding, not a failure to hide.
3. **The composite heuristic ranks this outcome poorly (AUC 0.57–0.67 across the three training windows).** It was designed to flag general
   dependency risk, not to predict new advisories, so this is a statement about fit to this outcome, not a
   verdict on the heuristic overall.

## 6. Caveats

- **Outcome ≠ danger.** Days since release has HR 0.75: *recently released* packages gain advisories **faster**,
  the opposite of the heuristic's assumption that staleness is risk. Most plausibly active projects attract more
  scrutiny and disclosure. The model predicts advisory *arrival*, not exploitability or severity.
- **Short history.** 110 days, 45 event days, bulk-ingest shocks early on; test windows hold 91–546 rows and
  14–45 positives. Intervals are wide and the AUCs are noisy (±0.05 is plausible).
- **Covariates are held at today's values** for the forecast horizon, and the baseline is held at its recent
  level. A new bulk-publication shock would make every probability too low.
- **Informational only.** Survival probabilities are shown beside the risk score on each package; they do **not**
  feed `risk_score`, the champion/challenger registry, or any gate.
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
| Tests (10 unit/statistical + API + browser) | `pipeline/tests/test_survival.py`, `dashboard/tests/` |

## References

- Cox (1972). *Regression models and life-tables.* JRSS B.
- Breslow (1974). *Covariance analysis of censored survival data.* Biometrics.
- Andersen & Gill (1982). *Cox's regression model for counting processes.* Annals of Statistics.
- Lin & Wei (1989). *The robust inference for the Cox proportional hazards model.* JASA.
- Grambsch & Therneau (1994). *Proportional hazards tests and diagnostics based on weighted residuals.* Biometrika.
- Heagerty & Zheng (2005). *Survival model predictive accuracy and ROC curves.* Biometrics. (landmark evaluation)

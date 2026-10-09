# Prediction intervals: drift-aware conformal inference

The growth model forecasts 70-day log download growth. A single number hides how uncertain that forecast is,
so each package also gets an **80% prediction interval** (and a 90% one is stored in the model artifact). This
page explains the method, why the textbook version failed on this data, what replaced it, and what is **not**
guaranteed.

## 1. The textbook method: split conformal prediction

Let `ŷ(x)` be any point forecaster trained on a *training* set. Hold out a *calibration* set of `n` examples
and compute absolute residuals (the *conformity scores*)

```
sᵢ = |yᵢ − ŷ(xᵢ)|,   i = 1..n
```

For a miscoverage level `α` (e.g. 0.2 for an 80% interval) take the finite-sample-corrected quantile

```
q̂ = the ⌈(n+1)(1−α)⌉-th smallest sᵢ,        interval(x) = [ ŷ(x) − q̂ ,  ŷ(x) + q̂ ]
```

**Guarantee.** If the calibration points and the new point are *exchangeable*, then
`P( y_new ∈ interval ) ≥ 1 − α`, for any model and any data distribution. The proof is a rank argument: under
exchangeability the new score is equally likely to fall in any of the `n+1` rank positions, so it lands at or
below `q̂` with probability at least `⌈(n+1)(1−α)⌉ / (n+1) ≥ 1−α`.

## 2. Why it failed here

Exchangeability does not hold. On the production history (91 packages, 34 forecast-origin dates, 3,094
supervised rows) the model's error grows steadily with the origin date:

| Origin date | MAE | Mean error (actual − predicted) |
|---|--:|--:|
| 2026-07-02 | 0.096 | −0.036 |
| 2026-07-17 | 0.157 | −0.120 |
| 2026-07-29 | 0.221 | −0.191 |

Download growth slowed through the summer and the model kept predicting too much of it. Residuals measured on
older dates are therefore smaller than the residuals the model makes on newer ones.

Calibrating once on the validation dates and testing on the later test dates gave:

| Nominal | Realised coverage |
|---|--:|
| 80% | **58.5%** |
| 90% | 79.6% |

The guarantee is not violated by a bug. Its assumption is simply false for this data, and the intervals were far
too narrow.

## 3. The fix: two standard drift remedies, combined

### 3.1 Recency weighting (Barber, Candès, Ramdas & Tibshirani, 2023)

*Conformal prediction beyond exchangeability* shows that weighting calibration scores keeps a bounded coverage
gap when the data drift. Here the weight of a residual from forecast-origin date `d`, with the newest calibration
date `d*`, is

```
wᵈ = 2^( −(d* − d) / h )          h = half-life (2 origin dates)
```

and the quantile becomes a *weighted* quantile in which the unseen test point is a point mass at +∞ carrying the
largest weight (this is the finite-sample correction):

```
q̂ = inf { t : Σᵢ wᵢ·1[sᵢ ≤ t] / ( Σᵢ wᵢ + max wᵢ )  ≥  1 − α }
```

Recent errors dominate, so the interval follows the current error regime instead of the historical average.

### 3.2 Adaptive Conformal Inference (Gibbs & Candès, 2021)

ACI treats the miscoverage level as a control variable. After each step it compares the realised miss rate with
the target and adjusts:

```
α_{t+1} = α_t + γ ( α − errₜ ),      errₜ = fraction of step t's points that fell outside their interval
```

Missing more than intended makes `α_t` smaller, which gives a higher quantile level `1−α_t` and a wider
interval. Over-covering does the opposite. Here a "step" is one forecast-origin date, which resolves 91
packages at once, so `errₜ` is a rate rather than a single 0/1 (a batch version of ACI). `α_t` is clipped to
[0.01, 0.5]; `γ = 0.2`.

## 4. Results

All figures use the production feature set (15 download features) on data through 2026-10-08, and the
75.8% / 87.4% row matches the metrics the production pipeline recorded on 2026-10-09.

Evaluation is **forward-chaining**: each origin date is predicted using only strictly earlier dates for
calibration and for the ACI state. The pool is the 10 validation + test dates; the first 3 only seed the
calibration set, so **7 dates (637 rows)** are scored. Residuals come from the train-only model, which never saw
these dates.

| Method | Coverage @ 80% | Coverage @ 90% | Mean interval width @ 80% |
|---|--:|--:|--:|
| Plain split conformal (expanding window) | 67.0% | 84.3% | 0.336 |
| + recency weighting (h = 2) | 71.4% | 86.2% | 0.368 |
| + ACI (γ = 0.1) | 73.8% | 87.0% | 0.389 |
| **+ ACI (γ = 0.2, shipped)** | **75.8%** | **87.4%** | 0.410 |

Drift-aware calibration closes most, **not all**, of the gap to nominal coverage, at the price of intervals about
22% wider (0.410 vs 0.336 at the 80% level). The shipped metrics are recomputed from live data on every training
run and stored with the model (`conformal_coverage_80`, `conformal_coverage_90`, `conformal_width_80`,
`conformal_width_90`, `conformal_n_dates_scored`).

The interval is on the log-growth scale, so it is symmetric there and asymmetric in percent
(`Δ = 100·expm1(log-growth)`). The momentum score is a monotone sigmoid of the prediction, so its interval is
obtained by transforming the endpoints.

## 5. What this does *not* guarantee

State these when presenting the numbers:

1. **Coverage is empirical, not guaranteed.** Under drift the finite-sample guarantee does not apply; the ACI
   long-run guarantee needs many more steps than the 7 scored here.
2. **The evaluation is lag-free.** It assumes the previous origin date's outcomes are known before predicting the
   next. Production outcomes take 70 days to resolve, so live calibration is staler and live coverage can be
   lower under continuing drift. The first live proof is realised coverage on *matured* predictions, because
   intervals are stored with each prediction and can be scored once the 70 days elapse.
3. **Settings were examined on the same 7 dates** they are reported on: γ and the half-life were compared on a
   small grid during development, and γ = 0.2 scored best. Treat 75.8% as indicative, not as an unbiased estimate.
4. **Outcome windows overlap.** Consecutive origins (3 days apart) share most of their 70-day outcome windows,
   so rows are far from independent and the effective sample size is much smaller than 637.
5. **Calibration uses the train-only model.** Its residuals are valid out-of-sample scores; they are applied to the
   deployed model (refit on train + validation), which can only be a little better, so this is slightly
   conservative.
6. **Borrowed calibration.** The validation gate can hold a retrained challenger while an older champion keeps
   serving. The champion's own residuals on the calibration dates are in-sample, so it cannot be calibrated from
   them; it borrows the latest challenger's out-of-sample error scale instead (same features and
   hyperparameters, different training date). That is an approximation, it is applied in memory only, and the
   pipeline logs `pipeline.interval_calibration_borrowed` whenever it happens.
7. **Uncalibrated fallback.** If a model has fewer than four distinct calibration dates, or the champion is the
   persistence fallback, no interval is shown and the dashboard says so.

## 6. Implementation map

| Piece | Location |
|---|---|
| Weighted quantile, recency weights, forward chain, calibrator | `pipeline/oss_radar/models/conformal.py` (NumPy only) |
| Calibration at training time, `predict_interval`, artifact persistence | `pipeline/oss_radar/models/growth.py` |
| Interval columns on each prediction | `pipeline/oss_radar/models/scoring.py`, `warehouse/schema.py` |
| Table tooltip and package drawer | `dashboard/app/static/index.html` |
| Unit and statistical tests | `pipeline/tests/test_conformal.py` |
| Browser tests asserting the intervals reach the page | `dashboard/tests/test_e2e_browser.py` |

## References

- Vovk, Gammerman & Shafer (2005). *Algorithmic Learning in a Random World.* (conformal prediction)
- Lei, G'Sell, Rinaldo, Tibshirani & Wasserman (2018). *Distribution-free predictive inference for regression.* (split conformal)
- Gibbs & Candès (2021). *Adaptive conformal inference under distribution shift.* NeurIPS.
- Barber, Candès, Ramdas & Tibshirani (2023). *Conformal prediction beyond exchangeability.* Annals of Statistics.

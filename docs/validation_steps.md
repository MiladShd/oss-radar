# Growth-model validation - step-by-step (Wolfram Language)

*Generated 2026-06-21T19:56:22Z by `pipeline/wolfram/validate_growth.wl` on 15.0.0 for Mac OS X ARM (64-bit) (May 26, 2026).*

Every statistic below is **recomputed from the dumped held-out predictions** and cross-checked
against the Python harness (`docs/validation_results.json`). Deterministic stats must match to
<1e-6. Educational companion to [`VALIDATION.md`](VALIDATION.md).

**Verdict:** 9/9 deterministic checks reproduced. The retired **0.74/0.70** headline was a centered-MA
lookahead leak; the honest numbers are **0.582 same-package / 0.363 package-disjoint**.

---

## R^2  coefficient of determination  (held-out, same-package split)

$$R^2 = 1 - \frac{\sum_i (y_i-\hat y_i)^2}{\sum_i (y_i-\bar y)^2}$$

```
inputs:
  n = 182
  ybar = 0.238057
steps:
  SS_res = 7.854862
  SS_tot = 18.800259
result:  R^2 = 1 - 7.854862 / 18.800259 = 0.58219394
```

**Cross-check vs Python harness:** computed `0.58219394` vs harness `0.58219394`  (|delta| = 0) - **PASS**


## Spearman rho  rank correlation of predictions vs actuals

$$\rho = \frac{\sum_i (R_i-\bar R)(S_i-\bar S)}{\sqrt{\sum_i (R_i-\bar R)^2\,\sum_i (S_i-\bar S)^2}}$$

```
inputs:
  n = 182
  R = rank(y), S = rank(yhat); ties averaged
steps:
  SpearmanRho[y,yhat] = 0.79037374
  Pearson-on-ranks    = 0.79037374  (equal when no ties)
result:  rho = 0.79037374
```

**Cross-check vs Python harness:** computed `0.79037374` vs harness `0.79037374`  (|delta| = 1.11e-16) - **PASS**


## MAE  mean absolute error

$$\mathrm{MAE} = \frac1n \sum_i |y_i - \hat y_i|$$

```
inputs:
  n = 182
steps:
  Sum |y - yhat| = 21.492964
result:  MAE = 21.492964 / 182 = 0.11809321
```

**Cross-check vs Python harness:** computed `0.11809321` vs harness `0.11809321`  (|delta| = 1.39e-17) - **PASS**


## Skill score  vs raw persistence  (MSE-based)

$$\mathrm{skill} = 1 - \frac{\mathrm{MSE}_{\mathrm{model}}}{\mathrm{MSE}_{\mathrm{persistence}}}$$

```
inputs:
  MSE_model = 0.043159
  MSE_pers = 0.731603
steps:
  ratio = 0.058992
result:  skill = 1 - 0.058992 = 0.94100823   (raw persistence is a strawman - see calibrated baseline next)
```

**Cross-check vs Python harness:** computed `0.94100823` vs harness `0.94100823`  (|delta| = 0) - **PASS**


## Calibrated persistence  (FAIR baseline: OLS fit on TRAIN, frozen onto TEST)

$$b=\frac{\sum(p_i-\bar p)(y_i-\bar y)}{\sum(p_i-\bar p)^2},\quad a=\bar y-b\bar p$$

```
inputs:
  train rows = 637
  pbar_train = 1.452887
steps:
  slope b   = 0.14137110  (harness 0.14137110)
  intercept a = 0.09211639  (harness 0.09211639)
result:  R^2_test(calibrated persistence) = 0.02190358   -> model beats it on level (0.582 vs 0.022) and on rank (0.790 vs 0.370)
```

**Cross-check vs Python harness:** computed `0.02190358` vs harness `0.02190358`  (|delta| = 2.22e-16) - **PASS**


## Diebold-Mariano  model vs persistence  (squared-error loss, Newey-West HAC)

$$DM=\frac{\bar d}{\sqrt{\big(\gamma_0+2\sum_{l=1}^{L}(1-\tfrac{l}{L+1})\gamma_l\big)/n}}$$

```
inputs:
  n = 182
  horizon h = 70
  Bartlett lag L = 69
steps:
  dbar = -0.68844488
  HAC var = 0.19100664
  SE = 0.03239579
result:  DM = -0.688445 / 0.032396 = -21.251062   (two-sided p = 0, favours model)
```

**Cross-check vs Python harness:** computed `-21.25106205` vs harness `-21.25106205`  (|delta| = 3.55e-15) - **PASS**


## Between-package R^2  (collapse each package to its mean, then R^2)

$$R^2_{\text{between}} = 1 - \frac{\sum_k(\bar y_k-\bar p_k)^2}{\sum_k(\bar y_k-\bar{\bar y})^2}$$

```
inputs:
  packages = 91
steps:
  isolates the cross-sectional (across-package) skill from within-package wiggle
result:  R^2_between = 0.59055257
```

**Cross-check vs Python harness:** computed `0.59055257` vs harness `0.59055257`  (|delta| = 1.11e-16) - **PASS**


## Head-of-watchlist slope  (predicted vs actual; <1 means regression to the mean)

$$\hat y \approx c + m\,y$$

```
inputs:
  n = 182
steps:
  intercept c = 0.097354
result:  slope m = 0.55208636   (top-10 overlap with real movers is only 2/10 - the head is the least reliable part)
```

**Cross-check vs Python harness:** computed `0.55208636` vs harness `0.55208636`  (|delta| = 4.44e-16) - **PASS**


## Package-disjoint R^2  (GroupKFold, train/test share ZERO packages - the honest number)

$$R^2_{\text{unseen}} = 1 - \frac{\sum(y_i-\hat y^{\,\text{oof}}_i)^2}{\sum(y_i-\bar y)^2}$$

```
inputs:
  oof rows = 819
  folds = 5
steps:
  R^2 = 0.36281059  (harness 0.36281059)
  rho = 0.68307967  (harness 0.68307967)
result:  unseen-package  R^2 = 0.362811,  rho = 0.683080   <- THE NUMBER TO QUOTE (same-package 0.582 leaks package identity)
```

**Cross-check vs Python harness:** computed `0.36281059` vs harness `0.36281059`  (|delta| = 1.11e-16) - **PASS**


## Permutation test  (is the rank skill better than chance?)

$$p = \frac{1 + \#\{\rho^{\text{perm}} \ge \rho^{\text{obs}}\}}{1 + B}$$

```
inputs:
  observed rho = 0.790374
  B = 2000 shuffles
steps:
  #{rho_perm >= rho_obs} = 0
result:  p ~ 0.00050   (harness p = 0.00050; both -> p < 0.001. Exact count differs: NumPy vs WL RNG / block vs simple shuffle)
```

**Cross-check vs Python harness:** computed `0.00049975` vs harness `0.00049975`  (|delta| = 0) - **INFO  (resampling - not an exact-match assertion)**


## Cluster bootstrap  (95% CI for R^2; resample whole PACKAGES, the independent unit)

$$\text{CI} = \big[Q_{2.5\%}(R^{2*}),\,Q_{97.5\%}(R^{2*})\big]$$

```
inputs:
  packages = 91
  B = 2000 resamples
steps:
  median R^2 = 0.5778
result:  95% CI R^2 ~ [0.383, 0.712]   (harness [0.371, 0.720]; both exclude 0 - signal is real, RNG-dependent width)
```

**Cross-check vs Python harness:** computed `0.57784563` vs harness `0.58115166`  (|delta| = 3.31e-3) - **INFO  (resampling - not an exact-match assertion)**


# Growth-Model Validation — does the R² claim survive scrutiny?

*Target:* 70-day forward **log-growth** of package downloads.
*Model:* LightGBM, 14 causal download-dynamics features.
*Harness:* [`pipeline/scripts/validate_growth.py`](../pipeline/scripts/validate_growth.py) — every number here is
reproducible with `python pipeline/scripts/validate_growth.py` and is saved to
[`docs/validation_results.json`](validation_results.json).

This document exists to **stress-test, not flatter** a headline number. It was produced by a
deterministic statistical harness *and* an adversarial LLM statistician panel (see §8), which
together found two separate leaks and a strawman baseline in the original analysis.

---

## 1. TL;DR

- **"R² = 0.74" is not real.** It was inflated ~0.12 by a centered-moving-average **lookahead leak**
  (a feature at date *t* averaged downloads from *t−14 … t+14*). With a causal trailing smoother,
  same split: **R² = 0.582, Spearman = 0.790**.
- **A second, larger leak hid underneath it.** All 91 packages appeared in both train and test, so
  the model memorised each package's level. A **package-disjoint** split (no shared packages) gives
  the honest unseen-package number: **R² = 0.363, Spearman = 0.683**.
- **There is real, significant *cross-sectional* skill.** Against a *fair* calibrated baseline
  (Spearman 0.37), the model ranks 70-day forward growth across packages with Spearman ≈ 0.68–0.79,
  permutation **p < 0.001**, cluster-bootstrap 95% CIs that exclude zero.
- **It is NOT a time-forward forecast.** Valid forecast origins span only 24 days (< the 70-day
  horizon) → ~**0.34 independent forecasts in time**; purged temporal CV is infeasible (0 folds);
  within-package skill is unmeasurable. The result describes *one frozen moment* (April 2026).
- **Ship it as a cross-sectional ranked watchlist**, not a forecaster — and never advertise an
  absolute R² as forecasting accuracy.

---

## 2. The number that matters, at four levels of honesty

| measurement (leak-free unless noted) | R² | Spearman | what it means |
|---|--:|--:|---|
| Leaky headline (centered MA) | 0.740 | 0.826 | **wrong** — lookahead leak |
| Same-package, causal smoother | 0.582 | 0.790 | still leaks package identity |
| **Unseen-package (GroupKFold)** | **0.363** | **0.683** | **the honest cross-sectional skill** |
| Fair calibrated-persistence baseline | 0.022 | 0.370 | what you beat |

The story is "0.74 → 0.36," and the model still clearly beats the fair baseline on ranking.

---

## 3. What was tested

1. **Leak isolation** — identical pipeline/split, swap only the smoother (centered vs causal trailing); the R² gap is the lookahead leak.
2. **Baselines** — persistence (raw *and* train-calibrated), predict-zero, predict-train-mean; MSE skill score.
3. **Package-disjoint GroupKFold** — train/test share zero packages → generalisation to an unseen package.
4. **Cluster bootstrap** — resample whole *packages* (the real independent unit) → 95% CIs.
5. **Permutation test** — package-block label shuffles → p-value for rank skill.
6. **Diebold–Mariano** vs persistence — squared-error loss with Newey-West HAC variance.
7. **Within/between decomposition** + **origin diagnostics** + purged walk-forward feasibility.
8. **Head-of-watchlist reliability** — is the top of the list (the breakouts) trustworthy?
9. **Normalization ablation** — does feature scaling change anything? (§6)

---

## 4. Results

### 4.1 Leak quantification (identical 637/182 split, smoother swapped)
| Smoother | R² | Spearman | MAE |
|---|--:|--:|--:|
| Centered (was shipping) | 0.7015 | 0.7800 | 0.1232 |
| **Causal trailing (leak-free)** | **0.5822** | **0.7904** | **0.1181** |
| Leak magnitude | **0.1193** | +0.010 | −0.005 |

The leak inflates **R²/level calibration**; rank order (Spearman) is almost untouched — i.e. the
metric that moved is the one *least* aligned with a ranking product.

### 4.2 Baselines (leak-free, same-package split, n=182)
| Predictor | R² | Spearman |
|---|--:|--:|
| **Model** | **0.5822** | **0.7904** |
| persistence (raw — strawman) | −6.082 | 0.370 |
| **persistence (train-calibrated — fair)** | **0.022** | **0.370** |
| predict-zero | −0.549 | — |
| predict-train-mean | −0.034 | — |

The raw persistence R² of −6.08 is a **level artifact**: the fair calibration fits slope **0.141**
(persistence had to be shrunk ~7×). The honest skill gap is **model 0.58 vs 0.02 (R²)** and
**0.79 vs 0.37 (rank)** — real, but far less dramatic than the raw "skill 0.94 / DM −21" suggested.

### 4.3 The honest held-out number — package-disjoint GroupKFold (5-fold, n=819)
**R² = 0.363, Spearman = 0.683.** Train and test share no packages, so this removes the
shared-package leak. ~38 % of the same-package R² (0.58 → 0.36) was package-identity leakage.

### 4.4 Inference (conditional on one April-2026 origin-cluster)
| Quantity | Value |
|---|---|
| Cluster-bootstrap 95% CI, R² | **[0.37, 0.72]** (median 0.58) |
| Cluster-bootstrap 95% CI, Spearman | **[0.65, 0.89]** (median 0.79) |
| Permutation p (Spearman, B=2000) | **< 0.001** |
| Diebold–Mariano vs persistence | dm = −21.3 (strawman); honest package-clustered t ≈ −8.2 |
| effective n | **≈ 91 packages** (not 182 rows; the 2 origins are 99.8 % correlated) |

### 4.5 Why it is cross-sectional, not temporal
| Quantity | Value |
|---|---|
| r2_between | 0.591 |
| r2_within | **null** (median 2 rows/package — unmeasurable) |
| distinct origins / span | 9 / **24 days** (< 70-day horizon) |
| independent forecasts in time | **≈ 0.34** |
| purged temporal CV | **infeasible — 0 folds** |

### 4.6 Head-of-watchlist reliability (the breakouts)
| Quantity | Value | reading |
|---|--:|---|
| predicted-vs-actual slope | 0.55 | range-compressed (regresses to mean) |
| top-10 overlap with real movers | **2 / 10** | the head is the *least* reliable part |
| MAE, top growth-decile | 0.265 | vs 0.118 overall |
| high-growth bias | −0.14 | breakouts are underpredicted |

---

## 5. What survived, and what didn't

**Survived scrutiny:**
- The leak quantification (0.70 → 0.58) — reproduced from raw data to 4+ sig figs by two independent agents.
- Real cross-sectional *rank* skill — Spearman 0.68 (unseen package) / 0.79 (in-universe), p < 0.001, beats the fair baseline (0.37).

**Did not survive:**
- "R² = 0.74" (leak-inflated), "n = 182" (effective ≈ 91), "beats persistence by 0.94" (strawman),
  "forecasts growth over time" (within-package skill is null), and reliability at the top of the list.

---

## 6. Normalization ablation

> Asked: "make sure all columns are normalized." Answer: for gradient-boosted trees, per-feature
> scaling is a **no-op** — proven, not asserted.

| feature treatment | R² | Spearman | size-feature importance |
|---|--:|--:|--:|
| raw (14 features) | 0.582 | 0.790 | 34 % |
| per-feature z-score (StandardScaler) | 0.577 | 0.783 | — |
| cross-sectional z-score (per date) | 0.571 | 0.787 | 32 % |
| momentum-only (drop 5 size features) | 0.429 | 0.653 | 0 % |
| **within-package z-score** | **−0.310** | **−0.144** | 31 % |

Standardization changes nothing (trees split on thresholds; ~0.005 is binning jitter).
Cross-sectional z-scoring is also ~a no-op because it preserves per-date *rank*. The
**within-package** collapse (R² −0.31) is the clincher: strip cross-package information and skill
vanishes — independent confirmation that the signal is cross-sectional, not temporal.

---

## 7. What it would take to claim *temporal* forecasting skill

The blocker is structural: PyPI/pepy endpoints expose only ~180 days of history, so origins can't
span > 2× the 70-day horizon today. A credible temporal claim needs:
1. **Origin span > 140 days** → multiple 70-day-embargoed, non-overlapping folds.
2. Because history is API-capped at ~180 days, this requires **accumulating our own daily snapshots
   over months** — roughly one new independent 70-day forecast per 70 days collected, so a 3–4-origin
   temporal evaluation is **~6–9 months out** from when daily snapshotting begins.
3. Track per-origin **Spearman / precision@10** as horizons close — not pooled R² — and alarm if a
   calibrated baseline ever wins a closed cohort.

---

## 8. How this report was produced

Two layers: a deterministic harness ([`validate_growth.py`](../pipeline/scripts/validate_growth.py))
and an **LLM statistician panel** (12 agents) that ran adversarially on top of it:

- **4 auditors** red-teamed the methodology — they found the **shared-package leak** and the
  **strawman persistence baseline** the first harness missed.
- **3 reproducers** rebuilt the numbers *from scratch* (raw DuckDB / the dumped predictions, without
  importing the harness) and confirmed the leak-free point estimates to full float precision.
- **4 interpreters** (frequentist, forecasting-econometrics, ML-practitioner, skeptic) framed the
  defensible claim and the not-supported claims.

Their findings were then **re-verified in the harness** (`package_disjoint_cv`,
`calibrated_persistence`, `head_reliability`) — every panel claim reproduced. This is the headline
practice on display: *no metric is trusted until an independent process reproduces it.*

---

## 9. The one claim we stand behind

> After removing two leaks (centered-MA lookahead, +0.12 R²; shared-package memorisation, +0.22 R²),
> the OSS Radar growth model has genuine **cross-sectional** skill at rank-ordering 70-day forward
> download growth across Python/AI packages: **unseen-package Spearman ≈ 0.68** (in-universe ≈ 0.79,
> 95% CI [0.65, 0.89]), permutation **p < 0.001**, beating a fairly-calibrated baseline (0.37). The
> independent sample is **~91 packages at one April-2026 origin** — it is a *ranked momentum
> watchlist*, **not** a validated time-forward forecast, and the honest held-out R² is **≈ 0.36, not
> 0.74**.

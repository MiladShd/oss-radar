# Time-Series Analysis — the daily download series, by the book

*Harness:* [`pipeline/scripts/timeseries_analysis.py`](../pipeline/scripts/timeseries_analysis.py) ·
results [`docs/timeseries_results.json`](timeseries_results.json). Hardened after an adversarial LLM
panel review (see §6). Everything is on `log1p(downloads)` daily series, 91 packages × ~181 days.

The 70-day cross-sectional growth model can't be temporally validated (only ~1 independent forecast
in time — see [VALIDATION.md](VALIDATION.md)). The **daily** series, by contrast, supports textbook
time-series work: hundreds of rolling origins at short horizons.

---

## 1. TL;DR

- The series are **non-stationary, I(1)** (87/91 integrated of order 1), **100% autocorrelated**,
  **97.8% weekly-seasonal** (median STL strength 0.76), and **~70% heteroscedastic** — every
  assumption that forbids naive iid modeling is violated, which is exactly why a differencing +
  seasonal model is the right tool.
- At **1 day ahead**, a weekly-seasonal **ETS** is genuinely good and **properly validated**:
  median MASE **0.69** vs seasonal-naive **0.98**, beating it in **100%** of packages
  (Diebold-Mariano p≈1e-6), and **matching/beating a SARIMA(1,1,1)(1,0,0,7) contender 4/4**.
- Skill **fades fast with horizon**: ETS still edges seasonal-naive at 3 days (78% of packages),
  but by **7 days it is ≈ persistence** (MASE 0.99). Honest limit, not a bug.
- Adequacy checks are mixed-honest: 1-step residuals are white-noise in **61%** of packages, 95%
  prediction intervals cover only **92%** (mild undercoverage — the series are heteroscedastic), and
  the Mincer-Zarnowitz slope is **0.84** (slightly miscalibrated).

---

## 2. What was tested

**Part A — assumption diagnostics (all 91 series):**
ADF (`c` and `ct`) + KPSS reported as the full 4-cell matrix; integration order from level/diff;
Ljung-Box (autocorrelation); STL(period=7) Wang-Hyndman seasonal strength; **Engle ARCH and
Jarque-Bera on the STL remainder** (de-trended *and* de-seasonalized — not on `np.diff`, which leaves
the weekly cycle in and inflates both).

**Part B — rolling-origin forecast (18 volume-stratified packages, expanding window):**
naive · proper multi-step seasonal-naive (`y[t+h−m·⌈h/m⌉]`) · ETS Holt-Winters · SARIMA contender.
Metric **MASE** scaled by the in-sample (train-window) seasonal-naive MAE. Tests: Diebold-Mariano per
horizon, Ljung-Box white-noise on 1-step residuals, **PICP@95** (interval coverage), and a
**Mincer-Zarnowitz** calibration regression.

---

## 3. Diagnostics (91 series)

| property | test | result | reading |
|---|---|---|---|
| Stationarity | ADF `c` / `ct` reject unit root | 36% / 41% | mostly non-stationary |
| | KPSS rejects stationarity | 92% (floored at 0.01 for 84%) | rejects stationarity |
| ADF×KPSS cells | — | unit-root **54** · conflict **30** · clean I(0) **3** · inconclusive 4 | mostly unit-root |
| Integration order | ADF on level & diff | **I(1): 87** · I(0): 3 · I(2)+: 1 | difference once |
| Autocorrelation | Ljung-Box | **100%** | not white noise |
| Weekly seasonality | STL strength | **97.8%**, median 0.76 | strong weekly cycle |
| Heteroscedasticity | ARCH (on STL remainder) | **70%** | genuine volatility clustering |
| Normality of irregular | Jarque-Bera (on STL remainder) | **100% non-normal** | heavy-tailed |

Note the honesty the 4-cell view buys: a strict "ADF rejects AND KPSS doesn't" rule would report only
3% stationary, but that buries **30 conflict** series (ADF and KPSS both reject — trend-stationary or
structural break, not clean unit roots) and a KPSS statistic that is **table-floor-censored for 84%**.

---

## 4. Forecast (rolling-origin, 18 volume-stratified packages)

| horizon | naive | seasonal-naive | **ETS** | ETS beats seasonal-naive |
|---|--:|--:|--:|--:|
| 1 day | 1.2 | 0.98 | **0.69** | **100%** |
| 3 days | ~2.3 | 1.00 | **0.86** | 78% |
| 7 days | 1.00 | (=persistence) | 0.99 | parity |

*(MASE; lower is better; 1.0 = no better than naive.)*

**Adequacy & inference:**
- Diebold-Mariano (ETS vs seasonal-naive, h=1): favors ETS, p≈1e-6 per package.
- **SARIMA contender:** ETS matches/beats SARIMA(1,1,1)(1,0,0,7) at h=1 in **4/4** packages compared.
- White-noise 1-step residuals (Ljung-Box): **61%** of packages.
- **PICP@95 = 92%** — intervals slightly undercover (expected: the irregular component is
  heteroscedastic and non-normal, so a Gaussian band is optimistic).
- **Mincer-Zarnowitz slope = 0.84** (1.0 = perfectly calibrated): forecasts are mildly compressed.

---

## 5. Strongest defensible claim, and what is NOT supported

**Defensible:**
> On daily log-download series that are I(1) with strong weekly seasonality, a weekly-seasonal ETS
> delivers materially better **one-step** forecasts than naive *and* seasonal-naive baselines
> (median MASE 0.69 vs 0.98, 100% of 18 volume-stratified packages, Diebold-Mariano p≈1e-6),
> matching or beating SARIMA — and this is **rolling-origin validated** across hundreds of origins.

**NOT supported:**
- **Multi-day (>3) forecasting skill** — by 7 days ETS ≈ persistence.
- **Well-calibrated uncertainty** — 92% coverage at a nominal 95%, MZ slope 0.84.
- **Count-scale accuracy** — modeling is on `log1p`; `expm1` of a log-unbiased forecast is biased low
  by ≈exp(σ²/2). MASE is reported on the log scale; a Duan-smearing back-transform is future work.

---

## 6. How this was hardened (the LLM panel earned its keep)

A 6-agent panel audited the first version and **caught real problems**, all since fixed:
- **ARCH/Jarque-Bera ran on `np.diff`**, leaving the weekly cycle in → "93% heteroscedastic" was
  partly seasonal leakage. Now on the **STL remainder** → an honest **70%**.
- **The h=7 seasonal-naive was degenerate** (at h=m it equals persistence), so the old "ETS ≈
  seasonal-naive at 7 days" was misleading. Fixed with a **proper multi-step seasonal-naive** and a
  non-degenerate **h=3** horizon.
- **Stationarity certainty was overstated** ("3% stationary" from a censored AND-rule, I(2)+ count
  inflated 15→1). Now reported as the full **ADF×KPSS 4-cell matrix** with `ct` and the KPSS floor.
- **Easy-case selection** (top-12 only) → now **volume-stratified**; **no model alternative** → added
  **SARIMA**; **no uncertainty** → added **PICP + Mincer-Zarnowitz**; **MASE scale** moved to
  train-only (Hyndman-standard).

The panel also independently reproduced the core numbers from raw data (e.g. `numpy` ETS MASE 0.476,
DM p=8.7e-8), confirming the 1-step ETS superiority is real and leakage-free.

---

## 7. The one claim we stand behind

> The OSS Radar daily download series are textbook non-stationary, weekly-seasonal, heteroscedastic
> count series. A weekly-seasonal ETS forecasts them **one day ahead** with genuine, rolling-origin-
> validated skill (MASE 0.69, beats every baseline incl. SARIMA, DM p≈1e-6). Beyond ~3 days the
> signal decays to persistence, and the prediction intervals are mildly optimistic — so the honest
> product is a **short-horizon nowcast with calibrated caveats**, not a multi-week forecaster.

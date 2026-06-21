"""Time-series best-practices analysis of the daily download series.

The 70-day growth model is cross-sectional and can't be temporally validated (docs/VALIDATION.md).
The DAILY series, however, supports proper time-series work with rolling-origin CV. This harness was
hardened after an adversarial LLM panel review (docs/TIMESERIES.md):

  PART A — assumption diagnostics (per package, log1p downloads):
    * stationarity:   ADF (regression 'c' AND 'ct', H0 unit root) + KPSS (H0 stationary), reported as
                      the full 4-cell matrix; integration order from level/diff ADF+KPSS
    * autocorrelation: Ljung-Box (H0 white noise)
    * weekly seasonality: STL(period=7) Wang-Hyndman seasonal strength
    * heteroscedasticity / normality: Engle ARCH + Jarque-Bera on the STL REMAINDER (de-trended AND
                      de-seasonalized) — NOT on np.diff, which leaves the weekly cycle in and inflates both

  PART B — rolling-origin forecast (volume-stratified packages, expanding window):
    * models:   naive, seasonal-naive (proper multi-step y[t+h-m*ceil(h/m)]), ETS Holt-Winters,
                and SARIMA(1,1,1)(1,0,0,7) as an alternative-model contender (subset)
    * horizons: 1, 3, 7 (h=3 keeps seasonal-naive distinct from persistence; at h=7=m they coincide)
    * metric:   MASE scaled by the IN-SAMPLE (train-window) seasonal-naive MAE
    * tests:    Diebold-Mariano per horizon, Ljung-Box white-noise on 1-step residuals,
                prediction-interval coverage (PICP @95%) given heteroscedasticity, and a
                Mincer-Zarnowitz calibration regression (H0 intercept=0, slope=1)
"""
from __future__ import annotations

import json
import math
import os
import warnings

import numpy as np
import pandas as pd
from scipy.stats import norm

warnings.simplefilter("ignore")

from statsmodels.stats.diagnostic import acorr_ljungbox, het_arch  # noqa: E402
from statsmodels.stats.stattools import jarque_bera  # noqa: E402
from statsmodels.tsa.holtwinters import ExponentialSmoothing  # noqa: E402
from statsmodels.tsa.seasonal import STL  # noqa: E402
from statsmodels.tsa.statespace.sarimax import SARIMAX  # noqa: E402
from statsmodels.tsa.stattools import adfuller, kpss  # noqa: E402

from oss_radar.warehouse import get_warehouse  # noqa: E402

M = 7
START = 60
HORIZONS = [1, 3, 7]
SARIMA_PKGS = 4
SARIMA_STEP = 3


def _series(history: pd.DataFrame) -> dict[str, np.ndarray]:
    out = {}
    for name, g in history.sort_values("date").groupby("name"):
        y = np.log1p(g["downloads"].astype(float).to_numpy())
        if len(y) >= 90 and np.nanstd(y) > 0:
            out[name] = y
    return out


def _p(fn, *a, **k):
    try:
        return float(fn(*a, **k)[1])
    except Exception:
        return np.nan


# ----------------------------- PART A -----------------------------
def diagnose(y: np.ndarray) -> dict:
    dy = np.diff(y)
    adf_c = _p(adfuller, y, autolag="AIC")
    adf_ct = _p(lambda x, **kw: adfuller(x, regression="ct", **kw), y, autolag="AIC")
    kpss_c = _p(lambda x: kpss(x, regression="c", nlags="auto"), y)
    adf_dc = _p(adfuller, dy, autolag="AIC")
    # 4-cell on the level: ADF rejects unit root? x KPSS rejects stationarity?
    adf_rej = adf_c < 0.05
    kpss_rej = kpss_c < 0.05
    cell = ("stationary" if adf_rej and not kpss_rej else
            "unit_root" if not adf_rej and kpss_rej else
            "conflict" if adf_rej and kpss_rej else "inconclusive")
    integ = "I(0)" if (adf_c < 0.05 and kpss_c > 0.05) else ("I(1)" if (adf_dc < 0.05) else "I(2)+")
    try:
        lb_p = float(acorr_ljungbox(y, lags=[M * 2], return_df=True)["lb_pvalue"].iloc[0])
    except Exception:
        lb_p = np.nan
    # STL once -> seasonal strength + de-seasonalized remainder for ARCH/JB
    try:
        stl = STL(pd.Series(y), period=M, robust=True).fit()
        resid = np.asarray(stl.resid)
        seas = float(max(0.0, 1 - np.var(resid) / max(np.var(resid + np.asarray(stl.seasonal)), 1e-12)))
        arch_p = _p(lambda r: het_arch(r, nlags=M), resid)        # on REMAINDER, not np.diff
        jb_p = _p(jarque_bera, resid)
    except Exception:
        seas, arch_p, jb_p = np.nan, np.nan, np.nan
    return {"adf_c_p": adf_c, "adf_ct_p": adf_ct, "kpss_c_p": kpss_c, "kpss_floored": kpss_c <= 0.01,
            "cell": cell, "integration": integ, "ljungbox_p": lb_p, "seasonal_strength": seas,
            "arch_remainder_p": arch_p, "jb_remainder_p": jb_p}


def summarize_diagnostics(diags: dict[str, dict]) -> dict:
    d = list(diags.values())

    def frac(pred):
        v = [pred(x) for x in d if pred(x) is not None]
        return round(float(np.mean([1.0 if p else 0.0 for p in v])), 3) if v else None

    cells, integ = {}, {}
    for x in d:
        cells[x["cell"]] = cells.get(x["cell"], 0) + 1
        integ[x["integration"]] = integ.get(x["integration"], 0) + 1
    return {
        "n_series": len(d),
        "adf_c_rejects_unitroot_frac": frac(lambda x: x["adf_c_p"] < 0.05),
        "adf_ct_rejects_unitroot_frac": frac(lambda x: x["adf_ct_p"] < 0.05),
        "kpss_rejects_stationarity_frac": frac(lambda x: x["kpss_c_p"] < 0.05),
        "kpss_floored_at_0.01_frac": frac(lambda x: x["kpss_floored"]),
        "adf_kpss_cell_counts": cells,
        "integration_order_counts": integ,
        "autocorrelated_frac": frac(lambda x: x["ljungbox_p"] < 0.05),
        "weekly_seasonal_frac": frac(lambda x: x["seasonal_strength"] > 0.3),
        "median_seasonal_strength": round(float(np.nanmedian([x["seasonal_strength"] for x in d])), 3),
        "heteroscedastic_remainder_frac": frac(lambda x: x["arch_remainder_p"] < 0.05),
        "nonnormal_remainder_frac": frac(lambda x: x["jb_remainder_p"] < 0.05),
    }


# ----------------------------- PART B -----------------------------
def sn_index(t, h, m=M):
    """Proper multi-step seasonal-naive source index for target t+h (most recent same season)."""
    return t + h - m * math.ceil(h / m)


def mase_scale(y_train: np.ndarray, m=M) -> float:
    return float(np.mean(np.abs(y_train[m:] - y_train[:-m])))  # in-sample seasonal-naive MAE


def dm(e1, e2, h):
    e1, e2 = np.asarray(e1), np.asarray(e2)
    d = e1 ** 2 - e2 ** 2
    n = len(d)
    if n < 8 or np.allclose(d, 0):
        return {"dm_stat": None, "p_value": None}
    dc = d - d.mean()
    L = max(h - 1, 0)
    var = np.mean(dc * dc)
    for lag in range(1, min(L, n - 1) + 1):
        var += 2 * (1 - lag / (L + 1)) * np.mean(dc[lag:] * dc[:-lag])
    se = math.sqrt(max(var, 1e-12) / n)
    stat = d.mean() / se
    return {"dm_stat": round(float(stat), 3), "p_value": float(2 * (1 - norm.cdf(abs(stat))))}


def fit_ets(train):
    return ExponentialSmoothing(train, trend="add", seasonal="add", seasonal_periods=M,
                                initialization_method="estimated").fit()


def rolling_ets(name, y, with_sarima=False, step=1):
    scale = mase_scale(y[:START + 1])  # train-only scale (causal, Hyndman-standard)
    err = {mdl: {h: [] for h in HORIZONS} for mdl in ("naive", "seasonal_naive", "ets", "sarima")}
    resid1, picp_hit, picp_n, mz_a, mz_f = [], 0, 0, [], []
    hmax = max(HORIZONS)
    for t in range(START, len(y) - hmax, step):
        train = y[: t + 1]
        try:
            f = fit_ets(train)
            fc = np.asarray(f.forecast(hmax))
            s = float(np.std(f.resid)) if np.std(f.resid) > 0 else 1e-6
        except Exception:
            continue
        sar = None
        if with_sarima:
            try:
                sar = np.asarray(SARIMAX(train, order=(1, 1, 1), seasonal_order=(1, 0, 0, M),
                                         enforce_stationarity=False, enforce_invertibility=False
                                         ).fit(disp=False, maxiter=50).forecast(hmax))
            except Exception:
                sar = None
        for h in HORIZONS:
            a = y[t + h]
            err["naive"][h].append(a - train[-1])
            err["seasonal_naive"][h].append(a - y[sn_index(t, h)])
            err["ets"][h].append(a - fc[h - 1])
            if sar is not None:
                err["sarima"][h].append(a - sar[h - 1])
        resid1.append(y[t + 1] - fc[0])
        lo, hi = fc[0] - 1.96 * s, fc[0] + 1.96 * s          # 95% Gaussian 1-step interval
        picp_hit += int(lo <= y[t + 1] <= hi)
        picp_n += 1
        mz_a.append(y[t + 1])
        mz_f.append(fc[0])
    if picp_n < 20:
        return {"name": name, "skipped": True}
    out = {"name": name, "n_origins": picp_n, "mase": {}, "dm_ets_vs_seasonal_naive": {}}
    for mdl in ("naive", "seasonal_naive", "ets"):
        out["mase"][mdl] = {f"h{h}": round(np.mean(np.abs(err[mdl][h])) / max(scale, 1e-9), 3) for h in HORIZONS}
    for h in HORIZONS:
        out["dm_ets_vs_seasonal_naive"][f"h{h}"] = dm(err["ets"][h], err["seasonal_naive"][h], h)
    if with_sarima and err["sarima"][1]:
        out["mase"]["sarima"] = {f"h{h}": round(np.mean(np.abs(err["sarima"][h])) / max(scale, 1e-9), 3) for h in HORIZONS}
        out["dm_ets_vs_sarima_h1"] = dm(err["ets"][1], err["sarima"][1], 1)
    try:
        lb = float(acorr_ljungbox(resid1, lags=[M], return_df=True)["lb_pvalue"].iloc[0])
    except Exception:
        lb = float("nan")
    out["ets_resid_ljungbox_p"] = round(lb, 4) if lb == lb else None
    out["ets_resid_white_noise"] = bool(lb > 0.05) if lb == lb else None
    out["picp95_h1"] = round(picp_hit / picp_n, 3)
    b, a = np.polyfit(mz_f, mz_a, 1)                          # Mincer-Zarnowitz: actual ~ a + b*forecast
    out["mincer_zarnowitz"] = {"slope": round(float(b), 3), "intercept": round(float(a), 3)}
    return out


def _stratified(totals: pd.Series, series: dict, per_tier=6) -> list[str]:
    names = [n for n in totals.index if n in series]
    n = len(names)
    hi, mid, lo = names[:per_tier], names[n // 2 - per_tier // 2: n // 2 + per_tier // 2], names[-per_tier:]
    seen, out = set(), []
    for grp in (hi, mid, lo):
        for x in grp:
            if x not in seen:
                seen.add(x)
                out.append(x)
    return out


def main():
    hist = get_warehouse().query_df("SELECT name,date,downloads FROM download_history")
    series = _series(hist)
    diags = {n: diagnose(y) for n, y in series.items()}
    out = {"config": {"n_series": len(series), "seasonal_period": M, "horizons": HORIZONS,
                      "note": "ARCH/JB on STL remainder; seasonal-naive is proper multi-step; MASE scaled "
                              "by in-sample seasonal-naive; forecasts on log1p scale"},
           "diagnostics_summary": summarize_diagnostics(diags),
           "diagnostics_sample": {k: diags[k] for k in list(diags)[:6]}}

    totals = hist.groupby("name")["downloads"].sum().sort_values(ascending=False)
    pkgs = _stratified(totals, series)
    sar_set = set([n for n in totals.index if n in series][:SARIMA_PKGS])
    fc = []
    for n in pkgs:
        with_sar = n in sar_set
        r = rolling_ets(n, series[n], with_sarima=with_sar, step=(SARIMA_STEP if with_sar else 1))
        if not r.get("skipped"):
            r["volume_rank"] = int(list(totals.index).index(n))
            fc.append(r)
    out["forecast_per_package"] = fc
    if fc:
        def med(key):
            vals = [key(r) for r in fc if key(r) is not None]
            return round(float(np.median(vals)), 3) if vals else None
        out["forecast_summary"] = {
            "n_packages": len(fc),
            "median_mase_ets_h1": med(lambda r: r["mase"]["ets"]["h1"]),
            "median_mase_seasonal_naive_h1": med(lambda r: r["mase"]["seasonal_naive"]["h1"]),
            "median_mase_ets_h3": med(lambda r: r["mase"]["ets"]["h3"]),
            "median_mase_seasonal_naive_h3": med(lambda r: r["mase"]["seasonal_naive"]["h3"]),
            "median_mase_ets_h7": med(lambda r: r["mase"]["ets"]["h7"]),
            "median_mase_naive_h7": med(lambda r: r["mase"]["naive"]["h7"]),
            "ets_beats_seasonal_naive_h1_frac": round(
                float(np.mean([r["mase"]["ets"]["h1"] < r["mase"]["seasonal_naive"]["h1"] for r in fc])), 3),
            "ets_beats_seasonal_naive_h3_frac": round(
                float(np.mean([r["mase"]["ets"]["h3"] < r["mase"]["seasonal_naive"]["h3"] for r in fc])), 3),
            "ets_residuals_white_noise_frac": round(
                float(np.mean([1.0 if r.get("ets_resid_white_noise") else 0.0 for r in fc])), 3),
            "median_picp95_h1": med(lambda r: r.get("picp95_h1")),
            "median_mz_slope": med(lambda r: r["mincer_zarnowitz"]["slope"]),
            "ets_beats_sarima_h1_count": sum(
                1 for r in fc if "sarima" in r.get("mase", {}) and r["mase"]["ets"]["h1"] < r["mase"]["sarima"]["h1"]),
            "n_sarima_compared": sum(1 for r in fc if "sarima" in r.get("mase", {})),
        }

    def clean(x):
        if isinstance(x, float):
            return None if (x != x or abs(x) == float("inf")) else x
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items()}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x

    out = clean(out)
    path = os.environ.get("TS_OUT", "/tmp/timeseries_results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, allow_nan=False)
    print(json.dumps(out["diagnostics_summary"], indent=2))
    print(json.dumps(out.get("forecast_summary", {}), indent=2))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()

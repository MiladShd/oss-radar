"""Time-series best-practices analysis of the daily download series.

The 70-day growth model is cross-sectional and can't be temporally validated (see
docs/VALIDATION.md). This harness instead does proper time-series work on the underlying
DAILY series, where rolling-origin CV *is* feasible:

  PART A — assumption diagnostics (per package, log1p downloads):
    * stationarity:        ADF (H0 unit root) + KPSS (H0 stationary) -> integration order
    * autocorrelation:     Ljung-Box (H0 white noise)
    * weekly seasonality:  STL(period=7) seasonal strength + ACF(7)
    * heteroscedasticity:  Engle ARCH test (H0 homoscedastic)
    * normality:           Jarque-Bera (H0 normal) on first differences

  PART B — rolling-origin forecast eval (top packages by volume, expanding window):
    * models:   naive, seasonal-naive (lag 7), ETS Holt-Winters (additive trend+weekly)
    * metric:   MASE (scaled by in-sample seasonal-naive) at h=1 and h=7 — best-practice,
                scale-free, comparable across packages
    * test:     Diebold-Mariano (ETS vs seasonal-naive) and Ljung-Box on 1-step residuals
                (white-noise residuals => the model captured the structure)
"""
from __future__ import annotations

import json
import os
import warnings

import numpy as np
import pandas as pd
from scipy.stats import norm

warnings.simplefilter("ignore")  # statsmodels p-value-out-of-range + convergence chatter

from statsmodels.stats.diagnostic import acorr_ljungbox, het_arch  # noqa: E402
from statsmodels.stats.stattools import jarque_bera  # noqa: E402
from statsmodels.tsa.holtwinters import ExponentialSmoothing  # noqa: E402
from statsmodels.tsa.seasonal import STL  # noqa: E402
from statsmodels.tsa.stattools import adfuller, kpss  # noqa: E402

from oss_radar.warehouse import get_warehouse  # noqa: E402

M = 7  # weekly seasonality
START = 60  # min history before the first rolling-origin forecast
HORIZONS = [1, 7]


def _series(history: pd.DataFrame) -> dict[str, np.ndarray]:
    out = {}
    for name, g in history.sort_values("date").groupby("name"):
        y = np.log1p(g["downloads"].astype(float).to_numpy())
        if len(y) >= 90 and np.nanstd(y) > 0:
            out[name] = y
    return out


# ----------------------------- PART A: diagnostics -----------------------------
def _p_adf(x):
    try:
        return float(adfuller(x, autolag="AIC")[1])
    except Exception:
        return np.nan


def _p_kpss(x):
    try:
        return float(kpss(x, regression="c", nlags="auto")[1])
    except Exception:
        return np.nan


def diagnose(y: np.ndarray) -> dict:
    dy = np.diff(y)
    adf_p, kpss_p = _p_adf(y), _p_kpss(y)
    adf_dp, kpss_dp = _p_adf(dy), _p_kpss(dy)
    # integration order from the ADF+KPSS pair
    level_stat = (adf_p < 0.05) and (kpss_p > 0.05)
    diff_stat = (adf_dp < 0.05) and (kpss_dp > 0.05)
    integ = "I(0)" if level_stat else ("I(1)" if diff_stat else "I(2)+")
    try:
        lb_p = float(acorr_ljungbox(y, lags=[M * 2], return_df=True)["lb_pvalue"].iloc[0])
    except Exception:
        lb_p = np.nan
    try:
        stl = STL(pd.Series(y), period=M, robust=True).fit()
        seas_strength = float(max(0.0, 1 - np.var(stl.resid) / max(np.var(stl.resid + stl.seasonal), 1e-12)))
    except Exception:
        seas_strength = np.nan
    try:
        arch_p = float(het_arch(dy, nlags=M)[1])
    except Exception:
        arch_p = np.nan
    try:
        jb_p = float(jarque_bera(dy)[1])
    except Exception:
        jb_p = np.nan
    return {"adf_p": adf_p, "kpss_p": kpss_p, "integration": integ, "ljungbox_p": lb_p,
            "seasonal_strength": seas_strength, "arch_p": arch_p, "jarque_bera_p": jb_p}


def summarize_diagnostics(diags: dict[str, dict]) -> dict:
    d = list(diags.values())
    frac = lambda pred: round(float(np.mean([1.0 if pred(x) else 0.0 for x in d])), 3)
    med = lambda key: round(float(np.nanmedian([x[key] for x in d])), 4)
    integ_counts = {}
    for x in d:
        integ_counts[x["integration"]] = integ_counts.get(x["integration"], 0) + 1
    return {
        "n_series": len(d),
        "level_stationary_frac": frac(lambda x: x["adf_p"] < 0.05 and x["kpss_p"] > 0.05),
        "integration_order_counts": integ_counts,
        "autocorrelated_frac": frac(lambda x: x["ljungbox_p"] < 0.05),
        "weekly_seasonal_frac": frac(lambda x: x["seasonal_strength"] > 0.3),
        "median_seasonal_strength": med("seasonal_strength"),
        "heteroscedastic_frac": frac(lambda x: x["arch_p"] < 0.05),
        "nonnormal_diff_frac": frac(lambda x: x["jarque_bera_p"] < 0.05),
        "median_adf_p_level": med("adf_p"),
    }


# ----------------------------- PART B: rolling-origin forecast -----------------------------
def mase(errs: np.ndarray, y_train_full: np.ndarray, m=M) -> float:
    scale = np.mean(np.abs(y_train_full[m:] - y_train_full[:-m]))  # in-sample seasonal-naive MAE
    return float(np.mean(np.abs(errs)) / max(scale, 1e-9))


def diebold_mariano(e1: np.ndarray, e2: np.ndarray, h=1) -> dict:
    d = e1 ** 2 - e2 ** 2
    n = len(d)
    dbar = d.mean()
    dc = d - dbar
    L = max(h - 1, 0)
    var = np.mean(dc * dc)
    for lag in range(1, L + 1):
        var += 2 * (1 - lag / (L + 1)) * np.mean(dc[lag:] * dc[:-lag])
    se = np.sqrt(max(var, 1e-12) / n)
    dm = dbar / se
    return {"dm_stat": float(dm), "p_value": float(2 * (1 - norm.cdf(abs(dm)))),
            "favored": "ETS" if dbar < 0 else "seasonal_naive"}


def rolling_eval(name: str, y: np.ndarray) -> dict:
    errs = {"naive": {h: [] for h in HORIZONS},
            "seasonal_naive": {h: [] for h in HORIZONS},
            "ets": {h: [] for h in HORIZONS}}
    resid_ets_1 = []
    hmax = max(HORIZONS)
    for t in range(START, len(y) - hmax):
        train = y[: t + 1]
        try:
            fc = ExponentialSmoothing(train, trend="add", seasonal="add",
                                      seasonal_periods=M, initialization_method="estimated"
                                      ).fit().forecast(hmax)
        except Exception:
            continue
        for h in HORIZONS:
            actual = y[t + h]
            errs["naive"][h].append(actual - train[-1])
            errs["seasonal_naive"][h].append(actual - y[t + h - M])  # value 7 days before target
            errs["ets"][h].append(actual - fc[h - 1])
        resid_ets_1.append(y[t + 1] - fc[0])

    if len(resid_ets_1) < 20:
        return {"name": name, "n_origins": len(resid_ets_1), "skipped": True}

    out = {"name": name, "n_origins": len(resid_ets_1), "mase": {}}
    for model in errs:
        out["mase"][model] = {f"h{h}": round(mase(np.array(errs[model][h]), y), 3) for h in HORIZONS}
    out["dm_ets_vs_seasonal_naive_h1"] = diebold_mariano(
        np.array(errs["ets"][1]), np.array(errs["seasonal_naive"][1]), h=1)
    try:
        lb = float(acorr_ljungbox(resid_ets_1, lags=[M], return_df=True)["lb_pvalue"].iloc[0])
    except Exception:
        lb = np.nan
    out["ets_resid_ljungbox_p"] = round(lb, 4)
    out["ets_resid_white_noise"] = bool(lb > 0.05) if lb == lb else None
    return out


def main():
    hist = get_warehouse().query_df("SELECT name,date,downloads FROM download_history")
    series = _series(hist)

    # PART A — diagnostics on every series
    diags = {name: diagnose(y) for name, y in series.items()}
    out = {"config": {"n_series": len(series), "seasonal_period": M, "horizons": HORIZONS},
           "diagnostics_summary": summarize_diagnostics(diags),
           "diagnostics_sample": {k: diags[k] for k in list(diags)[:8]}}

    # PART B — rolling-origin forecast on the top packages by volume
    totals = hist.groupby("name")["downloads"].sum().sort_values(ascending=False)
    top = [n for n in totals.index if n in series][:12]
    fc = [rolling_eval(n, series[n]) for n in top]
    fc = [r for r in fc if not r.get("skipped")]
    out["forecast_per_package"] = fc
    if fc:
        ets1 = [r["mase"]["ets"]["h1"] for r in fc]
        sn1 = [r["mase"]["seasonal_naive"]["h1"] for r in fc]
        ets7 = [r["mase"]["ets"]["h7"] for r in fc]
        sn7 = [r["mase"]["seasonal_naive"]["h7"] for r in fc]
        out["forecast_summary"] = {
            "n_packages": len(fc),
            "median_mase_ets_h1": round(float(np.median(ets1)), 3),
            "median_mase_seasonal_naive_h1": round(float(np.median(sn1)), 3),
            "median_mase_ets_h7": round(float(np.median(ets7)), 3),
            "median_mase_seasonal_naive_h7": round(float(np.median(sn7)), 3),
            "ets_beats_seasonal_naive_h1_frac": round(float(np.mean([a < b for a, b in zip(ets1, sn1)])), 3),
            "ets_residuals_white_noise_frac": round(
                float(np.mean([1.0 if r.get("ets_resid_white_noise") else 0.0 for r in fc])), 3),
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
    print(json.dumps(out, indent=2, allow_nan=False))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()

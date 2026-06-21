"""Rigorous statistical validation of the growth model's held-out R^2 claim.

This harness exists to STRESS-TEST, not flatter, the headline number. It:
  1. Quantifies the centered-smoother lookahead leak (centered vs causal/trailing MA).
  2. Compares the model against naive baselines (persistence, zero, train-mean) and
     reports skill scores (does it beat persistence?).
  3. Runs PURGED expanding-window walk-forward CV (a >=horizon embargo between train and
     test removes target-window overlap across the split) and reports the fold spread.
  4. Puts CONFIDENCE INTERVALS on R^2 / Spearman via a CLUSTER bootstrap that resamples
     whole packages (the real independent units), respecting within-series autocorrelation.
  5. Tests significance two ways: a package-block permutation p-value (null = no skill) and
     a Diebold-Mariano test (model vs persistence) with a Newey-West HAC variance that
     accounts for the 70-day overlapping forecasts.
  6. Decomposes skill into BETWEEN-package (cross-sectional) vs WITHIN-package (temporal).

Everything is causal unless explicitly labelled "centered (leaky)".
"""
from __future__ import annotations

import json
import os
from datetime import timedelta

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import norm, spearmanr
from sklearn.metrics import r2_score
from sklearn.model_selection import GroupKFold

from oss_radar.config.active_features import active_download_features
from oss_radar.features.engineering import _download_features, _window_sum
from oss_radar.warehouse import get_warehouse

HORIZON = 70
SMOOTH = 28
STRIDE = 3
MIN_HISTORY = 84
SEED = 42
FEATURES = active_download_features()
rng = np.random.default_rng(SEED)


# ----------------------------- series construction -----------------------------
def _raw_series(history: pd.DataFrame) -> dict[str, dict]:
    out = {}
    for name, g in history.groupby("name"):
        s = {}
        for d, dl in zip(g["date"], g["downloads"], strict=False):
            dd = d if hasattr(d, "year") else pd.to_datetime(d).date()
            s[dd] = float(dl)
        out[name] = s
    return out


def trailing_smooth(series: dict, k: int) -> dict:
    """Causal k-day moving average: smoothed[d] uses only days <= d."""
    out = {}
    for d in series:
        vals = [series[d - timedelta(days=o)] for o in range(0, k) if (d - timedelta(days=o)) in series]
        out[d] = sum(vals) / len(vals) if vals else series[d]
    return out


def centered_smooth(series: dict, k: int) -> dict:
    """The model's current (leaky) smoother: peeks +/- k//2 days."""
    half = k // 2
    out = {}
    for d in series:
        vals = [series[d + timedelta(days=o)] for o in range(-half, half + 1) if (d + timedelta(days=o)) in series]
        out[d] = sum(vals) / len(vals) if vals else series[d]
    return out


def build_xy(history: pd.DataFrame, smoother) -> pd.DataFrame:
    """Supervised rows. Target = log-growth of the forward-70d window vs the trailing-70d
    base. Persistence = the SAME quantity one horizon earlier (fully causal)."""
    rows = []
    for name, raw in _raw_series(history).items():
        sm = smoother(raw, SMOOTH)
        days = sorted(raw)
        if len(days) < MIN_HISTORY + HORIZON:
            continue
        cur = days[0] + timedelta(days=MIN_HISTORY)
        last = days[-1] - timedelta(days=HORIZON)
        while cur <= last:
            feats = _download_features(sm, cur)
            if feats:
                base = _window_sum(sm, cur, HORIZON)              # trailing 70d ending at t
                fwd = _window_sum(sm, cur + timedelta(days=HORIZON), HORIZON)  # next 70d
                prev = _window_sum(sm, cur - timedelta(days=HORIZON), HORIZON)  # trailing 70d ending at t-70
                if base > 0 and prev > 0:
                    feats["name"] = name
                    feats["t"] = cur
                    feats["y"] = np.log1p(fwd) - np.log1p(base)
                    feats["persistence"] = np.log1p(base) - np.log1p(prev)  # causal
                    rows.append(feats)
            cur += timedelta(days=STRIDE)
    return pd.DataFrame(rows)


# ----------------------------- model -----------------------------
def fit_predict(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    m = lgb.LGBMRegressor(
        n_estimators=400, learning_rate=0.03, num_leaves=31, subsample=0.8,
        subsample_freq=1, colsample_bytree=0.9, min_child_samples=30,
        random_state=SEED, n_jobs=-1, verbose=-1,
    )
    yclip = train["y"].clip(-0.9, 3.0)
    m.fit(train[FEATURES], yclip)
    return m.predict(test[FEATURES])


def time_split(df: pd.DataFrame, n_test_origins=2):
    """Hold out the latest N distinct forecast origins as the test set."""
    origins = np.sort(df["t"].unique())
    test_origins = set(origins[-n_test_origins:])
    te = df[df["t"].isin(test_origins)].copy()
    tr = df[~df["t"].isin(test_origins)].copy()
    return tr, te


def origin_diagnostics(df: pd.DataFrame) -> dict:
    """Can this dataset even support temporal cross-validation at this horizon?"""
    origins = np.sort(df["t"].unique())
    span = int((origins[-1] - origins[0]) / np.timedelta64(1, "D")) if len(origins) > 1 else 0
    return {
        "n_distinct_origins": int(len(origins)),
        "origin_span_days": span,
        "horizon_days": HORIZON,
        "purged_temporal_cv_feasible": bool(span > 2 * HORIZON),
        "independent_forecasts_in_time_approx": round(max(span, 1) / HORIZON, 2),
        "verdict": (
            "Temporal CV INFEASIBLE: every valid forecast origin falls inside a single "
            f"{span}-day window (< the {HORIZON}-day horizon), so all 70-day target windows "
            "overlap — the data contains ~1 independent forecast in time. Inference below is "
            "cross-sectional (across packages at one origin-cluster), NOT multi-origin temporal."
            if span <= HORIZON else
            "Limited temporal separation; purged folds may be thin."
        ),
    }


# ----------------------------- metrics & inference -----------------------------
def _metrics(y, yhat):
    rho = spearmanr(y, yhat).correlation if len(y) > 2 else np.nan
    return {"r2": float(r2_score(y, yhat)), "spearman": float(rho),
            "mae": float(np.mean(np.abs(y - yhat)))}


def cluster_bootstrap(test: pd.DataFrame, yhat: np.ndarray, B=2000):
    """Resample PACKAGES with replacement -> CI that respects within-series dependence."""
    t = test.reset_index(drop=True).copy()
    t["yhat"] = yhat
    groups = {n: idx.values for n, idx in t.groupby("name").groups.items()}
    names = list(groups)
    r2s, rhos, skills = [], [], []
    for _ in range(B):
        pick = rng.choice(names, size=len(names), replace=True)
        idx = np.concatenate([groups[n] for n in pick])
        s = t.iloc[idx]
        y, yh, per = s["y"].values, s["yhat"].values, s["persistence"].values
        if len(np.unique(y)) < 3:
            continue
        r2s.append(r2_score(y, yh))
        rhos.append(spearmanr(y, yh).correlation)
        skills.append(1 - np.mean((y - yh) ** 2) / max(np.mean((y - per) ** 2), 1e-9))
    def ci(a):
        return [float(np.percentile(a, 2.5)), float(np.percentile(a, 97.5))]
    return {"r2_ci": ci(r2s), "spearman_ci": ci(rhos), "skill_vs_persistence_ci": ci(skills),
            "r2_median": float(np.median(r2s)), "spearman_median": float(np.median(rhos))}


def block_permutation_p(test: pd.DataFrame, yhat: np.ndarray, B=2000):
    """Null = predictions carry no information. Permute whole-package target blocks so the
    autocorrelation structure is preserved under H0. p = P(null spearman >= observed)."""
    t = test.reset_index(drop=True).copy()
    t["yhat"] = yhat
    obs = spearmanr(t["y"], t["yhat"]).correlation
    blocks = [g["y"].values for _, g in t.groupby("name")]
    yhat_by_block = [g["yhat"].values for _, g in t.groupby("name")]
    ge = 0
    for _ in range(B):
        order = rng.permutation(len(blocks))
        y_perm = np.concatenate([blocks[i] for i in order])
        yh = np.concatenate(yhat_by_block)
        n = min(len(y_perm), len(yh))
        rho = spearmanr(y_perm[:n], yh[:n]).correlation
        if rho >= obs:
            ge += 1
    return {"observed_spearman": float(obs), "p_value": float((1 + ge) / (1 + B))}


def diebold_mariano(y, yhat_model, yhat_bench, h=HORIZON):
    """DM test on squared-error loss with Newey-West HAC variance (lag h-1) for the
    overlapping 70-day forecasts. d<0 => model has lower error than the benchmark."""
    e1 = (y - yhat_model) ** 2
    e2 = (y - yhat_bench) ** 2
    d = e1 - e2
    n = len(d)
    dbar = d.mean()
    dc = d - dbar
    L = min(h - 1, n - 1)
    gamma0 = np.mean(dc * dc)
    var = gamma0
    for lag in range(1, L + 1):
        w = 1 - lag / (L + 1)
        cov = np.mean(dc[lag:] * dc[:-lag])
        var += 2 * w * cov
    se = np.sqrt(max(var, 1e-12) / n)
    dm = dbar / se
    return {"dm_stat": float(dm), "p_value": float(2 * (1 - norm.cdf(abs(dm)))),
            "mean_loss_diff": float(dbar), "favored": "model" if dbar < 0 else "benchmark"}


def within_between(test: pd.DataFrame, yhat: np.ndarray):
    t = test.reset_index(drop=True).copy()
    t["yhat"] = yhat
    rows_per_pkg = t.groupby("name").size()
    btw = t.groupby("name").agg(y=("y", "mean"), p=("yhat", "mean"))
    res = {
        "r2_between": float(r2_score(btw["y"], btw["p"])) if len(btw) > 2 else None,
        "n_packages": int(t["name"].nunique()),
        "median_rows_per_package": int(rows_per_pkg.median()),
    }
    if rows_per_pkg.median() >= 3:
        gm_y = t.groupby("name")["y"].transform("mean")
        gm_p = t.groupby("name")["yhat"].transform("mean")
        res["r2_within"] = float(r2_score(t["y"] - gm_y, t["yhat"] - gm_p))
    else:
        res["r2_within"] = None
        res["within_note"] = ("insufficient temporal depth per package (~cross-sectional test); "
                              "within-package skill cannot be measured on this data.")
    return res


def unpurged_walk_forward(df: pd.DataFrame, n_folds=4):
    """NO embargo -> train and test 70-day windows overlap, so this is OPTIMISTICALLY BIASED.
    Reported only as a fold-to-fold stability check, never as an unbiased estimate."""
    dates = np.sort(df["t"].unique())
    bounds = np.linspace(0.5, 1.0, n_folds + 1)
    folds = []
    for k in range(n_folds):
        lo = dates[int(bounds[k] * (len(dates) - 1))]
        hi = dates[int(bounds[k + 1] * (len(dates) - 1))]
        tr = df[df["t"] <= lo]
        te = df[(df["t"] > lo) & (df["t"] <= hi)]
        if len(tr) < 100 or len(te) < 20:
            continue
        yhat = fit_predict(tr, te)
        m = _metrics(te["y"].values, yhat)
        folds.append({"fold": k + 1, "n_train": int(len(tr)), "n_test": int(len(te)),
                      "r2": m["r2"], "spearman": m["spearman"]})
    return folds


def purged_walk_forward(df: pd.DataFrame, n_folds=4):
    dates = np.sort(df["t"].unique())
    folds = []
    # expanding window: initial 50% train, then 4 equal forward test blocks
    bounds = np.linspace(0.5, 1.0, n_folds + 1)
    for k in range(n_folds):
        te_start_i = int(bounds[k] * (len(dates) - 1)) + 1
        te_end = dates[int(bounds[k + 1] * (len(dates) - 1))]
        te_start = dates[min(te_start_i, len(dates) - 1)]
        embargo = te_start - np.timedelta64(HORIZON, "D")          # purge target overlap
        tr = df[df["t"] <= embargo]
        te = df[(df["t"] >= te_start) & (df["t"] <= te_end)]
        if len(tr) < 100 or len(te) < 20:
            continue
        yhat = fit_predict(tr, te)
        bench = te["persistence"].values
        folds.append({
            "fold": k + 1, "n_train": int(len(tr)), "n_test": int(len(te)),
            "model": _metrics(te["y"].values, yhat),
            "persistence": _metrics(te["y"].values, bench),
            "skill_vs_persistence": float(1 - np.mean((te["y"].values - yhat) ** 2)
                                          / max(np.mean((te["y"].values - bench) ** 2), 1e-9)),
        })
    return folds


def package_disjoint_cv(df: pd.DataFrame, n_splits=5):
    """GroupKFold by package: train and test share NO packages. Answers 'does it generalize to
    a NEW package?' — removes the shared-package target-overlap leak the LLM panel flagged."""
    gkf = GroupKFold(n_splits=n_splits)
    y = df["y"].values
    oof = np.full(len(df), np.nan)
    for tr_idx, te_idx in gkf.split(df[FEATURES], y, df["name"].values):
        oof[te_idx] = fit_predict(df.iloc[tr_idx], df.iloc[te_idx])
    mask = ~np.isnan(oof)
    return {"r2": float(r2_score(y[mask], oof[mask])),
            "spearman": float(spearmanr(y[mask], oof[mask]).correlation),
            "n": int(mask.sum()), "n_splits": n_splits,
            "note": "train/test share zero packages -> honest 'unseen package' cross-sectional skill"}


def calibrated_persistence(tr: pd.DataFrame, te: pd.DataFrame):
    """A FAIR persistence baseline: regress y ~ a + b*persistence on train, apply to test.
    The raw persistence baseline is a strawman (biased +0.77 in level); this de-biases it."""
    b, a = np.polyfit(tr["persistence"].values, tr["y"].values, 1)
    pred = a + b * te["persistence"].values
    return {"r2": float(r2_score(te["y"].values, pred)),
            "spearman": float(spearmanr(te["y"].values, pred).correlation),
            "fitted_slope": float(b), "fitted_intercept": float(a)}


def head_reliability(te: pd.DataFrame, yhat: np.ndarray, k=10, top_frac=0.2):
    """Is the HEAD of the watchlist (the breakout candidates) reliable?"""
    t = te.reset_index(drop=True).copy()
    t["yhat"] = yhat
    slope = float(np.polyfit(t["y"].values, t["yhat"].values, 1)[0])  # <1 => regress to mean
    top_pred = set(t.sort_values("yhat", ascending=False).head(k)["name"])
    top_real = set(t.sort_values("y", ascending=False).head(k)["name"])
    thr = t["y"].quantile(1 - top_frac)
    hi = t[t["y"] >= thr]
    return {"pred_vs_actual_slope": slope, f"top{k}_overlap": len(top_pred & top_real),
            "top_decile_mae": float(np.mean(np.abs(hi["y"] - hi["yhat"]))),
            "overall_mae": float(np.mean(np.abs(t["y"] - t["yhat"]))),
            "high_growth_bias": float(np.mean(hi["yhat"] - hi["y"]))}


# ----------------------------- run -----------------------------
def main():
    hist = get_warehouse().query_df("SELECT name,date,downloads FROM download_history")
    out = {"config": {"horizon": HORIZON, "smooth": SMOOTH, "stride": STRIDE,
                      "n_features": len(FEATURES), "n_packages": int(hist.name.nunique())}}

    # 1) LEAK QUANTIFICATION: centered (leaky) vs causal split, identical everything else
    leak = {}
    for label, sm in [("centered_leaky", centered_smooth), ("causal_trailing", trailing_smooth)]:
        df = build_xy(hist, sm)
        tr, te = time_split(df)
        yhat = fit_predict(tr, te)
        leak[label] = {**_metrics(te["y"].values, yhat), "n_train": int(len(tr)), "n_test": int(len(te))}
    out["leak_quantification"] = leak

    # Everything below uses the HONEST causal construction.
    df = build_xy(hist, trailing_smooth)
    tr, te = time_split(df)
    yhat = fit_predict(tr, te)
    y = te["y"].values
    # dump exact held-out predictions so the inference can be reproduced independently
    _dump = te[["name", "t", "y", "persistence"]].copy()
    _dump["yhat"] = yhat
    _dump.to_csv("/tmp/validation_testset.csv", index=False)

    # 2) baselines + skill on the honest split (incl. a FAIR calibrated persistence)
    base = {"model": _metrics(y, yhat),
            "persistence_raw_strawman": _metrics(y, te["persistence"].values),
            "persistence_calibrated_fair": calibrated_persistence(tr, te),
            "predict_zero": _metrics(y, np.zeros_like(y)),
            "predict_train_mean": _metrics(y, np.full_like(y, tr["y"].mean()))}
    base["skill_vs_persistence_r2"] = float(1 - np.mean((y - yhat) ** 2)
                                            / max(np.mean((y - te["persistence"].values) ** 2), 1e-9))
    out["baselines"] = base

    # 2b) shared-package leak: package-disjoint GroupKFold (the honest 'new package' number)
    out["package_disjoint_cv"] = package_disjoint_cv(df)

    # 2c) is the HEAD of the watchlist reliable?
    out["head_reliability"] = head_reliability(te, yhat)

    # 3) effective sample
    span = (df["t"].max() - df["t"].min()).days
    out["effective_sample"] = {
        "n_test_rows": int(len(te)), "n_packages_test": int(te["name"].nunique()),
        "overlap_factor_horizon_over_stride": HORIZON / STRIDE,
        "naive_effective_n_approx": round(len(te) / (HORIZON / STRIDE), 1),
        "independent_time_blocks_approx": round(span / HORIZON, 1)}

    # 4) cluster bootstrap CIs
    out["cluster_bootstrap"] = cluster_bootstrap(te, yhat)

    # 5) significance: permutation + Diebold-Mariano
    out["permutation_test"] = block_permutation_p(te, yhat)
    out["diebold_mariano_vs_persistence"] = diebold_mariano(y, yhat, te["persistence"].values)

    # 6) within vs between decomposition
    out["within_between"] = within_between(te, yhat)

    # 7) temporal CV feasibility + folds
    out["origin_diagnostics"] = origin_diagnostics(df)
    out["purged_walk_forward"] = purged_walk_forward(df)  # empty if data too short (expected)
    out["unpurged_walk_forward_OPTIMISTIC"] = unpurged_walk_forward(df)

    def _clean(x):  # NaN -> null so the JSON is valid for browsers
        if isinstance(x, float):
            return None if (x != x or x in (float("inf"), float("-inf"))) else x
        if isinstance(x, dict):
            return {k: _clean(v) for k, v in x.items()}
        if isinstance(x, list):
            return [_clean(v) for v in x]
        return x

    out = _clean(out)
    path = os.environ.get("VALIDATION_OUT", "/tmp/validation_results.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, allow_nan=False, default=float)
    print(json.dumps(out, indent=2, allow_nan=False, default=float))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()

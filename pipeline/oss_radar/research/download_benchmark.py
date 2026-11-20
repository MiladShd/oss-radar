"""Frozen, offline 28-day forecast comparison. Never writes to the warehouse or registry.

Run: python -m oss_radar.research.download_benchmark --data EXPORT.csv
     --protocol pipeline/research/download_protocol_v1.json --out OUTPUT_DIRECTORY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import warnings
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd


def audit_history(frame: pd.DataFrame) -> dict:
    required = {"name", "date", "downloads"}
    if not required.issubset(frame):
        raise ValueError("Expected name, date, downloads columns")
    if frame.empty or frame[list(required)].isna().any().any():
        raise ValueError("Empty or null download history")
    if frame.duplicated(["name", "date"]).any():
        raise ValueError("Duplicate package/date observations")
    values = pd.to_numeric(frame.downloads, errors="coerce")
    if (not np.isfinite(values).all() or (values < 0).any()
            or (values != np.floor(values)).any()):
        raise ValueError("Downloads must be finite, non-negative integer counts")
    rows = []
    for name, group in frame.groupby("name", sort=True):
        dates = pd.to_datetime(group.date, errors="raise").sort_values()
        expected = pd.date_range(dates.min(), dates.max())
        if len(dates) != len(expected):
            raise ValueError(f"Missing calendar dates for {name}")
        v = values.loc[group.index]
        ratios = v.to_numpy()[1:] / np.maximum(v.to_numpy()[:-1], 1)
        rows.append({"name": name, "days": len(group), "first_date": str(dates.min().date()),
                     "last_date": str(dates.max().date()), "zero_days": int((v == 0).sum()),
                     "median_daily": float(v.median()),
                     "large_daily_changes": int(((ratios > 3) | (ratios < 1/3)).sum())})
    return {"rows": len(frame), "packages": len(rows), "first_date": str(frame.date.min().date()),
            "last_date": str(frame.date.max().date()), "missing_dates": 0,
            "duplicates": 0, "nulls": 0, "negative_values": 0, "package_audit": rows,
            "interpretation": "Structural checks only; large changes are flagged, not removed or declared errors. Backfills and source accuracy are unverified."}


def features(context: np.ndarray, cutoff: pd.Timestamp) -> dict:
    """All features use only the trailing observed context; no centered smoothing."""
    x = np.asarray(context, dtype=float)
    if len(x) != 84 or not np.isfinite(x).all():
        raise ValueError("Require 84 complete finite observations")
    def slope(a):
        return float(np.polyfit(np.arange(len(a)), a / max(a.mean(), 1), 1)[0])
    dates = pd.date_range(end=cutoff, periods=84)
    weekends = dates.dayofweek >= 5
    return {"log_mean_7": float(np.log1p(x[-7:].mean())),
            "log_mean_28": float(np.log1p(x[-28:].mean())),
            "log_mean_84": float(np.log1p(x.mean())),
            "log_ratio_7": float(np.log1p(x[-7:].sum()) - np.log1p(x[-14:-7].sum())),
            "log_ratio_28": float(np.log1p(x[-28:].sum()) - np.log1p(x[-56:-28].sum())),
            "slope_28": slope(x[-28:]), "slope_84": slope(x),
            "cv_28": float(x[-28:].std() / max(x[-28:].mean(), 1)),
            "weekend_ratio": float(x[weekends].mean() / max(x[~weekends].mean(), 1)),
            "origin_weekday": int(cutoff.dayofweek)}


def make_rows(frame: pd.DataFrame, protocol: dict) -> tuple[pd.DataFrame, dict]:
    context_days = protocol["context_days"]
    horizon = protocol["horizon_days"]
    lag = protocol["availability_lag_days"]
    rows, series = [], {}
    for name, group in frame.groupby("name", sort=True):
        g = group.sort_values("date")
        x = g.downloads.to_numpy(dtype=float)
        dates = pd.DatetimeIndex(g.date)
        series[name] = pd.Series(x, index=dates)
        for i in range(context_days-1, len(x)-horizon-lag):
            # Context ends at i, issuance is i+lag, target begins i+lag+1.
            origin = dates[i]
            start, end = i+lag+1, i+lag+horizon+1
            reference, actual = float(x[i-27:i+1].sum()), float(x[start:end].sum())
            rows.append({"name": name, "cutoff": origin, "outcome_end": dates[end-1],
                         "reference": reference, "actual_downloads": actual,
                         "target": float(np.log1p(actual)-np.log1p(reference)),
                         **features(x[i-context_days+1:i+1], origin)})
    return pd.DataFrame(rows), series


def training_at(rows: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    """Every training label is completely observed at the simulated information cutoff."""
    return rows.loc[rows.outcome_end <= cutoff].copy()


def seasonal_naive(context: np.ndarray, steps: int) -> np.ndarray:
    return np.resize(np.asarray(context, dtype=float)[-7:], steps)


def local_forecasts(context: np.ndarray, steps: int) -> tuple[np.ndarray, np.ndarray, str]:
    # Optional research dependency. Production imports never need statsmodels.
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    naive = seasonal_naive(context, steps)
    scale = max(float(np.mean(context)), 1.0)
    try:
        with warnings.catch_warnings(record=True):
            fit = ExponentialSmoothing(
                context/scale, trend="add", damped_trend=True,
                seasonal="add", seasonal_periods=7, initialization_method="estimated",
            ).fit(optimized=True, use_brute=False, method="L-BFGS-B", remove_bias=False)
        forecast = np.asarray(fit.forecast(steps))*scale
        if not fit.mle_retvals.get("success", False):
            return naive, naive.copy(), "optimizer_failed_fallback"
        if not np.isfinite(forecast).all():
            return naive, naive.copy(), "nonfinite_fallback"
        return naive, np.maximum(forecast, 0), "ok"
    except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
        return naive, naive.copy(), f"{type(exc).__name__}_fallback"


def metric_table(predictions: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (split, origin, model), group in predictions.groupby(["split", "cutoff", "model"]):
        errors = group.prediction - group.actual
        k = min(10, len(group))
        top = set(group.nlargest(k, "prediction").name)
        actual_top = set(group.nlargest(k, "actual").name)
        rho = group.prediction.corr(group.actual, method="spearman") if group.prediction.nunique() > 1 and group.actual.nunique() > 1 else None
        rows.append({"split": split, "cutoff": origin, "model": model,
                     "packages": len(group), "mae_log_growth": float(errors.abs().mean()),
                     "bias_log_growth": float(errors.mean()),
                     "median_absolute_growth_pp": float((np.expm1(group.prediction)-np.expm1(group.actual)).abs().median()*100),
                     "top_10_hits": len(top & actual_top), "top_k": k,
                     "spearman": None if rho is None or not np.isfinite(rho) else float(rho),
                     "mean_actual_top_10_log_growth": float(group.loc[group.name.isin(top), "actual"].mean())})
    return pd.DataFrame(rows)


def paired_bootstrap(predictions: pd.DataFrame, baseline: str) -> dict:
    final = predictions.loc[predictions.split == "final"].copy()
    final["absolute_error"] = (final.prediction-final.actual).abs()
    pivot = final.pivot(index=["name", "cutoff"], columns="model", values="absolute_error")
    by_package = (pivot["pooled_lightgbm"]-pivot[baseline]).groupby("name").mean().to_numpy()
    rng = np.random.default_rng(20261031)
    samples = np.array([rng.choice(by_package, size=len(by_package), replace=True).mean() for _ in range(5000)])
    return {"estimand": "LightGBM absolute error minus development-selected baseline error; negative favors LightGBM",
            "mean_difference": float(by_package.mean()),
            "package_cluster_95_percent_interval": np.quantile(samples, [.025, .975]).tolist(),
            "qualification": "Conditional on two observed final periods. Does not quantify general future-period uncertainty."}


def run_benchmark(frame: pd.DataFrame, protocol: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    from lightgbm import LGBMRegressor

    all_rows, series = make_rows(frame, protocol)
    predictions, train_audit, fit_status = [], [], []
    feature_names = protocol["feature_set"]
    lag, horizon = protocol["availability_lag_days"], protocol["horizon_days"]
    for split in ["development", "final"]:
        for cutoff_string in protocol[f"{split}_cutoffs"]:
            cutoff = pd.Timestamp(cutoff_string)
            train = training_at(all_rows, cutoff)
            test = all_rows.loc[all_rows.cutoff == cutoff].sort_values("name")
            if train.empty or len(test) != frame.name.nunique():
                raise ValueError(f"Incomplete common cohort at {cutoff_string}")
            assert train.outcome_end.max() <= cutoff
            model = LGBMRegressor(**protocol["pooled_lightgbm"])
            model.fit(train[feature_names], train.target)
            pooled = model.predict(test[feature_names])
            train_audit.append({"cutoff": cutoff_string, "split": split, "rows": len(train),
                                "distinct_origins": train.cutoff.nunique(),
                                "latest_training_outcome": str(train.outcome_end.max().date())})
            for (_, row), pooled_pred in zip(test.iterrows(), pooled, strict=True):
                context = series[row["name"]].loc[:cutoff].tail(protocol["context_days"]).to_numpy()
                naive, ets, status = local_forecasts(context, lag+horizon)
                fit_status.append({"name": row["name"], "cutoff": cutoff_string, "status": status})
                local = {"seasonal_naive": naive, "damped_ets": ets}
                for model_name in ["seasonal_naive", "damped_ets", "pooled_lightgbm"]:
                    total = None if model_name == "pooled_lightgbm" else float(local[model_name][lag:].sum())
                    value = float(pooled_pred) if total is None else float(np.log1p(total)-np.log1p(row.reference))
                    predictions.append({"split": split, "cutoff": cutoff_string, "name": row["name"],
                                        "issued_date": str((cutoff+pd.Timedelta(days=lag)).date()),
                                        "outcome_start": str((cutoff+pd.Timedelta(days=lag+1)).date()),
                                        "outcome_end": str(row.outcome_end.date()), "model": model_name,
                                        "reference_downloads": row.reference,
                                        "actual_downloads": row.actual_downloads,
                                        "prediction": value, "actual": row.target,
                                        "predicted_growth_percent": float(np.expm1(value)*100),
                                        "actual_growth_percent": float(np.expm1(row.target)*100)})
            print(f"Completed {split} cutoff {cutoff_string}; {len(train)} matured training rows", flush=True)
    prediction_frame = pd.DataFrame(predictions)
    metrics = metric_table(prediction_frame)
    dev = metrics.loc[(metrics.split == "development") & (metrics.model != "pooled_lightgbm")]
    baseline = str(dev.groupby("model").mae_log_growth.mean().idxmin())
    aggregate = metrics.groupby(["split", "model"]).agg(
        mae_log_growth=("mae_log_growth", "mean"), mean_top_10_hits=("top_10_hits", "mean"),
        mean_spearman=("spearman", "mean"), mean_bias_log_growth=("bias_log_growth", "mean"),
    ).reset_index()
    final = aggregate.loc[aggregate.split == "final"].set_index("model")
    improvement = 1-float(final.loc["pooled_lightgbm", "mae_log_growth"])/float(final.loc[baseline, "mae_log_growth"])
    result = {"selected_baseline": baseline, "final_lightgbm_error_reduction": improvement,
              "aggregates": aggregate.to_dict("records"), "training_audit": train_audit,
              "ets_fit_status": fit_status, "ets_fallbacks": sum(s["status"] != "ok" for s in fit_status),
              "paired_comparison": paired_bootstrap(prediction_frame, baseline),
              "production_promotion": False,
              "reason": "Retrospective comparison with only two final periods; prospective confirmation required."}
    return prediction_frame, metrics, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text())
    digest = hashlib.sha256(args.data.read_bytes()).hexdigest()
    if digest != protocol["data_sha256"]:
        raise ValueError("Data fingerprint differs from the frozen protocol")
    frame = pd.read_csv(args.data, parse_dates=["date"])
    audit = audit_history(frame)
    if str(frame.date.max().date()) != protocol["data_end"]:
        raise ValueError("Unexpected final observation date")
    args.out.mkdir(parents=True, exist_ok=True)
    predictions, metrics, results = run_benchmark(frame, protocol)
    results["data_sha256"] = digest
    results["protocol_sha256"] = hashlib.sha256(args.protocol.read_bytes()).hexdigest()
    results["environment"] = {"python": platform.python_version(), **{n: version(n) for n in ["numpy", "pandas", "lightgbm", "statsmodels", "scipy"]}}
    predictions.to_csv(args.out/"predictions.csv", index=False)
    metrics.to_csv(args.out/"metrics_by_period.csv", index=False)
    (args.out/"results.json").write_text(json.dumps(results, indent=2, allow_nan=False))
    (args.out/"data_audit.json").write_text(json.dumps(audit, indent=2, allow_nan=False))
    (args.out/"protocol.json").write_text(json.dumps(protocol, indent=2))
    print(json.dumps({k: v for k, v in results.items() if k not in ["ets_fit_status", "training_audit"]}, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

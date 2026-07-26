"""Validation gate — the hard quality check a retrained growth model must pass to be promoted.

This turns the rigorous-validation philosophy (docs/VALIDATION.md) into an automatic, per-run
guard so the daily self-improvement loop can *never* silently ship a leak or a sub-baseline model:

  1. has-skill   — held-out R^2 > floor (beats the mean predictor) AND Spearman >= floor.
  2. generalises — package-disjoint (GroupKFold) Spearman >= floor, AND the same-package ->
                   unseen-package R^2 gap is not blown out. A large gap is the shared-package
                   memorisation-leak signature the LLM panel caught (same 0.58 vs unseen 0.36).
  3. not-too-good — held-out R^2 below a ceiling. An implausibly high R^2 on this intrinsically
                   noisy 70-day target is the fingerprint of a re-introduced lookahead leak (the
                   retired centered-MA smoother leak scored ~0.70).

It is a *fast* guard (a handful of LightGBM fits), deliberately NOT a replacement for the deep
harness (pipeline/scripts/validate_growth.py + the Wolfram cross-check), which stays the daily
audit. The gate's verdict is consumed by the registry (promotion) and the pipeline (serving /
auto-rollback), and surfaced in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import r2_score
from sklearn.model_selection import GroupKFold

from oss_radar.config import Settings, get_settings
from oss_radar.features import ALL_DOWNLOAD_FEATURES, GROWTH_TARGET_COLUMN
from oss_radar.models.evaluation import (
    date_grouped_train_validation_test,
    growth_evaluation_provenance,
)

# Same regressor configuration as GrowthModel / the validation harness, so the gate measures the
# model that will actually be served rather than a different one.
_LGB_PARAMS = dict(
    n_estimators=400, learning_rate=0.03, num_leaves=31, subsample=0.8, subsample_freq=1,
    colsample_bytree=0.9, min_child_samples=30, n_jobs=-1, verbose=-1,
)


@dataclass
class GateResult:
    passed: bool
    skipped: bool = False
    checks: list[dict] = field(default_factory=list)   # [{name, passed, value, threshold, detail}]
    metrics: dict = field(default_factory=dict)         # gate_* numbers for the dashboard/registry
    reasons: list[str] = field(default_factory=list)    # human-readable failure reasons

    def as_metric_dict(self) -> dict:
        """Numeric-only view persisted to model_runs so the gate is charted over time."""
        return {f"gate_{k}": v for k, v in self.metrics.items()} | {"gate_passed": float(self.passed)}


def _fit_predict(train: pd.DataFrame, test: pd.DataFrame, features: list[str], seed: int) -> np.ndarray:
    import lightgbm as lgb

    m = lgb.LGBMRegressor(random_state=seed, **_LGB_PARAMS)
    yclip = train[GROWTH_TARGET_COLUMN].astype(float).clip(-0.9, 3.0)
    m.fit(train[features].astype(float), yclip)
    return m.predict(test[features].astype(float))


def _fit_temporal(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    test: pd.DataFrame,
    features: list[str],
    seed: int,
) -> np.ndarray:
    """Freeze iteration count on validation, refit development rows, touch test only once."""
    import lightgbm as lgb

    tuning = lgb.LGBMRegressor(random_state=seed, **_LGB_PARAMS)
    tuning.fit(
        train[features].astype(float),
        train[GROWTH_TARGET_COLUMN].astype(float).clip(-0.9, 3.0),
        eval_set=[(
            validation[features].astype(float),
            validation[GROWTH_TARGET_COLUMN].astype(float).clip(-0.9, 3.0),
        )],
        eval_metric="l1",
        callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
    )
    best_iteration = int(tuning.best_iteration_ or _LGB_PARAMS["n_estimators"])
    development = pd.concat([train, validation], ignore_index=True)
    model = lgb.LGBMRegressor(
        random_state=seed, **{**_LGB_PARAMS, "n_estimators": best_iteration}
    )
    model.fit(
        development[features].astype(float),
        development[GROWTH_TARGET_COLUMN].astype(float).clip(-0.9, 3.0),
    )
    return model.predict(test[features].astype(float))


def _r2_spearman(y: np.ndarray, yhat: np.ndarray) -> tuple[float, float]:
    if len(y) < 3 or len(np.unique(y)) < 2:
        return float("nan"), float("nan")
    rho = spearmanr(y, yhat).correlation
    return float(r2_score(y, yhat)), float(rho if rho == rho else float("nan"))


def _calibrated_persistence(
    train: pd.DataFrame,
    test: pd.DataFrame,
) -> np.ndarray:
    """Fair causal baseline: calibrate trailing momentum on train, then freeze it on test."""
    proxy = "mom_56v56" if "mom_56v56" in train else "mom_28v28"
    if proxy not in train or proxy not in test:
        return np.full(len(test), np.nan)
    x_train = np.log(np.clip(train[proxy].astype(float).to_numpy(), 1e-9, None))
    x_test = np.log(np.clip(test[proxy].astype(float).to_numpy(), 1e-9, None))
    y_train = train[GROWTH_TARGET_COLUMN].astype(float).to_numpy()
    finite = np.isfinite(x_train) & np.isfinite(y_train)
    if finite.sum() < 3 or np.unique(x_train[finite]).size < 2:
        return np.full(len(test), np.nan)
    slope, intercept = np.polyfit(x_train[finite], y_train[finite], 1)
    return intercept + slope * x_test


def growth_gate(train_df: pd.DataFrame, features: list[str],
                settings: Settings | None = None, seed: int | None = None) -> GateResult:
    """Evaluate the held-out and unseen-package skill of a freshly-buildable growth model and
    decide whether it is safe to promote. Pure + deterministic given (train_df, features, seed)."""
    s = settings or get_settings()
    seed = s.random_seed if seed is None else seed

    # No training rows at all (empty/column-less frame) -> nothing to verify; don't block.
    if train_df is None or train_df.empty or GROWTH_TARGET_COLUMN not in train_df.columns:
        return GateResult(passed=True, skipped=True,
                          reasons=["skipped: no growth training rows"], metrics={"n": 0.0})

    df = train_df.dropna(subset=[GROWTH_TARGET_COLUMN]).copy()
    # Not enough data to *verify* anything -> don't block the loop (the previous champion stays).
    if len(df) < s.min_train_rows:
        return GateResult(passed=True, skipped=True,
                          reasons=[f"skipped: only {len(df)} rows (< min_train_rows {s.min_train_rows})"],
                          metrics={"n": float(len(df))})

    missing_features = [feature for feature in features if feature not in df]
    causal_features = set(features).issubset(ALL_DOWNLOAD_FEATURES) and not missing_features
    try:
        temporal = date_grouped_train_validation_test(df)
        tr, val, te = temporal.train, temporal.validation, temporal.test
        same_pred = _fit_temporal(tr, val, te, features, seed)
        same_r2, same_rho = _r2_spearman(
            te[GROWTH_TARGET_COLUMN].astype(float).to_numpy(), same_pred
        )
        baseline_pred = _calibrated_persistence(pd.concat([tr, val]), te)
        baseline_r2, baseline_rho = _r2_spearman(
            te[GROWTH_TARGET_COLUMN].astype(float).to_numpy(), baseline_pred
        )
        provenance = growth_evaluation_provenance(
            df, temporal, features, s.growth_horizon_days
        )
    except (KeyError, TypeError, ValueError):
        tr = val = te = pd.DataFrame()
        same_r2 = same_rho = baseline_r2 = baseline_rho = float("nan")
        provenance = {}

    # 2) package-disjoint generalisation (GroupKFold by name) — the unseen-package number
    oof_r2 = oof_rho = baseline_oof_r2 = baseline_oof_rho = float("nan")
    has_group_cohort = "name" in df.columns and df["name"].nunique() >= s.gate_cv_splits
    if has_group_cohort and not missing_features:
        y = df[GROWTH_TARGET_COLUMN].astype(float).to_numpy()
        oof = np.full(len(df), np.nan)
        baseline_oof = np.full(len(df), np.nan)
        gkf = GroupKFold(n_splits=s.gate_cv_splits)
        for tr_idx, te_idx in gkf.split(df[features], y, df["name"].to_numpy()):
            oof[te_idx] = _fit_predict(df.iloc[tr_idx], df.iloc[te_idx], features, seed)
            baseline_oof[te_idx] = _calibrated_persistence(
                df.iloc[tr_idx], df.iloc[te_idx]
            )
        mask = ~np.isnan(oof)
        oof_r2, oof_rho = _r2_spearman(y[mask], oof[mask])
        baseline_mask = ~np.isnan(baseline_oof)
        baseline_oof_r2, baseline_oof_rho = _r2_spearman(
            y[baseline_mask], baseline_oof[baseline_mask]
        )

    gap = (same_r2 - oof_r2) if (same_r2 == same_r2 and oof_r2 == oof_r2) else float("nan")
    metrics = {"same_split_r2": same_r2, "same_split_spearman": same_rho,
               "oof_r2": oof_r2, "oof_spearman": oof_rho, "generalization_gap": gap,
               "baseline_r2": baseline_r2, "baseline_spearman": baseline_rho,
               "baseline_oof_r2": baseline_oof_r2,
               "baseline_oof_spearman": baseline_oof_rho,
               "oof_spearman_lift_vs_baseline": (
                   oof_rho - baseline_oof_rho
                   if np.isfinite(oof_rho) and np.isfinite(baseline_oof_rho)
                   else float("nan")
               ),
               "n": float(len(df)),
               "n_packages": float(df["name"].nunique() if "name" in df else 0)}
    if provenance:
        metrics["benchmark_hash_numeric"] = float(
            int(provenance["benchmark_hash"][:13], 16)
        )

    # Once the row threshold says evidence is expected, a NaN is a hard failure. Missing evidence
    # must not silently approve an unevaluable candidate.
    def chk(name, ok, value, threshold, detail):
        return {"name": name, "passed": bool(ok), "value": value, "threshold": threshold, "detail": detail}

    finite = np.isfinite
    baseline_lift = metrics["oof_spearman_lift_vs_baseline"]
    checks = [
        chk("causal_feature_contract", causal_features, float(causal_features), 1.0,
            "only reviewed point-in-time feature definitions may be served"),
        chk("has_date_grouped_evidence", finite(same_r2) and finite(same_rho),
            same_rho, "finite", "date-grouped origin metrics must be computable"),
        chk("has_group_evidence", finite(oof_r2) and finite(oof_rho),
            oof_rho, "finite", "package-disjoint metrics must be computable"),
        chk("has_baseline_evidence", finite(baseline_oof_rho),
            baseline_oof_rho, "finite", "fair calibrated baseline must be computable"),
        chk("has_skill_spearman", finite(same_rho) and same_rho >= s.gate_min_spearman,
            same_rho, s.gate_min_spearman, "held-out rank skill beats chance"),
        chk("has_skill_r2", finite(same_r2) and same_r2 >= s.gate_min_r2,
            same_r2, s.gate_min_r2, "held-out R^2 beats the mean predictor"),
        chk("generalises_spearman", finite(oof_rho) and oof_rho >= s.gate_min_oof_spearman,
            oof_rho, s.gate_min_oof_spearman, "unseen-package rank skill"),
        chk("beats_fair_baseline", finite(baseline_lift) and baseline_lift > 0.0,
            baseline_lift, 0.0, "unseen-package Spearman lift over calibrated persistence"),
        chk("not_leaky_ceiling", finite(same_r2) and same_r2 <= s.gate_max_r2,
            same_r2, s.gate_max_r2, "R^2 below the leak ceiling"),
        chk("not_leaky_gap", finite(gap) and gap <= s.gate_max_generalization_gap,
            gap, s.gate_max_generalization_gap, "same->unseen R^2 gap (shared-package leak)"),
    ]
    failed = [c for c in checks if not c["passed"]]
    reasons = [f"{c['name']}: {_fmt(c['value'])} vs threshold {_fmt(c['threshold'])} ({c['detail']})"
               for c in failed]
    return GateResult(passed=(not failed), checks=checks, metrics=metrics, reasons=reasons)


def _fmt(x) -> str:
    return f"{x:.4f}" if isinstance(x, float) and x == x else str(x)

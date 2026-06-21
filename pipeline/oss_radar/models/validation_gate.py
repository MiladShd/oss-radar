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
from oss_radar.features import GROWTH_TARGET_COLUMN

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


def _r2_spearman(y: np.ndarray, yhat: np.ndarray) -> tuple[float, float]:
    if len(y) < 3 or len(np.unique(y)) < 2:
        return float("nan"), float("nan")
    rho = spearmanr(y, yhat).correlation
    return float(r2_score(y, yhat)), float(rho if rho == rho else float("nan"))


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

    # 1) held-out time split (mirror GrowthModel.fit: sort by feature_date, train on the earlier 80%)
    if "feature_date" in df.columns:
        df = df.sort_values("feature_date")
    split = int(len(df) * 0.8)
    tr, te = df.iloc[:split], df.iloc[split:]
    same_pred = _fit_predict(tr, te, features, seed)
    same_r2, same_rho = _r2_spearman(te[GROWTH_TARGET_COLUMN].astype(float).to_numpy(), same_pred)

    # 2) package-disjoint generalisation (GroupKFold by name) — the unseen-package number
    oof_r2 = oof_rho = float("nan")
    if "name" in df.columns and df["name"].nunique() >= s.gate_cv_splits:
        y = df[GROWTH_TARGET_COLUMN].astype(float).to_numpy()
        oof = np.full(len(df), np.nan)
        gkf = GroupKFold(n_splits=s.gate_cv_splits)
        for tr_idx, te_idx in gkf.split(df[features], y, df["name"].to_numpy()):
            oof[te_idx] = _fit_predict(df.iloc[tr_idx], df.iloc[te_idx], features, seed)
        mask = ~np.isnan(oof)
        oof_r2, oof_rho = _r2_spearman(y[mask], oof[mask])

    gap = (same_r2 - oof_r2) if (same_r2 == same_r2 and oof_r2 == oof_r2) else float("nan")
    metrics = {"same_split_r2": same_r2, "same_split_spearman": same_rho,
               "oof_r2": oof_r2, "oof_spearman": oof_rho, "generalization_gap": gap,
               "n": float(len(df)), "n_packages": float(df["name"].nunique() if "name" in df else 0)}

    # --- checks (a NaN metric never fails a check; missing evidence != evidence of a leak) ---
    def chk(name, ok, value, threshold, detail):
        return {"name": name, "passed": bool(ok), "value": value, "threshold": threshold, "detail": detail}

    checks = [
        chk("has_skill_spearman", not (same_rho == same_rho) or same_rho >= s.gate_min_spearman,
            same_rho, s.gate_min_spearman, "held-out rank skill beats chance"),
        chk("has_skill_r2", not (same_r2 == same_r2) or same_r2 >= s.gate_min_r2,
            same_r2, s.gate_min_r2, "held-out R^2 beats the mean predictor"),
        chk("generalises_spearman", not (oof_rho == oof_rho) or oof_rho >= s.gate_min_oof_spearman,
            oof_rho, s.gate_min_oof_spearman, "unseen-package rank skill"),
        chk("not_leaky_ceiling", not (same_r2 == same_r2) or same_r2 <= s.gate_max_r2,
            same_r2, s.gate_max_r2, "R^2 below the leak ceiling"),
        chk("not_leaky_gap", not (gap == gap) or gap <= s.gate_max_generalization_gap,
            gap, s.gate_max_generalization_gap, "same->unseen R^2 gap (shared-package leak)"),
    ]
    failed = [c for c in checks if not c["passed"]]
    reasons = [f"{c['name']}: {_fmt(c['value'])} vs threshold {_fmt(c['threshold'])} ({c['detail']})"
               for c in failed]
    return GateResult(passed=(not failed), checks=checks, metrics=metrics, reasons=reasons)


def _fmt(x) -> str:
    return f"{x:.4f}" if isinstance(x, float) and x == x else str(x)

"""Backtest harness — held-out predicted-vs-actual for the dashboard and experiments.

Trains a model on the earlier as-of dates and evaluates on the later (unseen) ones, returning
metrics + calibration deciles + a scatter sample (growth) and an ROC curve (risk). The pipeline
persists this each run so the dashboard can show how the model is actually doing.
"""

from __future__ import annotations

import math

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    r2_score,
    roc_auc_score,
    roc_curve,
)
from sklearn.pipeline import make_pipeline

from oss_radar.features import GROWTH_TARGET_COLUMN
from oss_radar.models.evaluation import (
    date_grouped_train_validation_test,
    growth_evaluation_provenance,
    risk_evaluation_provenance,
    risk_holdout_mask,
)

# Kept in step with GrowthModel (self-contained so the harness never breaks on import).
GROWTH_PARAMS = {
    "n_estimators": 500, "learning_rate": 0.03, "num_leaves": 31, "subsample": 0.8,
    "subsample_freq": 1, "colsample_bytree": 0.9, "min_child_samples": 30,
}


def _r(x):
    try:
        x = float(x)
        return None if (x != x or x in (float("inf"), float("-inf"))) else round(x, 4)
    except (TypeError, ValueError):
        return None


def growth_backtest(train_df: pd.DataFrame, features: list[str], params: dict | None = None,
                    seed: int = 42, frac: float = 0.8, scatter_n: int = 300,
                    horizon_days: int = 70) -> dict | None:
    # ``frac`` remains in the public signature for compatibility; the operational benchmark is
    # now the shared, versioned 70/15/15 date-grouped contract.
    del frac
    df = train_df.dropna(subset=[GROWTH_TARGET_COLUMN]).copy()
    feats = [f for f in features if f in df.columns]
    if len(df) < 50 or not feats:
        return None
    try:
        split = date_grouped_train_validation_test(df)
    except ValueError:
        return None
    tr, val, te = split.train, split.validation, split.test
    base_params = {**GROWTH_PARAMS, **(params or {})}
    tuning = lgb.LGBMRegressor(**base_params, random_state=seed, n_jobs=-1, verbose=-1)
    tuning.fit(
        tr[feats].astype(float),
        tr[GROWTH_TARGET_COLUMN].astype(float).clip(-0.9, 3.0),
        eval_set=[(
            val[feats].astype(float),
            val[GROWTH_TARGET_COLUMN].astype(float).clip(-0.9, 3.0),
        )],
        eval_metric="l1",
        callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
    )
    best_iteration = int(tuning.best_iteration_ or base_params["n_estimators"])
    development = pd.concat([tr, val], ignore_index=True)
    final_params = {**base_params, "n_estimators": best_iteration}
    model = lgb.LGBMRegressor(**final_params, random_state=seed, n_jobs=-1, verbose=-1)
    model.fit(
        development[feats].astype(float),
        development[GROWTH_TARGET_COLUMN].astype(float).clip(-0.9, 3.0),
    )
    pred = model.predict(te[feats].astype(float))
    act = te[GROWTH_TARGET_COLUMN].astype(float).to_numpy()

    rho = spearmanr(act, pred).correlation if len(act) > 2 else float("nan")
    metrics = {
        "spearman": _r(rho), "mae": _r(mean_absolute_error(act, pred)),
        "rmse": _r(math.sqrt(mean_squared_error(act, pred))),
        "r2": _r(r2_score(act, pred)), "n_test": int(len(act)), "n_train": int(len(tr)),
        "n_validation": int(len(val)), "best_iteration": best_iteration,
    }
    dec = pd.qcut(pd.Series(pred).rank(method="first"), 10, labels=False).to_numpy()
    calib = [{"decile": d + 1, "pred": _r(pred[dec == d].mean()),
              "actual": _r(act[dec == d].mean()), "n": int((dec == d).sum())} for d in range(10)]
    n = min(scatter_n, len(act))
    idx = np.random.default_rng(0).choice(len(act), size=n, replace=False)
    scatter = [[_r(pred[i]), _r(act[i])] for i in idx]
    provenance = growth_evaluation_provenance(df, split, feats, horizon_days)
    provenance.update({"test_target_clipped": False, "early_stopping_partition": "validation"})
    return {
        "metrics": metrics,
        "calibration": calib,
        "scatter": scatter,
        "features": feats,
        "evaluation_provenance": provenance,
    }


def risk_backtest(risk_df: pd.DataFrame, features: list[str], seed: int = 42) -> dict:
    feats = [f for f in features if f in risk_df.columns]
    mask = risk_holdout_mask(risk_df)
    train = risk_df.loc[~mask]
    test = risk_df.loc[mask]
    y_train = train["at_risk_label"].astype(int).to_numpy()
    y_test = test["at_risk_label"].astype(int).to_numpy()
    n_pos = int(risk_df["at_risk_label"].sum())
    if (
        len(train) < 6
        or len(test) < 6
        or len(np.unique(y_train)) < 2
        or len(np.unique(y_test)) < 2
    ):
        return {"auc": None, "n": int(len(risk_df)), "n_pos": n_pos, "points": [],
                "note": "insufficient stable package-holdout balance"}
    clf = make_pipeline(
        SimpleImputer(strategy="median", keep_empty_features=True),
        lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15,
                           min_child_samples=5, subsample=0.9, colsample_bytree=0.9,
                           random_state=seed, n_jobs=-1, verbose=-1),
    )
    clf.fit(train[feats].astype(float), y_train)
    proba = clf.predict_proba(test[feats].astype(float))[:, 1]
    fpr, tpr, _ = roc_curve(y_test, proba)
    return {"auc": _r(roc_auc_score(y_test, proba)), "n": int(len(risk_df)),
            "n_test": int(len(test)), "n_pos": n_pos,
            "points": [[_r(a), _r(b)] for a, b in zip(fpr, tpr, strict=False)],
            "evaluation_provenance": risk_evaluation_provenance(
                risk_df, feats, benchmark_frame=test
            )}

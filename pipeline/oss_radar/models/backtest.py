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
from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    r2_score,
    roc_auc_score,
    roc_curve,
)
from sklearn.model_selection import StratifiedKFold, cross_val_predict

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
                    seed: int = 42, frac: float = 0.8, scatter_n: int = 300) -> dict | None:
    df = train_df.dropna(subset=["growth_target_7d"]).sort_values("feature_date")
    df = df.assign(growth_target_7d=df["growth_target_7d"].clip(-0.9, 3.0))
    feats = [f for f in features if f in df.columns]
    if len(df) < 50 or not feats:
        return None
    X = df[feats].astype(float)
    y = df["growth_target_7d"].astype(float)
    split = int(len(df) * frac)
    Xtr, Xte, ytr, yte = X.iloc[:split], X.iloc[split:], y.iloc[:split], y.iloc[split:]

    model = lgb.LGBMRegressor(**{**GROWTH_PARAMS, **(params or {})},
                              random_state=seed, n_jobs=-1, verbose=-1)
    model.fit(Xtr, ytr, eval_set=[(Xte, yte)], eval_metric="l1",
              callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)])
    pred = model.predict(Xte)
    act = yte.to_numpy()

    rho = spearmanr(act, pred).correlation if len(act) > 2 else float("nan")
    metrics = {
        "spearman": _r(rho), "mae": _r(mean_absolute_error(act, pred)),
        "rmse": _r(math.sqrt(mean_squared_error(act, pred))),
        "r2": _r(r2_score(act, pred)), "n_test": int(len(act)), "n_train": int(len(Xtr)),
    }
    dec = pd.qcut(pd.Series(pred).rank(method="first"), 10, labels=False).to_numpy()
    calib = [{"decile": d + 1, "pred": _r(pred[dec == d].mean()),
              "actual": _r(act[dec == d].mean()), "n": int((dec == d).sum())} for d in range(10)]
    n = min(scatter_n, len(act))
    idx = np.random.default_rng(0).choice(len(act), size=n, replace=False)
    scatter = [[_r(pred[i]), _r(act[i])] for i in idx]
    return {"metrics": metrics, "calibration": calib, "scatter": scatter, "features": feats}


def risk_backtest(risk_df: pd.DataFrame, features: list[str], seed: int = 42) -> dict:
    y = risk_df["at_risk_label"].astype(int).to_numpy()
    feats = [f for f in features if f in risk_df.columns]
    X = risk_df[feats].astype(float)
    X = X.fillna(X.median())
    n_pos = int(y.sum())
    if n_pos < 3 or n_pos > len(y) - 3:
        return {"auc": None, "n": int(len(y)), "n_pos": n_pos, "points": [],
                "note": "insufficient class balance"}
    clf = lgb.LGBMClassifier(n_estimators=200, learning_rate=0.05, num_leaves=15,
                             min_child_samples=5, subsample=0.9, colsample_bytree=0.9,
                             random_state=seed, n_jobs=-1, verbose=-1)
    proba = cross_val_predict(clf, X, y,
                              cv=StratifiedKFold(min(5, n_pos), shuffle=True, random_state=seed),
                              method="predict_proba")[:, 1]
    fpr, tpr, _ = roc_curve(y, proba)
    return {"auc": _r(roc_auc_score(y, proba)), "n": int(len(y)), "n_pos": n_pos,
            "points": [[_r(a), _r(b)] for a, b in zip(fpr, tpr)]}

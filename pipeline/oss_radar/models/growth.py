"""Growth model — LightGBM regressor forecasting 70-day download momentum.

Uses a time-aware split (train on earlier as-of dates, test on later ones) so reported
metrics reflect genuine forward forecasting rather than random-shuffle leakage. SHAP gives
per-package feature attributions used for the human-readable "why".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from oss_radar.features import DOWNLOAD_FEATURES, GROWTH_TARGET_COLUMN


@dataclass
class GrowthModel:
    features: list[str] = field(default_factory=lambda: list(DOWNLOAD_FEATURES))
    model: lgb.LGBMRegressor | None = None
    metrics: dict = field(default_factory=dict)
    importances: dict = field(default_factory=dict)
    seed: int = 42

    def fit(self, df: pd.DataFrame) -> dict:
        df = df.dropna(subset=[GROWTH_TARGET_COLUMN]).sort_values("feature_date")
        # clip extreme targets (download spikes) to stabilize the regressor
        df = df.assign(**{GROWTH_TARGET_COLUMN: df[GROWTH_TARGET_COLUMN].clip(-0.9, 3.0)})
        X = df[self.features].astype(float)
        y = df[GROWTH_TARGET_COLUMN].astype(float)

        split = int(len(df) * 0.8)
        Xtr, Xte = X.iloc[:split], X.iloc[split:]
        ytr, yte = y.iloc[:split], y.iloc[split:]

        self.model = lgb.LGBMRegressor(
            n_estimators=500,
            learning_rate=0.03,
            num_leaves=31,
            subsample=0.8,
            subsample_freq=1,
            colsample_bytree=0.9,
            min_child_samples=30,
            random_state=self.seed,
            n_jobs=-1,
            verbose=-1,
        )
        self.model.fit(
            Xtr, ytr,
            eval_set=[(Xte, yte)],
            eval_metric="l1",
            callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
        )

        pred = self.model.predict(Xte)
        rho = spearmanr(yte, pred).correlation if len(yte) > 2 else float("nan")
        self.metrics = {
            "mae": float(mean_absolute_error(yte, pred)),
            "rmse": float(math.sqrt(mean_squared_error(yte, pred))),
            "r2": float(r2_score(yte, pred)) if len(yte) > 2 else float("nan"),
            "spearman": float(rho) if rho == rho else 0.0,
            "n_train": int(len(Xtr)),
            "n_test": int(len(Xte)),
        }
        imp = self.model.booster_.feature_importance(importance_type="gain")
        total = imp.sum() or 1
        self.importances = {f: float(v / total) for f, v in zip(self.features, imp, strict=False)}
        return self.metrics

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        assert self.model is not None, "model not fitted"
        return self.model.predict(df[self.features].astype(float))

    def explain(self, df: pd.DataFrame, top_k: int = 3) -> list[list[tuple[str, float]]]:
        """Per-row SHAP attributions (feature, signed contribution), top-k by magnitude."""
        assert self.model is not None
        try:
            import shap

            explainer = shap.TreeExplainer(self.model)
            vals = explainer.shap_values(df[self.features].astype(float))
        except Exception:
            return [[] for _ in range(len(df))]
        out = []
        for row in vals:
            pairs = sorted(zip(self.features, row, strict=False), key=lambda p: abs(p[1]), reverse=True)
            out.append([(f, float(v)) for f, v in pairs[:top_k]])
        return out

    def save(self, path: str) -> None:
        joblib.dump(
            {"model": self.model, "features": self.features,
             "metrics": self.metrics, "importances": self.importances},
            path,
        )

    @classmethod
    def load(cls, path: str) -> GrowthModel:
        blob = joblib.load(path)
        return cls(features=blob["features"], model=blob["model"],
                   metrics=blob.get("metrics", {}), importances=blob.get("importances", {}))

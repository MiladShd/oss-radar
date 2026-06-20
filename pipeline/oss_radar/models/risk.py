"""Risk model — LightGBM classifier predicting an at-risk dependency label.

The watchlist is small and mostly healthy, so this is a deliberately modest learner whose
job is to *rank* relative risk; the headline ``risk_score`` is the transparent composite in
``scoring.py``. The classifier's cross-validated AUC is tracked over time so you can see it
improve as the daily snapshot history accumulates. Methodology: docs/METHODOLOGY.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict

from oss_radar.features import RISK_FEATURES


@dataclass
class RiskModel:
    features: list[str] = field(default_factory=lambda: list(RISK_FEATURES))
    model: lgb.LGBMClassifier | None = None
    metrics: dict = field(default_factory=dict)
    importances: dict = field(default_factory=dict)
    medians: dict = field(default_factory=dict)
    seed: int = 42

    def _prep(self, df: pd.DataFrame) -> pd.DataFrame:
        X = df[self.features].astype(float)
        if not self.medians:
            self.medians = {f: float(X[f].median()) if X[f].notna().any() else 0.0
                            for f in self.features}
        return X.fillna(self.medians)

    def fit(self, df: pd.DataFrame) -> dict:
        y = df["at_risk_label"].astype(int).values
        X = self._prep(df)
        n_pos = int(y.sum())
        self.metrics = {"n_samples": int(len(y)), "n_positive": n_pos}

        if n_pos < 3 or n_pos > len(y) - 3:
            # too few/many positives to learn a meaningful boundary this run
            self.model = None
            self.metrics["auc"] = float("nan")
            self.metrics["note"] = "insufficient class balance; composite score used"
            return self.metrics

        self.model = lgb.LGBMClassifier(
            n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=5,
            subsample=0.9, colsample_bytree=0.9, random_state=self.seed, n_jobs=-1, verbose=-1,
        )
        folds = min(5, n_pos)
        try:
            proba = cross_val_predict(
                self.model, X, y, cv=StratifiedKFold(folds, shuffle=True, random_state=self.seed),
                method="predict_proba",
            )[:, 1]
            self.metrics["auc"] = float(roc_auc_score(y, proba))
        except Exception:
            self.metrics["auc"] = float("nan")

        self.model.fit(X, y)
        imp = self.model.booster_.feature_importance(importance_type="gain")
        total = imp.sum() or 1
        self.importances = {f: float(v / total) for f, v in zip(self.features, imp, strict=False)}
        return self.metrics

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            return np.full(len(df), np.nan)
        return self.model.predict_proba(self._prep(df))[:, 1]

    def save(self, path: str) -> None:
        joblib.dump(
            {"model": self.model, "features": self.features, "metrics": self.metrics,
             "importances": self.importances, "medians": self.medians},
            path,
        )

    @classmethod
    def load(cls, path: str) -> RiskModel:
        blob = joblib.load(path)
        return cls(features=blob["features"], model=blob["model"], metrics=blob.get("metrics", {}),
                   importances=blob.get("importances", {}), medians=blob.get("medians", {}))

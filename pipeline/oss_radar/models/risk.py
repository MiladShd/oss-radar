"""Risk model — calibrated LightGBM classifier predicting an at-risk dependency label.

The learner is evaluated on a stable, never-trained package holdout. Its raw probabilities are
Platt-scaled from package-disjoint out-of-fold predictions before they can contribute to the
headline ``risk_score`` alongside the transparent composite and categorical safety floors.
Methodology: docs/METHODOLOGY.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedGroupKFold, StratifiedKFold, cross_val_predict
from sklearn.pipeline import make_pipeline

from oss_radar.features import RISK_FEATURES
from oss_radar.models.evaluation import risk_evaluation_provenance, risk_holdout_mask


@dataclass
class RiskModel:
    features: list[str] = field(default_factory=lambda: list(RISK_FEATURES))
    model: lgb.LGBMClassifier | None = None
    metrics: dict = field(default_factory=dict)
    importances: dict = field(default_factory=dict)
    medians: dict = field(default_factory=dict)
    calibrator: LogisticRegression | None = None
    eval_provenance: dict = field(default_factory=dict)
    seed: int = 42

    def _prep(self, df: pd.DataFrame) -> pd.DataFrame:
        X = df[self.features].astype(float)
        if not self.medians:
            self.medians = {f: float(X[f].median()) if X[f].notna().any() else 0.0
                            for f in self.features}
        return X.fillna(self.medians)

    def fit(self, df: pd.DataFrame) -> dict:
        y = df["at_risk_label"].astype(int).values
        n_pos = int(y.sum())
        holdout_mask = risk_holdout_mask(df)
        train = df.loc[~holdout_mask].copy()
        test = df.loc[holdout_mask].copy()
        y_train = train["at_risk_label"].astype(int).to_numpy()
        y_test = test["at_risk_label"].astype(int).to_numpy()
        self.metrics = {
            "n_samples": int(len(y)),
            "n_train": int(len(train)),
            "n_test": int(len(test)),
            "n_positive": n_pos,
            "n_train_packages": int(train["name"].nunique()),
            "n_test_packages": int(test["name"].nunique()),
        }
        self.eval_provenance = risk_evaluation_provenance(
            df, self.features, benchmark_frame=test
        )

        train_pos = int(y_train.sum())
        test_pos = int(y_test.sum())
        if (
            len(train) < 6
            or len(test) < 6
            or train_pos < 3
            or train_pos > len(train) - 3
            or test_pos < 3
            or test_pos > len(test) - 3
        ):
            # too few/many positives to learn a meaningful boundary this run
            self.model = None
            self.metrics["auc"] = float("nan")
            self.metrics["group_auc"] = float("nan")
            self.metrics["note"] = (
                "insufficient class balance in stable package holdout; composite score used"
            )
            return self.metrics

        def classifier() -> lgb.LGBMClassifier:
            return lgb.LGBMClassifier(
                n_estimators=200,
                learning_rate=0.05,
                num_leaves=15,
                min_child_samples=5,
                subsample=0.9,
                colsample_bytree=0.9,
                random_state=self.seed,
                n_jobs=-1,
                verbose=-1,
            )

        groups = train["name"].astype(str).to_numpy()
        cv, cv_groups = risk_cv(y_train, groups, self.seed)
        try:
            if cv is None:
                raise ValueError("insufficient independent groups for cross-validation")
            # Imputation is fit independently inside every fold. Computing medians on the whole
            # frame before package-disjoint CV would leak held-out-package distributions.
            evaluation_model = make_pipeline(
                SimpleImputer(strategy="median", keep_empty_features=True),
                classifier(),
            )
            proba = cross_val_predict(
                evaluation_model,
                train[self.features].astype(float),
                y_train,
                cv=cv,
                groups=cv_groups,
                method="predict_proba",
            )[:, 1]
            self.metrics["cv_auc"] = float(roc_auc_score(y_train, proba))
            self.metrics["cv_mode"] = "package-disjoint-diagnostic"
            # Platt scaling is learned only from package-disjoint out-of-fold predictions.
            # The reserved promotion holdout remains completely untouched by calibration.
            self.calibrator = LogisticRegression(
                random_state=self.seed,
                solver="liblinear",
            ).fit(proba.reshape(-1, 1), y_train)
            self.metrics["probability_calibration"] = "platt-package-disjoint-oof"
        except Exception:
            self.model = None
            self.calibrator = None
            self.metrics["auc"] = float("nan")
            self.metrics["group_auc"] = float("nan")
            self.metrics["cv_auc"] = float("nan")
            self.metrics["note"] = (
                "package-disjoint probability calibration unavailable; composite score used"
            )
            return self.metrics

        # The served model never trains on the stable benchmark packages, so future incumbents and
        # challengers can both be evaluated on the same current benchmark without package leakage.
        self.model = classifier()
        self.model.fit(self._prep(train), y_train)
        holdout_proba = self.predict_proba(test)
        holdout_auc = float(roc_auc_score(y_test, holdout_proba))
        self.metrics["auc"] = holdout_auc
        self.metrics["group_auc"] = holdout_auc
        self.metrics["brier"] = float(brier_score_loss(y_test, holdout_proba))
        self.metrics["evaluation_mode"] = "stable-package-disjoint-holdout"

        imp = self.model.booster_.feature_importance(importance_type="gain")
        total = imp.sum() or 1
        self.importances = {f: float(v / total) for f, v in zip(self.features, imp, strict=False)}
        return self.metrics

    def evaluate_closed_test(self, df: pd.DataFrame) -> tuple[dict, dict]:
        """Evaluate a frozen champion on the challenger's current reserved-package cohort."""
        if self.model is None:
            raise ValueError("model not fitted")
        mask = risk_holdout_mask(df)
        test = df.loc[mask].copy()
        actual = test["at_risk_label"].astype(int).to_numpy()
        if len(test) < 2 or len(np.unique(actual)) < 2:
            raise ValueError("stable risk benchmark lacks both classes")
        predicted = self.predict_proba(test)
        metrics = {
            "group_auc": float(roc_auc_score(actual, predicted)),
            "auc": float(roc_auc_score(actual, predicted)),
            "n_test": int(len(test)),
        }
        provenance = risk_evaluation_provenance(
            df, self.features, benchmark_frame=test
        )
        return metrics, provenance

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            return np.full(len(df), np.nan)
        raw = self.model.predict_proba(self._prep(df))[:, 1]
        if self.calibrator is None:
            return raw
        return self.calibrator.predict_proba(raw.reshape(-1, 1))[:, 1]

    def save(self, path: str) -> None:
        joblib.dump(
            {"model": self.model, "features": self.features, "metrics": self.metrics,
             "importances": self.importances, "medians": self.medians,
             "calibrator": self.calibrator,
             "eval_provenance": self.eval_provenance},
            path,
        )

    @classmethod
    def load(cls, path: str) -> RiskModel:
        blob = joblib.load(path)
        return cls(features=blob["features"], model=blob["model"], metrics=blob.get("metrics", {}),
                   importances=blob.get("importances", {}), medians=blob.get("medians", {}),
                   calibrator=blob.get("calibrator"),
                   eval_provenance=blob.get("eval_provenance", {}))


def risk_cv(y: np.ndarray, groups: np.ndarray | None, seed: int = 42):
    """Return an honest classifier CV splitter and the groups it should consume.

    Rolling forward labels create several rows per package.  Keeping a package wholly within a
    fold prevents the classifier from being scored on another date from a package it trained on.
    """
    n_pos = int(y.sum())
    n_neg = int(len(y) - n_pos)
    if groups is None:
        folds = min(5, n_pos, n_neg)
        return ((StratifiedKFold(folds, shuffle=True, random_state=seed), None)
                if folds >= 2 else (None, None))

    pos_groups = len(np.unique(groups[y == 1]))
    neg_groups = len(np.unique(groups[y == 0]))
    folds = min(5, pos_groups, neg_groups, len(np.unique(groups)))
    if folds < 2:
        return None, groups
    return StratifiedGroupKFold(folds, shuffle=True, random_state=seed), groups

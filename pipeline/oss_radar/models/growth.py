"""Growth model — LightGBM regressor forecasting 70-day download momentum.

Uses a date-grouped origin split rather than a random shuffle. The source window is not yet deep
enough to embargo overlapping 70-day outcomes, so package-disjoint gate evidence is reported
separately and the metric is not presented as an independent temporal forecast. SHAP provides
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
from oss_radar.models.conformal import DriftAwareConformal
from oss_radar.models.evaluation import (
    date_grouped_train_validation_test,
    growth_evaluation_provenance,
)

_TARGET_MIN = -0.9
_TARGET_MAX = 3.0
_MAX_ESTIMATORS = 500
# Nominal central-interval levels and the drift-aware conformal settings (docs/CONFORMAL.md).
INTERVAL_LEVELS = (0.8, 0.9)
_CONFORMAL_HALF_LIFE = 2.0
_CONFORMAL_GAMMA = 0.2
_CONFORMAL_WARMUP_DATES = 3


@dataclass
class GrowthModel:
    features: list[str] = field(default_factory=lambda: list(DOWNLOAD_FEATURES))
    model: lgb.LGBMRegressor | None = None
    metrics: dict = field(default_factory=dict)
    importances: dict = field(default_factory=dict)
    eval_provenance: dict = field(default_factory=dict)
    conformal: dict = field(default_factory=dict)  # level (str) -> DriftAwareConformal.to_dict()
    seed: int = 42
    horizon_days: int = 70

    def fit(self, df: pd.DataFrame) -> dict:
        """Fit with a date-grouped train/validation/test protocol.

        Early stopping sees only the validation dates. Once the iteration count is frozen, the
        deployable model is refit on train+validation and evaluated once against untouched,
        *unclipped* test outcomes.
        """
        df = df.dropna(subset=[GROWTH_TARGET_COLUMN]).copy()
        missing = [feature for feature in self.features if feature not in df]
        if missing:
            raise ValueError(f"missing growth features: {missing}")
        split = date_grouped_train_validation_test(df)
        train, validation, test = split.train, split.validation, split.test
        y_train_raw = train[GROWTH_TARGET_COLUMN].astype(float)
        y_validation_raw = validation[GROWTH_TARGET_COLUMN].astype(float)
        y_test_raw = test[GROWTH_TARGET_COLUMN].astype(float)

        tuning_model = self._new_model(_MAX_ESTIMATORS)
        tuning_model.fit(
            train[self.features].astype(float),
            y_train_raw.clip(_TARGET_MIN, _TARGET_MAX),
            eval_set=[(
                validation[self.features].astype(float),
                y_validation_raw.clip(_TARGET_MIN, _TARGET_MAX),
            )],
            eval_metric="l1",
            callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
        )
        best_iteration = int(tuning_model.best_iteration_ or _MAX_ESTIMATORS)

        development = pd.concat([train, validation], ignore_index=True)
        y_development = development[GROWTH_TARGET_COLUMN].astype(float)
        self.model = self._new_model(best_iteration)
        self.model.fit(
            development[self.features].astype(float),
            y_development.clip(_TARGET_MIN, _TARGET_MAX),
        )

        pred = self.model.predict(test[self.features].astype(float))
        rho = spearmanr(y_test_raw, pred).correlation if len(y_test_raw) > 2 else float("nan")
        self.eval_provenance = growth_evaluation_provenance(
            df, split, self.features, self.horizon_days
        )
        self.eval_provenance.update({
            "target_training_clip": [_TARGET_MIN, _TARGET_MAX],
            "test_target_clipped": False,
            "early_stopping_partition": "validation",
            "best_iteration": best_iteration,
        })
        self.metrics = {
            "mae": float(mean_absolute_error(y_test_raw, pred)),
            "rmse": float(math.sqrt(mean_squared_error(y_test_raw, pred))),
            "r2": float(r2_score(y_test_raw, pred)) if len(y_test_raw) > 2 else float("nan"),
            "spearman": float(rho),
            "n_train": int(len(train)),
            "n_validation": int(len(validation)),
            "n_test": int(len(test)),
            "n_train_dates": int(split.metadata["train"]["n_dates"]),
            "n_validation_dates": int(split.metadata["validation"]["n_dates"]),
            "n_test_dates": int(split.metadata["test"]["n_dates"]),
            "best_iteration": best_iteration,
            "train_clip_rate": float(
                ((y_train_raw < _TARGET_MIN) | (y_train_raw > _TARGET_MAX)).mean()
            ),
            "test_clip_rate": float(
                ((y_test_raw < _TARGET_MIN) | (y_test_raw > _TARGET_MAX)).mean()
            ),
        }
        self._calibrate_intervals(tuning_model, validation, test)
        imp = self.model.booster_.feature_importance(importance_type="gain")
        total = imp.sum() or 1
        self.importances = {
            feature: float(value / total)
            for feature, value in zip(self.features, imp, strict=False)
        }
        return self.metrics

    def _calibrate_intervals(
        self, tuning_model: lgb.LGBMRegressor, validation: pd.DataFrame, test: pd.DataFrame
    ) -> None:
        """Calibrate drift-aware conformal intervals on genuinely out-of-sample residuals.

        The train-only tuning model never saw the validation or test origins, so its absolute residuals
        there are valid calibration scores; they are applied to the deployed (train+validation) model,
        which can only be slightly better. Point-forecast test metrics above are unaffected.
        """
        self.conformal = {}
        pool = pd.concat([validation, test], ignore_index=True)
        dates = pd.to_datetime(pool["feature_date"]).dt.normalize()
        date_index = pd.factorize(dates, sort=True)[0]
        if date_index.max() + 1 <= _CONFORMAL_WARMUP_DATES:
            return
        actual = pool[GROWTH_TARGET_COLUMN].astype(float).to_numpy()
        predicted = tuning_model.predict(pool[self.features].astype(float))
        residuals = np.abs(actual - predicted)
        for level in INTERVAL_LEVELS:
            calibrator, evidence = DriftAwareConformal.calibrate(
                residuals, date_index, alpha=round(1 - level, 4),
                half_life=_CONFORMAL_HALF_LIFE, gamma=_CONFORMAL_GAMMA,
                warmup_dates=_CONFORMAL_WARMUP_DATES,
            )
            tag = int(round(level * 100))
            self.conformal[str(level)] = calibrator.to_dict()
            self.metrics[f"conformal_coverage_{tag}"] = float(evidence.coverage)
            self.metrics[f"conformal_width_{tag}"] = float(evidence.mean_width)
        self.metrics["conformal_n_dates_scored"] = float(evidence.n_dates_scored)
        self.metrics["conformal_n_calibration"] = float(len(residuals))

    def predict_interval(
        self, df: pd.DataFrame, level: float = 0.8
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """Lower/upper log-growth bounds at ``level``, or None if intervals were not calibrated."""
        blob = self.conformal.get(str(level))
        if self.model is None or not blob:
            return None
        return DriftAwareConformal.from_dict(blob).interval(self.predict(df))

    def _new_model(self, n_estimators: int) -> lgb.LGBMRegressor:
        return lgb.LGBMRegressor(
            n_estimators=n_estimators,
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

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        assert self.model is not None, "model not fitted"
        return self.model.predict(df[self.features].astype(float))

    def evaluate_closed_test(self, df: pd.DataFrame) -> tuple[dict, dict]:
        """Re-score an already-fitted champion on another candidate's exact closed cohort."""
        assert self.model is not None, "model not fitted"
        clean = df.dropna(subset=[GROWTH_TARGET_COLUMN]).copy()
        split = date_grouped_train_validation_test(clean)
        test = split.test
        actual = test[GROWTH_TARGET_COLUMN].astype(float)
        predicted = self.predict(test)
        rho = spearmanr(actual, predicted).correlation if len(actual) > 2 else float("nan")
        metrics = {
            "mae": float(mean_absolute_error(actual, predicted)),
            "rmse": float(math.sqrt(mean_squared_error(actual, predicted))),
            "r2": float(r2_score(actual, predicted)) if len(actual) > 2 else float("nan"),
            "spearman": float(rho),
            "n_test": int(len(test)),
        }
        return metrics, growth_evaluation_provenance(
            clean, split, self.features, self.horizon_days
        )

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
             "metrics": self.metrics, "importances": self.importances,
             "eval_provenance": self.eval_provenance, "horizon_days": self.horizon_days,
             "conformal": self.conformal},
            path,
        )

    @classmethod
    def load(cls, path: str) -> GrowthModel:
        blob = joblib.load(path)
        return cls(features=blob["features"], model=blob["model"],
                   metrics=blob.get("metrics", {}), importances=blob.get("importances", {}),
                   eval_provenance=blob.get("eval_provenance", {}),
                   horizon_days=blob.get("horizon_days", 70),
                   conformal=blob.get("conformal", {}))

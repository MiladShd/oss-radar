"""Model registry: champion/challenger promotion, artifact persistence, MLflow tracking.

Promotion rule: a newly trained model becomes champion if its primary metric beats the best
previously-recorded champion for that model (or if there is no prior champion). Either way the
artifact and every metric are persisted so the dashboard can chart model improvement over time.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import structlog

from oss_radar.config import Settings, get_settings
from oss_radar.warehouse.base import Warehouse

log = structlog.get_logger(__name__)

# primary metric per model and whether higher is better
PRIMARY_METRIC = {"growth": ("spearman", True), "risk": ("auc", True)}


class ModelRegistry:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.local_dir = Path("models_local")
        self.local_dir.mkdir(exist_ok=True)

    # --- artifact storage ---

    def _upload_gcs(self, local_path: Path, model_name: str, version: str) -> str | None:
        try:
            from google.cloud import storage

            client = storage.Client(project=self.settings.gcp_project)
            bucket = client.bucket(self.settings.artifact_bucket)
            if not bucket.exists():
                bucket = client.create_bucket(bucket, location=self.settings.region)
            blob = bucket.blob(f"models/{model_name}/{version}.pkl")
            blob.upload_from_filename(str(local_path))
            return f"gs://{self.settings.artifact_bucket}/models/{model_name}/{version}.pkl"
        except Exception as exc:  # noqa: BLE001
            log.warning("registry.gcs_upload_failed", error=str(exc))
            return None

    # --- promotion ---

    def _prev_best(self, wh: Warehouse, model_name: str, metric: str) -> float | None:
        try:
            df = wh.query_df(
                f"SELECT metric_value FROM model_runs "
                f"WHERE model_name = '{model_name}' AND metric_name = '{metric}' "
                f"AND is_champion = TRUE"
            )
        except Exception:
            return None
        vals = [v for v in df.get("metric_value", []) if v == v]
        return max(vals) if vals else None

    def persist(
        self, wh: Warehouse, run_id: str, model_name: str, model_obj, metrics: dict, params: dict
    ) -> tuple[bool, list[dict]]:
        version = f"{model_name}-{run_id}"
        local_path = self.local_dir / f"{version}.pkl"
        model_obj.save(str(local_path))
        gcs_uri = self._upload_gcs(local_path, model_name, version) if self.settings.is_cloud else str(local_path)

        metric_name, higher_better = PRIMARY_METRIC[model_name]
        new_val = metrics.get(metric_name)
        prev = self._prev_best(wh, model_name, metric_name)
        is_champion = True
        if new_val is None or new_val != new_val:  # NaN -> not a valid champion candidate
            is_champion = False
        elif prev is not None:
            is_champion = new_val >= prev if higher_better else new_val <= prev

        self._mlflow_log(model_name, version, params, metrics, is_champion)

        now = datetime.now(UTC)
        rows = [
            {
                "run_id": run_id, "model_name": model_name, "trained_at": now, "version": version,
                "metric_name": mname, "metric_value": float(mval) if isinstance(mval, (int, float)) else None,
                "n_train": metrics.get("n_train"), "n_test": metrics.get("n_test"),
                "params": params, "is_champion": is_champion, "gcs_uri": gcs_uri or "",
                "notes": metrics.get("note", ""),
            }
            for mname, mval in metrics.items()
            if isinstance(mval, (int, float))
        ]
        log.info("registry.persisted", model=model_name, version=version,
                 champion=is_champion, primary=f"{metric_name}={new_val}", prev=prev)
        return is_champion, rows

    def _mlflow_log(self, model_name, version, params, metrics, is_champion) -> None:
        try:
            import mlflow

            mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "file:./mlruns"))
            mlflow.set_experiment(f"oss-radar-{model_name}")
            with mlflow.start_run(run_name=version):
                mlflow.log_params({k: str(v) for k, v in params.items()})
                mlflow.log_metrics({k: float(v) for k, v in metrics.items()
                                    if isinstance(v, (int, float)) and v == v})
                mlflow.set_tag("is_champion", is_champion)
        except Exception as exc:  # noqa: BLE001
            log.debug("registry.mlflow_skipped", error=str(exc))

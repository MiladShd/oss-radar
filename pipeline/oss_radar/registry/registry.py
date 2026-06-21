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
# A new model must beat the best-ever champion by at least this margin to be promoted.
PROMOTION_MARGIN = {"growth": 0.0, "risk": 0.0}


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
        self, wh: Warehouse, run_id: str, model_name: str, model_obj, metrics: dict, params: dict,
        gate_passed: bool | None = None,
    ) -> tuple[bool, list[dict]]:
        version = f"{model_name}-{run_id}"
        local_path = self.local_dir / f"{version}.pkl"
        model_obj.save(str(local_path))
        gcs_uri = self._upload_gcs(local_path, model_name, version) if self.settings.is_cloud else str(local_path)

        metric_name, higher_better = PRIMARY_METRIC[model_name]
        margin = PROMOTION_MARGIN.get(model_name, 0.0)
        new_val = metrics.get(metric_name)
        prev = self._prev_best(wh, model_name, metric_name)

        # Promote only on genuine improvement over the best-ever champion (strict, beyond margin).
        if new_val is None or new_val != new_val:  # NaN -> not a valid champion candidate
            beats = False
            note = f"not promoted: {metric_name} unavailable"
        elif prev is None:
            beats = True
            note = f"first champion: {metric_name}={new_val:.3f}"
        else:
            beats = (new_val > prev + margin) if higher_better else (new_val < prev - margin)
            cmp = ">" if higher_better else "<"
            note = (
                f"promoted: {metric_name}={new_val:.3f} {cmp} prev best {prev:.3f}"
                if beats
                else f"held challenger: {metric_name}={new_val:.3f} did not beat best {prev:.3f}"
            )
        # HARD VALIDATION GATE: a model is served only if it also clears the validation gate
        # (leak-free, beats the fair baseline, generalises). So is_champion == TRUE always implies
        # the gate passed, which is why "last-good champion" below needs no separate flag.
        is_champion = beats
        if gate_passed is False:
            is_champion = False
            note = f"BLOCKED by validation gate ({note})"
        metrics["promotion_note"] = note

        self._mlflow_log(model_name, version, params, metrics, is_champion)

        now = datetime.now(UTC)
        rows = [
            {
                "run_id": run_id, "model_name": model_name, "trained_at": now, "version": version,
                "metric_name": mname, "metric_value": float(mval) if isinstance(mval, (int, float)) else None,
                "n_train": metrics.get("n_train"), "n_test": metrics.get("n_test"),
                "params": params, "is_champion": is_champion, "gcs_uri": gcs_uri or "",
                "notes": note,
            }
            for mname, mval in metrics.items()
            if isinstance(mval, (int, float))
        ]
        log.info("registry.persisted", model=model_name, version=version,
                 champion=is_champion, primary=f"{metric_name}={new_val}", prev=prev)
        return is_champion, rows

    # --- auto-rollback: load the last-good (gate-passed) champion artifact ---

    def load_champion(self, wh: Warehouse, model_name: str, model_cls):
        """Return (model, version) for the most recently promoted champion, or (None, None).

        Because promotion now requires the validation gate, every is_champion row is by
        construction a gate-passed model — so this IS the "last-good" model to roll back to
        when a freshly-trained candidate fails the gate."""
        try:
            df = wh.query_df(
                f"SELECT version, gcs_uri, trained_at FROM model_runs "
                f"WHERE model_name = '{model_name}' AND is_champion = TRUE AND gcs_uri != '' "
                f"ORDER BY trained_at DESC LIMIT 1"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("registry.load_champion_query_failed", model=model_name, error=str(exc))
            return None, None
        if df.empty:
            return None, None
        uri, version = df.iloc[0]["gcs_uri"], df.iloc[0]["version"]
        try:
            path = self._materialize(uri, version)
            return model_cls.load(path), version
        except Exception as exc:  # noqa: BLE001
            log.warning("registry.load_champion_failed", model=model_name, uri=uri, error=str(exc))
            return None, None

    def _materialize(self, uri: str, version: str) -> str:
        """Resolve a stored artifact URI to a local path (downloading from GCS if needed)."""
        if not uri.startswith("gs://"):
            return uri  # local backend stores the filesystem path directly
        local_path = self.local_dir / f"{version}.pkl"
        if not local_path.exists():
            from google.cloud import storage

            _, _, rest = uri.partition("gs://")
            bucket_name, _, blob_name = rest.partition("/")
            client = storage.Client(project=self.settings.gcp_project)
            client.bucket(bucket_name).blob(blob_name).download_to_filename(str(local_path))
        return str(local_path)

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

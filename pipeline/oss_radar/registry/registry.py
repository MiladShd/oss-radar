"""Model registry: cohort-aware promotion, artifact persistence, and MLflow tracking.

Metrics are comparable only inside the same versioned evaluation lineage and benchmark. Growth
incumbents can also be re-scored on a challenger's exact closed cohort. Every candidate is
persisted so the dashboard shows both promotions and held runs without implying monotonic quality.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import structlog

from oss_radar.config import Settings, get_settings
from oss_radar.warehouse.base import Warehouse

log = structlog.get_logger(__name__)

# primary metric per model and whether higher is better
PRIMARY_METRIC = {"growth": ("spearman", True), "risk": ("group_auc", True)}
# A comparable candidate must beat its incumbent by at least this margin to be promoted.
PROMOTION_MARGIN = {"growth": 0.0, "risk": 0.0}
BOOTSTRAP_FLOOR = {"risk": 0.55}


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
            # Terraform owns bucket creation and lifecycle. The runtime identity intentionally has
            # object-level access only, so it must not probe/create bucket metadata here.
            bucket = client.bucket(self.settings.artifact_bucket)
            blob = bucket.blob(f"models/{model_name}/{version}.pkl")
            blob.upload_from_filename(str(local_path))
            return f"gs://{self.settings.artifact_bucket}/models/{model_name}/{version}.pkl"
        except Exception as exc:  # noqa: BLE001
            log.warning("registry.gcs_upload_failed", error=str(exc))
            return None

    # --- promotion ---

    def _prev_best(
        self,
        wh: Warehouse,
        model_name: str,
        metric: str,
        eval_provenance: dict | None = None,
    ) -> tuple[float | None, str | None]:
        try:
            df = wh.query_df(
                f"SELECT metric_value, eval_provenance FROM model_runs "
                f"WHERE model_name = '{model_name}' AND metric_name = '{metric}' "
                f"AND is_champion = TRUE"
            )
        except Exception:
            return None, None

        candidates: list[tuple[float, str | None]] = []
        for _, row in df.iterrows():
            value = row.get("metric_value")
            if value is None or value != value:
                continue
            previous_provenance = _decode_json(row.get("eval_provenance"))
            if eval_provenance and not evaluation_lineage_matches(
                eval_provenance, previous_provenance
            ):
                continue
            current_hash = (eval_provenance or {}).get("benchmark_hash")
            previous_hash = previous_provenance.get("benchmark_hash")
            if current_hash and current_hash != previous_hash:
                continue
            candidates.append((
                float(value),
                previous_hash,
            ))
        if not candidates:
            return None, None
        best = max(candidates, key=lambda item: item[0])
        return best

    def _has_compatible_champion(
        self,
        wh: Warehouse,
        model_name: str,
        eval_provenance: dict | None,
    ) -> bool:
        """Return whether this evaluation lineage already has a champion.

        A legacy or differently-labelled champion must not prevent the first honest champion in a
        new lineage from being established. Once a compatible lineage exists, however, a changed
        benchmark is held until the incumbent can be evaluated fairly on that benchmark.
        """
        try:
            df = wh.query_df(
                f"SELECT eval_provenance FROM model_runs "
                f"WHERE model_name = '{model_name}' AND is_champion = TRUE AND gcs_uri != ''"
            )
        except Exception:
            return False
        current = eval_provenance or {}
        return any(
            evaluation_lineage_matches(current, _decode_json(row.get("eval_provenance")))
            for _, row in df.iterrows()
        )

    def persist(
        self, wh: Warehouse, run_id: str, model_name: str, model_obj, metrics: dict, params: dict,
        gate_passed: bool | None = None, incumbent_metric: float | None = None,
        incumbent_version: str | None = None, comparison_provenance: dict | None = None,
        compatible_incumbent_available: bool | None = None,
    ) -> tuple[bool, list[dict]]:
        version = f"{model_name}-{run_id}"
        model_obj.version = version
        local_path = self.local_dir / f"{version}.pkl"
        model_obj.save(str(local_path))
        gcs_uri = self._upload_gcs(local_path, model_name, version) if self.settings.is_cloud else str(local_path)

        metric_name, higher_better = PRIMARY_METRIC[model_name]
        margin = PROMOTION_MARGIN.get(model_name, 0.0)
        new_val = metrics.get(metric_name)
        eval_provenance = getattr(model_obj, "eval_provenance", {}) or {}
        prev, _ = self._prev_best(
            wh, model_name, metric_name, eval_provenance
        )
        comparison_mode = "same-persisted-benchmark"
        current_hash = eval_provenance.get("benchmark_hash")
        comparison_hash = (comparison_provenance or {}).get("benchmark_hash")
        matched_incumbent = (
            incumbent_version
            and incumbent_metric is not None
            and incumbent_metric == incumbent_metric
            and current_hash
            and current_hash == comparison_hash
            and evaluation_lineage_matches(
                eval_provenance, comparison_provenance or {}
            )
        )
        if matched_incumbent:
            prev = float(incumbent_metric)
            comparison_mode = "incumbent-rescored-current-benchmark"
        has_compatible_champion = self._has_compatible_champion(
            wh, model_name, eval_provenance
        )
        if compatible_incumbent_available is not None:
            has_compatible_champion = compatible_incumbent_available
            if not compatible_incumbent_available and not matched_incumbent:
                # A registry row whose artifact cannot be materialized must not permanently block
                # recovery. The newly persisted candidate can re-bootstrap this evaluation lineage.
                prev = None

        # Promote only on a genuine, comparable improvement (strict, beyond margin).
        if new_val is None or new_val != new_val:  # NaN -> not a valid champion candidate
            beats = False
            note = f"not promoted: {metric_name} unavailable"
        elif prev is None and not has_compatible_champion:
            floor = BOOTSTRAP_FLOOR.get(model_name)
            beats = floor is None or new_val >= floor
            if beats:
                note = f"first champion in evaluation lineage: {metric_name}={new_val:.3f}"
                comparison_mode = "first-champion-in-lineage"
            else:
                note = (
                    f"held bootstrap candidate: {metric_name}={new_val:.3f} "
                    f"is below absolute floor {floor:.3f}"
                )
                comparison_mode = "bootstrap-floor"
        elif prev is None:
            beats = False
            comparison_mode = "not-comparable"
            benchmark_label = current_hash[:10] if current_hash else "unknown"
            note = (
                f"held challenger: current benchmark {benchmark_label} "
                "has no matched incumbent evaluation"
            )
        else:
            beats = (new_val > prev + margin) if higher_better else (new_val < prev - margin)
            cmp = ">" if higher_better else "<"
            note = (
                f"promoted: {metric_name}={new_val:.3f} {cmp} prev best {prev:.3f}"
                if beats
                else f"held challenger: {metric_name}={new_val:.3f} did not beat best {prev:.3f}"
            )
            if matched_incumbent:
                note += f"; incumbent {incumbent_version} re-scored on benchmark {current_hash[:10]}"
        # The hard validation gate applies to growth. Risk promotion is instead constrained by
        # its versioned package-disjoint metric lineage; do not imply the growth gate covers both.
        is_champion = beats
        if gate_passed is False:
            is_champion = False
            note = f"BLOCKED by validation gate ({note})"
        if self.settings.is_cloud and not gcs_uri:
            # A model that cannot survive this process cannot be a production champion. Keep its
            # metrics as a held candidate, but never serve or advertise an empty artifact URI.
            is_champion = False
            note = f"BLOCKED by artifact persistence failure ({note})"
        metrics["promotion_note"] = note

        self._mlflow_log(model_name, version, params, metrics, is_champion)

        now = datetime.now(UTC)
        rows = [
            {
                "run_id": run_id, "model_name": model_name, "trained_at": now, "version": version,
                "metric_name": mname, "metric_value": float(mval) if isinstance(mval, (int, float)) else None,
                "n_train": metrics.get("n_train"), "n_test": metrics.get("n_test"),
                "params": params, "is_champion": is_champion, "gcs_uri": gcs_uri or "",
                "notes": note, "served_version": version if is_champion else "",
                "eval_provenance": eval_provenance,
                "comparison_version": (
                    incumbent_version if mname == metric_name and matched_incumbent else ""
                ),
                "comparison_metric_value": (
                    prev if mname == metric_name and prev is not None else None
                ),
                "comparison_mode": comparison_mode if mname == metric_name else "",
            }
            for mname, mval in metrics.items()
            if isinstance(mval, (int, float))
        ]
        log.info("registry.persisted", model=model_name, version=version,
                 champion=is_champion, primary=f"{metric_name}={new_val}", prev=prev)
        return is_champion, rows

    # --- rollback: load the most recent compatible promoted artifact ---

    def load_champion(
        self,
        wh: Warehouse,
        model_name: str,
        model_cls,
        required_provenance: dict | None = None,
    ):
        """Return (model, version) for the most recently promoted champion, or (None, None).

        For growth, promoted rows have also cleared the configured validation gate. Risk uses a
        separate versioned, package-disjoint evaluation lineage."""
        try:
            df = wh.query_df(
                f"SELECT version, gcs_uri, trained_at, eval_provenance FROM model_runs "
                f"WHERE model_name = '{model_name}' AND is_champion = TRUE AND gcs_uri != '' "
                f"ORDER BY trained_at DESC"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("registry.load_champion_query_failed", model=model_name, error=str(exc))
            return None, None
        if df.empty:
            return None, None
        for _, row in df.drop_duplicates(subset=["version"]).iterrows():
            provenance = _decode_json(row.get("eval_provenance"))
            if required_provenance and not evaluation_lineage_matches(
                required_provenance, provenance
            ):
                continue
            uri, version = row["gcs_uri"], row["version"]
            try:
                path = self._materialize(uri, version)
                return model_cls.load(path), version
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "registry.load_champion_failed",
                    model=model_name,
                    uri=uri,
                    error=str(exc),
                )
        return None, None

    def select_for_serving(
        self, wh: Warehouse, model_name: str, candidate, candidate_promoted: bool, model_cls,
    ):
        """Serve only a promoted candidate or an already persisted champion.

        Registration rows are written after scoring, so a candidate promoted during this run is
        returned directly. A held or invalid candidate is never served merely because training
        completed; it resolves to the last promoted artifact instead.
        """
        candidate_version = getattr(candidate, "version", None) or None
        if candidate_promoted and getattr(candidate, "model", None) is not None:
            return candidate, candidate_version
        required_provenance = getattr(candidate, "eval_provenance", {}) or {}
        return self.load_champion(
            wh,
            model_name,
            model_cls,
            required_provenance=required_provenance or None,
        )

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


def _decode_json(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value or not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, ValueError):
        return {}


def evaluation_lineage_matches(current: dict, previous: dict) -> bool:
    """Compare metrics inside the same label/split contract.

    ``feature_set_hash`` is intentionally excluded: testing a changed feature set on the same
    benchmark is the purpose of champion/challenger comparison.
    """
    if not current:
        return not previous
    if not previous:
        return False
    keys = (
        "schema_version",
        "label_version",
        "benchmark_kind",
        "split_version",
    )
    return all(current.get(key) == previous.get(key) for key in keys)

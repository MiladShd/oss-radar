"""Select a warehouse backend from settings."""

from __future__ import annotations

from oss_radar.config import Settings, get_settings
from oss_radar.warehouse.base import Warehouse


def get_warehouse(settings: Settings | None = None) -> Warehouse:
    settings = settings or get_settings()
    if settings.backend == "bigquery":
        from oss_radar.warehouse.bigquery_backend import BigQueryWarehouse

        if not settings.gcp_project:
            raise RuntimeError("backend=bigquery requires a GCP project (set OSS_RADAR_GCP_PROJECT)")
        return BigQueryWarehouse(
            project=settings.gcp_project,
            dataset=settings.bq_dataset,
            location=settings.region,
        )
    from oss_radar.warehouse.duckdb_backend import DuckDBWarehouse

    return DuckDBWarehouse(path=settings.duckdb_path)

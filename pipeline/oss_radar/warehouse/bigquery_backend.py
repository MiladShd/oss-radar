"""BigQuery backend — the managed warehouse used in the Cloud Run job.

Uses load jobs (not streaming inserts) for batch appends, and sets a default dataset
so the same bare-table-name SQL used by DuckDB resolves transparently.
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import structlog
from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from oss_radar.warehouse import schema as S
from oss_radar.warehouse.base import Warehouse

log = structlog.get_logger(__name__)


class BigQueryWarehouse(Warehouse):
    def __init__(self, project: str, dataset: str = "oss_radar", location: str = "us-central1"):
        self.project = project
        self.dataset = dataset
        self.location = location
        self._client = bigquery.Client(project=project)
        self._ds_ref = bigquery.DatasetReference(project, dataset)

    def _table_id(self, table: str) -> str:
        return f"{self.project}.{self.dataset}.{table}"

    def init_schema(self) -> None:
        try:
            self._client.get_dataset(self._ds_ref)
        except NotFound:
            ds = bigquery.Dataset(self._ds_ref)
            ds.location = self.location
            self._client.create_dataset(ds)
            log.info("bq.dataset_created", dataset=self.dataset)
        for table, cols in S.TABLES.items():
            tbl_id = self._table_id(table)
            schema = [bigquery.SchemaField(name, S.BIGQUERY_TYPES[ctype]) for name, ctype in cols]
            try:
                self._client.get_table(tbl_id)
            except NotFound:
                self._client.create_table(bigquery.Table(tbl_id, schema=schema))
                log.info("bq.table_created", table=table)

    def insert_rows(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        prepared = self.prepare_rows(table, rows)
        # BigQuery load_from_json wants DATE/TIMESTAMP as ISO strings.
        payload = []
        for r in prepared:
            rec = {}
            for k, v in r.items():
                if isinstance(v, (date, datetime)):
                    rec[k] = v.isoformat()
                else:
                    rec[k] = v
            payload.append(rec)
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema=[bigquery.SchemaField(n, S.BIGQUERY_TYPES[t]) for n, t in S.TABLES[table]],
        )
        job = self._client.load_table_from_json(payload, self._table_id(table), job_config=job_config)
        job.result()
        return len(payload)

    def query_df(self, sql: str) -> pd.DataFrame:
        job_config = bigquery.QueryJobConfig(default_dataset=self._ds_ref)
        result = self._client.query(sql, job_config=job_config).result()
        return pd.DataFrame([dict(row) for row in result])

    def truncate(self, table: str) -> None:
        self._client.query(f"TRUNCATE TABLE `{self._table_id(table)}`").result()

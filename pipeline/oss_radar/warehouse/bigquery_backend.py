"""BigQuery backend — the managed warehouse used in the Cloud Run job.

Uses load jobs (not streaming inserts) for batch appends, and sets a default dataset
so the same bare-table-name SQL used by DuckDB resolves transparently.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import uuid4

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
                tbl = self._client.get_table(tbl_id)
            except NotFound:
                self._client.create_table(bigquery.Table(tbl_id, schema=schema))
                log.info("bq.table_created", table=table)
            else:
                existing = {field.name for field in tbl.schema}
                missing = [field for field in schema if field.name not in existing]
                if missing:
                    tbl.schema = list(tbl.schema) + missing
                    self._client.update_table(tbl, ["schema"])
                    log.info("bq.table_schema_updated", table=table, added=[f.name for f in missing])

    def insert_rows(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        prepared = self.prepare_rows(table, rows)
        payload = self._json_payload(prepared)
        job_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            schema=[bigquery.SchemaField(n, S.BIGQUERY_TYPES[t]) for n, t in S.TABLES[table]],
        )
        job = self._client.load_table_from_json(payload, self._table_id(table), job_config=job_config)
        job.result()
        return len(payload)

    @staticmethod
    def _json_payload(prepared: list[dict]) -> list[dict]:
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
        return payload

    def upsert_rows(self, table: str, rows: list[dict], key_columns: list[str]) -> int:
        if not rows:
            return 0
        prepared = self.prepare_upsert_rows(table, rows, key_columns)
        if not prepared:
            return 0

        columns = [name for name, _ in S.TABLES[table]]
        schema = [bigquery.SchemaField(n, S.BIGQUERY_TYPES[t]) for n, t in S.TABLES[table]]
        stage_name = f"{table}__upsert_{uuid4().hex}"
        stage_id = self._table_id(stage_name)
        target_id = self._table_id(table)
        payload = self._json_payload(prepared)
        load_config = bigquery.LoadJobConfig(
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
            schema=schema,
        )
        match = " AND ".join(f"T.{key} = S.{key}" for key in key_columns)
        updates = ", ".join(
            f"T.{column} = S.{column}" for column in columns if column not in key_columns
        )
        insert_columns = ", ".join(f"`{column}`" for column in columns)
        insert_values = ", ".join(f"S.`{column}`" for column in columns)
        matched_clause = f"WHEN MATCHED THEN UPDATE SET {updates} " if updates else ""
        merge_sql = (f"MERGE `{target_id}` T USING `{stage_id}` S ON {match} "
                     f"{matched_clause}WHEN NOT MATCHED THEN INSERT ({insert_columns}) "
                     f"VALUES ({insert_values})")

        try:
            self._client.load_table_from_json(payload, stage_id, job_config=load_config).result()
            self._client.query(merge_sql).result()
        finally:
            self._client.delete_table(stage_id, not_found_ok=True)
        return len(payload)

    @staticmethod
    def _query_parameter(value: Any) -> bigquery.ScalarQueryParameter:
        """Build a positional BigQuery parameter without interpolating values."""
        if isinstance(value, bool):
            type_name = "BOOL"
        elif isinstance(value, int):
            type_name = "INT64"
        elif isinstance(value, float):
            type_name = "FLOAT64"
        elif isinstance(value, datetime):
            type_name = "TIMESTAMP"
        elif isinstance(value, date):
            type_name = "DATE"
        else:
            type_name = "STRING"
        return bigquery.ScalarQueryParameter(None, type_name, value)

    def query_df(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> pd.DataFrame:
        job_config = bigquery.QueryJobConfig(
            default_dataset=self._ds_ref,
            query_parameters=[self._query_parameter(value) for value in (params or [])],
        )
        result = self._client.query(sql, job_config=job_config).result()
        return pd.DataFrame([dict(row) for row in result])

    def truncate(self, table: str) -> None:
        self._client.query(f"TRUNCATE TABLE `{self._table_id(table)}`").result()

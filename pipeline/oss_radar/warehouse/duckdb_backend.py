"""Local DuckDB backend — a single file, zero cloud dependencies."""

from __future__ import annotations

from typing import Any

import duckdb
import pandas as pd
import structlog

from oss_radar.warehouse import schema as S
from oss_radar.warehouse.base import Warehouse

log = structlog.get_logger(__name__)


class DuckDBWarehouse(Warehouse):
    def __init__(self, path: str = "oss_radar.duckdb"):
        self.path = path
        self._con = duckdb.connect(path)

    def init_schema(self) -> None:
        for table, cols in S.TABLES.items():
            coldefs = ", ".join(f'"{name}" {S.DUCKDB_TYPES[ctype]}' for name, ctype in cols)
            self._con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({coldefs})')
            existing = {row[1] for row in self._con.execute(f'PRAGMA table_info("{table}")').fetchall()}
            for name, ctype in cols:
                if name not in existing:
                    self._con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {S.DUCKDB_TYPES[ctype]}')
        log.info("duckdb.schema_ready", path=self.path, tables=len(S.TABLES))

    def insert_rows(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        prepared = self.prepare_rows(table, rows)
        cols = [name for name, _ in S.TABLES[table]]
        placeholders = ", ".join("?" for _ in cols)
        colnames = ", ".join(f'"{c}"' for c in cols)
        sql = f'INSERT INTO "{table}" ({colnames}) VALUES ({placeholders})'
        data = [[r[c] for c in cols] for r in prepared]
        self._con.executemany(sql, data)
        return len(data)

    def upsert_rows(self, table: str, rows: list[dict], key_columns: list[str]) -> int:
        if not rows:
            return 0
        prepared = self.prepare_upsert_rows(table, rows, key_columns)
        if not prepared:
            return 0

        columns = [name for name, _ in S.TABLES[table]]
        batch_name = "_oss_radar_upsert_batch"
        batch = pd.DataFrame(prepared, columns=columns)
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        match = " AND ".join(
            f't."{column}" IS NOT DISTINCT FROM s."{column}"' for column in key_columns
        )

        self._con.register(batch_name, batch)
        try:
            self._con.execute("BEGIN TRANSACTION")
            self._con.execute(
                f'DELETE FROM "{table}" AS t USING "{batch_name}" AS s WHERE {match}'
            )
            self._con.execute(
                f'INSERT INTO "{table}" ({quoted_columns}) '
                f'SELECT {quoted_columns} FROM "{batch_name}"'
            )
            self._con.execute("COMMIT")
        except Exception:
            self._con.execute("ROLLBACK")
            raise
        finally:
            self._con.unregister(batch_name)
        return len(prepared)

    def query_df(
        self, sql: str, params: list[Any] | tuple[Any, ...] | None = None
    ) -> pd.DataFrame:
        return self._con.execute(sql, params or []).fetch_df()

    def truncate(self, table: str) -> None:
        self._con.execute(f'DELETE FROM "{table}"')

    def close(self) -> None:
        self._con.close()

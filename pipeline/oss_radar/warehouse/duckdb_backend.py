"""Local DuckDB backend — a single file, zero cloud dependencies."""

from __future__ import annotations

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

    def query_df(self, sql: str) -> pd.DataFrame:
        return self._con.execute(sql).fetch_df()

    def truncate(self, table: str) -> None:
        self._con.execute(f'DELETE FROM "{table}"')

    def close(self) -> None:
        self._con.close()

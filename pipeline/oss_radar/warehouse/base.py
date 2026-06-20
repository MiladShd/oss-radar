"""Warehouse abstraction + shared row coercion.

The same SQL (bare table names, portable subset) and the same row dicts work against
both backends. Heavy date arithmetic is done in pandas, not SQL, to stay portable.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import Any

import pandas as pd
from dateutil import parser as dtparser

from oss_radar.warehouse import schema as S


def _coerce(value: Any, col_type: str) -> Any:
    if value is None:
        return None
    try:
        if col_type == "JSON":
            return value if isinstance(value, str) else json.dumps(value, default=str)
        if col_type == "DATE":
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            return dtparser.parse(str(value)).date()
        if col_type == "TIMESTAMP":
            if isinstance(value, datetime):
                return value
            return dtparser.parse(str(value))
        if col_type == "INT":
            if isinstance(value, float) and value != value:  # NaN
                return None
            return int(value)
        if col_type == "FLOAT":
            f = float(value)
            return None if f != f else f  # drop NaN
        if col_type == "BOOL":
            return bool(value)
        return str(value)
    except (ValueError, TypeError, OverflowError):
        return None


class Warehouse(ABC):
    """Backend-agnostic warehouse interface."""

    def prepare_rows(self, table: str, rows: list[dict]) -> list[dict]:
        cols = S.TABLES[table]
        out = []
        for row in rows:
            out.append({name: _coerce(row.get(name), ctype) for name, ctype in cols})
        return out

    @abstractmethod
    def init_schema(self) -> None: ...

    @abstractmethod
    def insert_rows(self, table: str, rows: list[dict]) -> int: ...

    @abstractmethod
    def query_df(self, sql: str) -> pd.DataFrame: ...

    @abstractmethod
    def truncate(self, table: str) -> None: ...

    # --- convenience helpers (portable SQL) ---

    def table_names(self) -> list[str]:
        return list(S.TABLES.keys())

    def count(self, table: str) -> int:
        df = self.query_df(f"SELECT COUNT(*) AS n FROM {table}")
        return int(df.iloc[0]["n"]) if not df.empty else 0

"""Pipeline run liveness and terminal-status contracts."""

from __future__ import annotations

import pandas as pd
import pytest

from oss_radar.config import Settings
from oss_radar.orchestrator import pipeline


class _FailureWarehouse:
    def __init__(self):
        self.rows: list[dict] = []

    def init_schema(self) -> None:
        return None

    def query_df(self, _query: str, _params=()) -> pd.DataFrame:
        return pd.DataFrame()

    def upsert_rows(self, table: str, rows: list[dict], keys: list[str]) -> int:
        assert table == "pipeline_runs"
        assert keys == ["run_id"]
        self.rows.extend(rows)
        return len(rows)


def test_unhandled_pipeline_exception_is_persisted_as_failed(monkeypatch):
    warehouse = _FailureWarehouse()

    def explode(*_args, **_kwargs):
        raise ConnectionError("upstream unavailable")

    monkeypatch.setattr(pipeline, "_execute_pipeline", explode)
    monkeypatch.setattr(pipeline, "get_warehouse", lambda _settings: warehouse)

    with pytest.raises(ConnectionError):
        pipeline.run_pipeline(Settings(backend="duckdb", env="test"))

    assert warehouse.rows[-1]["status"] == "failed"
    assert warehouse.rows[-1]["finished_at"] is not None
    assert warehouse.rows[-1]["counts"] == {"error_type": "ConnectionError"}

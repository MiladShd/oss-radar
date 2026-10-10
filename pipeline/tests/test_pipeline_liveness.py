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


class _OrphanWarehouse(_FailureWarehouse):
    def __init__(self, running: pd.DataFrame):
        super().__init__()
        self._running = running

    def query_df(self, query: str, _params=()) -> pd.DataFrame:
        return self._running if "status = 'running'" in query else pd.DataFrame()


def _running_row(run_id: str, hours_ago: float) -> dict:
    from datetime import UTC, datetime, timedelta

    return {
        "run_id": run_id, "started_at": datetime.now(UTC) - timedelta(hours=hours_ago),
        "finished_at": None, "status": "running", "stages": {"ingest": 12.0}, "counts": {},
        "git_sha": "abc1234",
    }


def test_orphaned_runs_are_closed_but_recent_ones_are_left_alone():
    running = pd.DataFrame([
        _running_row("killed-weeks-ago", 360.0),
        _running_row("killed-yesterday", 20.0),
        _running_row("still-in-progress", 0.2),
        _running_row("this-run", 5.0),
    ])
    warehouse = _OrphanWarehouse(running)

    closed = pipeline._close_orphaned_runs(warehouse, "this-run")

    assert closed == 2
    by_id = {row["run_id"]: row for row in warehouse.rows}
    assert set(by_id) == {"killed-weeks-ago", "killed-yesterday"}
    for row in by_id.values():
        assert row["status"] == "failed"
        assert row["counts"]["error_type"] == "OrphanedRun"
        assert "timeout" in row["counts"]["reason"]
        assert row["stages"] == {"ingest": 12.0}  # history is preserved, only the outcome is added
        assert row["git_sha"] == "abc1234"


def test_orphan_cleanup_never_blocks_a_run():
    class _Broken(_FailureWarehouse):
        def query_df(self, *_a, **_k):
            raise RuntimeError("warehouse unavailable")

    assert pipeline._close_orphaned_runs(_Broken(), "x") == 0


def test_sigterm_is_recorded_as_a_failed_run_with_a_reason(monkeypatch):
    """Cloud Run sends SIGTERM when the job timeout is reached; the run must say so, not stay 'running'."""
    import os
    import signal

    warehouse = _FailureWarehouse()

    def killed_midway(*_args, **_kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler should have interrupted the run")  # pragma: no cover

    monkeypatch.setattr(pipeline, "_execute_pipeline", killed_midway)
    monkeypatch.setattr(pipeline, "get_warehouse", lambda _settings: warehouse)
    before = signal.getsignal(signal.SIGTERM)

    with pytest.raises(pipeline.PipelineInterrupted):
        pipeline.run_pipeline(Settings(backend="duckdb", env="test"))

    row = warehouse.rows[-1]
    assert row["status"] == "failed"
    assert row["counts"]["error_type"] == "PipelineInterrupted"
    assert "job timeout" in row["counts"]["reason"]
    assert signal.getsignal(signal.SIGTERM) == before  # handler restored afterwards

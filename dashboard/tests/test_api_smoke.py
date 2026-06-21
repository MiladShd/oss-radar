"""Dashboard API smoke tests.

Proves the FastAPI app imports, starts, and serves useful JSON against a
warehouse seeded with minimal valid rows — using a temporary DuckDB file, never
the developer's local ``oss_radar.duckdb``.

The production app deliberately swallows query errors into empty defaults (see
``dashboard/app/main.py:_safe``), so a missing table or broken query would look
like a healthy-but-empty dashboard. To stop that hiding regressions, these tests
seed real rows and assert the endpoints return *that data* — a missing table then
collapses the response to the empty default and fails the assertion (and CI).
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from oss_radar.warehouse.duckdb_backend import DuckDBWarehouse

RUN_ID = "smoke-run"
_NOW = dt.datetime(2026, 6, 20, 12, 0, 0)
_TODAY = dt.date(2026, 6, 20)


def _seed(path: str) -> DuckDBWarehouse:
    """A two-package warehouse with just enough rows for the read queries."""
    wh = DuckDBWarehouse(path=path)
    wh.init_schema()
    wh.insert_rows("pipeline_runs", [{
        "run_id": RUN_ID, "started_at": _NOW, "finished_at": _NOW,
        "status": "success", "stages": {"ingest": "ok", "train": "ok"},
        "counts": {"scored": 2}, "git_sha": "abc1234",
    }])
    wh.insert_rows("predictions", [
        {"run_id": RUN_ID, "predicted_at": _NOW, "name": "vllm", "category": "llm",
         "momentum_score": 88.0, "risk_score": 21.0, "growth_pred_70d": 0.12,
         "momentum_label": "high", "risk_level": "low",
         "top_reasons": ["downloads accelerating", "active maintenance"]},
        {"run_id": RUN_ID, "predicted_at": _NOW, "name": "langchain", "category": "framework",
         "momentum_score": 41.0, "risk_score": 67.0, "growth_pred_70d": -0.03,
         "momentum_label": "low", "risk_level": "high",
         "top_reasons": ["recent CVE", "issue backlog growing"]},
    ])
    wh.insert_rows("snapshots", [
        {"run_id": RUN_ID, "snapshot_date": _TODAY, "name": "vllm", "category": "llm",
         "repo": "vllm-project/vllm", "stars": 30000, "forks": 4000,
         "monthly_downloads": 5_000_000, "downloads_7d": 1_200_000,
         "dependent_repos_count": 1200, "vuln_count": 0, "scorecard_overall": 7.5,
         "days_since_last_release": 5.0, "bus_factor": 8.0, "archived": False},
        {"run_id": RUN_ID, "snapshot_date": _TODAY, "name": "langchain", "category": "framework",
         "repo": "langchain-ai/langchain", "stars": 90000, "forks": 14000,
         "monthly_downloads": 20_000_000, "downloads_7d": 4_800_000,
         "dependent_repos_count": 8000, "vuln_count": 2, "scorecard_overall": 5.0,
         "days_since_last_release": 1.0, "bus_factor": 3.0, "archived": False},
    ])
    wh.insert_rows("model_runs", [
        {"run_id": RUN_ID, "model_name": "growth", "trained_at": _NOW, "version": "v1",
         "metric_name": "spearman", "metric_value": 0.31, "n_train": 1200, "n_test": 200,
         "is_champion": True},
        {"run_id": RUN_ID, "model_name": "risk", "trained_at": _NOW, "version": "v1",
         "metric_name": "roc_auc", "metric_value": 0.78, "n_train": 90, "n_test": 20,
         "is_champion": True},
    ])
    return wh


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from dashboard.app import main, queries

    wh = _seed(str(tmp_path / "smoke.duckdb"))
    # Point the read layer at the seeded warehouse. Sharing the one connection
    # avoids a second read-write lock on the same DuckDB file.
    monkeypatch.setattr(queries, "_wh_cache", wh, raising=False)
    try:
        yield TestClient(main.app)
    finally:
        wh.close()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_overview_serves_seeded_data(client):
    r = client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["tracked"] == 2  # not the empty default of 0
    assert body["high_risk"] == 1
    # movers are sorted by momentum desc -> vllm leads
    assert body["movers"][0]["name"] == "vllm"
    # risks are sorted by risk desc -> langchain leads
    assert body["risks"][0]["name"] == "langchain"


def test_packages_lists_every_scored_package(client):
    r = client.get("/api/packages")
    assert r.status_code == 200
    body = r.json()
    assert {p["name"] for p in body} == {"vllm", "langchain"}
    # JSON reasons round-trip from a stored string back into a list
    vllm = next(p for p in body if p["name"] == "vllm")
    assert isinstance(vllm["top_reasons"], list) and vllm["top_reasons"]


def test_models_history(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    assert {m["model_name"] for m in body} == {"growth", "risk"}
    assert all("metric_value" in m for m in body)


def test_empty_warehouse_degrades_gracefully(tmp_path, monkeypatch):
    """A warehouse with no tables must not 500 — the app degrades to documented
    empty defaults (status 200, tracked 0). This locks the degrade behavior and
    shows why the seeded assertions above are the real regression guard."""
    from dashboard.app import main, queries

    empty = DuckDBWarehouse(path=str(tmp_path / "empty.duckdb"))  # no init_schema
    monkeypatch.setattr(queries, "_wh_cache", empty, raising=False)
    try:
        c = TestClient(main.app)
        assert c.get("/healthz").status_code == 200
        overview = c.get("/api/overview")
        assert overview.status_code == 200
        assert overview.json()["tracked"] == 0
        assert c.get("/api/packages").json() == []
    finally:
        empty.close()

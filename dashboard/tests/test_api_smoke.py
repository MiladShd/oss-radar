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
import warnings
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from oss_radar.warehouse.duckdb_backend import DuckDBWarehouse

RUN_ID = "smoke-run"
_NOW = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0)
_TODAY = _NOW.date()


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
         "momentum_reasons": ["downloads accelerating"],
         "risk_reasons": ["active maintenance"],
         "top_reasons": ["downloads accelerating", "active maintenance"]},
        {"run_id": RUN_ID, "predicted_at": _NOW, "name": "langchain", "category": "framework",
         "momentum_score": 41.0, "risk_score": 67.0, "growth_pred_70d": -0.03,
         "momentum_label": "low", "risk_level": "high",
         "top_reasons": [
             "monthly downloads falling", "weekly downloads slowing",
             "recent vulnerabilities", "issue backlog pressure",
         ]},
    ])
    wh.insert_rows("snapshots", [
        {"run_id": RUN_ID, "snapshot_date": _TODAY, "name": "vllm", "category": "llm",
         "primary_category": "llm", "capabilities": ["inference_serving_runtime"],
         "github_topics": ["inference"], "primary_language": "Python",
         "repo": "vllm-project/vllm", "stars": 30000, "forks": 4000,
         "monthly_downloads": 5_000_000, "downloads_7d": 1_200_000,
         "dependent_repos_count": 1200, "vuln_count": 0, "scorecard_overall": 7.5,
         "days_since_last_release": 5.0, "bus_factor": 8.0, "archived": False,
         "source_status": {"github": True, "osv": True, "pypi_downloads": True}},
        {"run_id": RUN_ID, "snapshot_date": _TODAY, "name": "langchain", "category": "framework",
         "repo": "langchain-ai/langchain", "stars": 90000, "forks": 14000,
         "monthly_downloads": 20_000_000, "downloads_7d": 4_800_000,
         "dependent_repos_count": 8000, "vuln_count": 2, "scorecard_overall": 5.0,
         "days_since_last_release": 1.0, "bus_factor": 3.0, "archived": False,
         "source_status": {"github": False, "osv": True, "pypi_downloads": True}},
        {"run_id": RUN_ID, "snapshot_date": _TODAY, "name": "my-package", "category": "test",
         "downloads_7d": 11},
        {"run_id": RUN_ID, "snapshot_date": _TODAY, "name": "my.package", "category": "test",
         "downloads_7d": 22},
    ])
    wh.insert_rows("download_history", [
        {"name": "vllm", "date": _TODAY, "downloads": 1_200_000},
        {"name": "my-package", "date": _TODAY, "downloads": 11},
        {"name": "my.package", "date": _TODAY, "downloads": 22},
    ])
    wh.insert_rows("model_runs", [
        {"run_id": RUN_ID, "model_name": "growth", "trained_at": _NOW, "version": "v1",
         "metric_name": "spearman", "metric_value": 0.31, "n_train": 1200, "n_test": 200,
         "params": {"model": "LightGBMRegressor"}, "is_champion": True,
         "gcs_uri": "gs://example/models/growth/v1.pkl",
         "notes": "promoted on matched benchmark", "served_version": "v1",
         "eval_provenance": {
             "benchmark_hash": "benchmark-123",
             "benchmark_kind": "stable-package-disjoint-holdout",
         },
         "comparison_version": "v0", "comparison_metric_value": 0.29,
         "comparison_mode": "incumbent-rescored-current-benchmark"},
        {"run_id": RUN_ID, "model_name": "risk", "trained_at": _NOW, "version": "v1",
         "metric_name": "roc_auc", "metric_value": 0.78, "n_train": 90, "n_test": 20,
         "is_champion": True},
    ])
    wh.insert_rows("agent_activity", [
        {"run_id": RUN_ID, "ts": _NOW, "agent": "DataEngineer",
         "action": "check_ingestion_freshness", "status": "ok",
         "summary": "All sources green.", "artifact_url": ""},
        {"run_id": RUN_ID, "ts": _NOW, "agent": "MLOps",
         "action": "publish_report", "status": "ok",
         "summary": "Report written.", "artifact_url": ""},
    ])
    return wh


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from dashboard.app import main, queries

    wh = _seed(str(tmp_path / "smoke.duckdb"))
    # Point the read layer at the seeded warehouse. Sharing the one connection
    # avoids a second read-write lock on the same DuckDB file.
    monkeypatch.setattr(queries, "_wh_cache", wh, raising=False)
    main._response_cache.clear()
    main._audit_limiter.clear()
    try:
        yield TestClient(main.app)
    finally:
        main._response_cache.clear()
        main._audit_limiter.clear()
        wh.close()


def test_health(client, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "0123456789abcdef")
    monkeypatch.setenv("K_REVISION", "oss-radar-dashboard-00004-test")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "git_sha": "0123456789abcdef",
        "revision": "oss-radar-dashboard-00004-test",
    }


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
    assert body["warehouse"].endswith("smoke.duckdb")
    source_health = {row["source"]: row for row in body["source_health"]}
    assert source_health["github"]["success_rate"] == 0.5
    assert source_health["github"]["status"] == "degraded"
    assert "export OSS_RADAR_GITHUB_TOKEN" in source_health["github"]["guidance"]
    assert "Secret Manager" not in source_health["github"]["guidance"]
    assert source_health["osv"]["status"] == "healthy"


def test_cloud_source_health_uses_secret_manager_recovery_guidance():
    from dashboard.app import queries

    class CloudWarehouse:
        project = "oss-radar-test"
        dataset = "oss_radar"

        def query_df(self, _sql):
            return pd.DataFrame([
                {"source_status": {"github": False, "osv": True}},
            ])

    source_health = {
        row["source"]: row
        for row in queries._source_health(CloudWarehouse())
    }

    guidance = source_health["github"]["guidance"]
    assert source_health["github"]["status"] == "down"
    assert "oss-radar-github-token" in guidance
    assert "Secret Manager" in guidance
    assert "docs/OPERATIONS.md" in guidance
    assert "export OSS_RADAR_GITHUB_TOKEN" not in guidance


def test_packages_lists_every_scored_package(client):
    r = client.get("/api/packages")
    assert r.status_code == 200
    body = r.json()
    assert {p["name"] for p in body} == {"vllm", "langchain"}
    # JSON reasons round-trip from a stored string back into a list
    vllm = next(p for p in body if p["name"] == "vllm")
    assert isinstance(vllm["top_reasons"], list) and vllm["top_reasons"]
    assert vllm["momentum_reasons"] == ["downloads accelerating"]
    assert vllm["risk_reasons"] == ["active maintenance"]
    assert vllm["primary_category"] == "llm"
    assert vllm["capabilities"] == ["inference_serving_runtime"]
    assert vllm["github_topics"] == ["inference"]
    legacy = next(p for p in body if p["name"] == "langchain")
    assert legacy["momentum_reasons"] == [
        "monthly downloads falling",
        "weekly downloads slowing",
    ]
    assert legacy["risk_reasons"] == [
        "recent vulnerabilities",
        "issue backlog pressure",
    ]


def test_models_history(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    assert {m["model_name"] for m in body} == {"growth", "risk"}
    assert all("metric_value" in m for m in body)
    growth = next(m for m in body if m["model_name"] == "growth")
    assert growth["served_version"] == "v1"
    assert growth["n_test"] == 200
    assert growth["params"]["model"] == "LightGBMRegressor"
    assert growth["gcs_uri"].endswith("/v1.pkl")
    assert growth["notes"] == "promoted on matched benchmark"
    assert growth["eval_provenance"]["benchmark_hash"] == "benchmark-123"
    assert growth["comparison_version"] == "v0"
    assert growth["comparison_metric_value"] == 0.29
    assert growth["comparison_mode"] == "incumbent-rescored-current-benchmark"


def test_models_history_falls_back_to_legacy_schema(monkeypatch):
    from dashboard.app import queries

    class LegacyWarehouse:
        def __init__(self):
            self.queries = []

        def query_df(self, sql):
            self.queries.append(sql)
            if "served_version" in sql:
                raise RuntimeError("column does not exist")
            return pd.DataFrame([{
                "run_id": RUN_ID,
                "model_name": "growth",
                "trained_at": _NOW,
                "version": "legacy-v1",
                "metric_name": "spearman",
                "metric_value": 0.25,
                "n_train": 100,
                "n_test": 20,
                "params": '{"model":"legacy"}',
                "is_champion": True,
                "gcs_uri": "",
                "notes": "legacy row",
            }])

    legacy = LegacyWarehouse()
    monkeypatch.setattr(queries, "_wh_cache", legacy, raising=False)

    body = queries.model_history()

    assert len(legacy.queries) == 2
    assert body[0]["served_version"] == ""
    assert body[0]["params"] == {"model": "legacy"}
    assert body[0]["eval_provenance"] == {}
    assert body[0]["comparison_version"] == ""
    assert body[0]["comparison_metric_value"] is None
    assert body[0]["comparison_mode"] == ""


def test_dashboard_uses_scoped_reason_fallback_and_describes_risk_formula():
    html = (
        Path(__file__).resolve().parents[1] / "app" / "static" / "index.html"
    ).read_text()

    assert "function scopedReasons" in html
    assert "if(scoped.length)" in html
    assert 'const source=scopedReasons(p,scoreKey==="momentum_score"?"momentum":"risk")' in html
    assert 'scopedReasons(p,"momentum")' in html
    assert 'scopedReasons(p,"risk")' in html
    assert "calibrated classifier" in html
    assert "categorical safety floors" in html
    assert 'id="sourceHealth"' in html
    assert "o.source_health" in html
    assert "s.guidance" in html


@pytest.mark.parametrize(
    ("name", "downloads"),
    [("vllm", 1_200_000), ("my-package", 11), ("my.package", 22)],
)
def test_package_detail_uses_parameterized_names(client, name, downloads):
    body = client.get(f"/api/package/{name}").json()
    assert body["downloads"][0]["downloads"] == downloads
    assert body["snapshots"]


def test_package_detail_rejects_hostile_input(client):
    body = client.get("/api/package/vllm%27%20OR%201%3D1--").json()
    assert body == {"prediction": None, "downloads": [], "snapshots": []}


def test_expensive_read_routes_are_cached(client, monkeypatch):
    from dashboard.app import main, queries

    calls = 0

    def fake_overview():
        nonlocal calls
        calls += 1
        return {"data_state": "ready", "tracked": 123, "movers": [], "risks": []}

    main._response_cache.clear()
    monkeypatch.setattr(queries, "overview", fake_overview)
    assert client.get("/api/overview").json()["tracked"] == 123
    assert client.get("/api/overview").json()["tracked"] == 123
    assert calls == 1


def test_system_health_reports_green_run_and_logs(client):
    r = client.get("/api/system-health")
    assert r.status_code == 200
    body = r.json()
    assert body["data_state"] == "ready"
    assert body["status"] == "green"
    assert body["run_days"] == 1
    assert body["error_count"] == 0
    assert body["warning_count"] == 0
    assert any("Run smoke-run status: success" in log["message"] for log in body["logs"])
    assert any(log["source"] == "DataEngineer" for log in body["logs"])


def test_system_health_detects_a_stale_success(client):
    from dashboard.app import main, queries

    wh = queries._wh_cache
    wh._con.execute(  # noqa: SLF001 - test fixture intentionally mutates its private connection
        "UPDATE pipeline_runs SET finished_at = ? WHERE run_id = ?",
        [_NOW - dt.timedelta(hours=48), RUN_ID],
    )
    main._response_cache.clear()
    body = client.get("/api/system-health").json()

    assert body["status"] == "red"
    assert any(issue["status"] == "stale" for issue in body["issues"])


def test_system_health_detects_a_stuck_running_execution(client):
    from dashboard.app import main, queries

    wh = queries._wh_cache
    wh._con.execute(  # noqa: SLF001 - test fixture intentionally mutates its private connection
        "UPDATE pipeline_runs SET status = 'running', started_at = ?, finished_at = NULL "
        "WHERE run_id = ?",
        [_NOW - dt.timedelta(hours=2), RUN_ID],
    )
    main._response_cache.clear()
    body = client.get("/api/system-health").json()

    assert body["status"] == "red"
    assert any(issue["status"] == "running" for issue in body["issues"])


def test_public_audit_endpoint_is_rate_limited(client):
    responses = [client.post("/api/audit", json={"packages": []}) for _ in range(11)]

    assert all(response.status_code == 200 for response in responses[:10])
    assert responses[-1].status_code == 429
    assert responses[-1].headers["retry-after"] == "60"


def test_public_audit_enforces_actual_streamed_body_size(client):
    response = client.post(
        "/api/audit",
        content=b'{"requirements":"' + (b"x" * 25_001) + b'"}',
        headers={"content-length": "0", "content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"] == "request body too large"


def test_legacy_growth_prediction_fallback_is_future_warning_free():
    from dashboard.app.queries import _normalize_growth_prediction

    frame = pd.DataFrame({
        "growth_pred_70d": [None, 0.3],
        "growth_pred_7d": [0.1, 0.2],
    })
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        normalized = _normalize_growth_prediction(frame)

    assert normalized["growth_pred_70d"].tolist() == [0.1, 0.3]


def test_empty_warehouse_degrades_gracefully(tmp_path, monkeypatch):
    """A warehouse with no tables must not 500 — the app degrades to documented
    empty defaults (status 200, tracked 0). This locks the degrade behavior and
    shows why the seeded assertions above are the real regression guard."""
    from dashboard.app import main, queries

    empty = DuckDBWarehouse(path=str(tmp_path / "empty.duckdb"))  # no init_schema
    monkeypatch.setattr(queries, "_wh_cache", empty, raising=False)
    main._response_cache.clear()
    try:
        c = TestClient(main.app)
        assert c.get("/health").status_code == 200
        overview = c.get("/api/overview")
        assert overview.status_code == 200
        overview_body = overview.json()
        assert overview_body["tracked"] == 0
        assert overview_body["data_state"] == "error"
        assert overview_body["warehouse"] == empty.path
        assert overview_body["setup_command"] == "make demo"
        assert c.get("/api/packages").json() == []
        system = c.get("/api/system-health").json()
        assert system["status"] == "unknown"
        assert system["data_state"] == "error"
        assert system["warehouse"] == empty.path
    finally:
        main._response_cache.clear()
        empty.close()


def test_initialized_warehouse_has_explicit_first_run_state(tmp_path, monkeypatch):
    from dashboard.app import main, queries

    empty = DuckDBWarehouse(path=str(tmp_path / "first-run.duckdb"))
    empty.init_schema()
    monkeypatch.setattr(queries, "_wh_cache", empty, raising=False)
    main._response_cache.clear()
    try:
        body = TestClient(main.app).get("/api/overview").json()

        assert body["data_state"] == "empty"
        assert body["tracked"] == 0
        assert body["warehouse"] == empty.path
        assert body["setup_command"] == "make demo"
        assert body["source_health"] == []
    finally:
        main._response_cache.clear()
        empty.close()

"""DuckDB warehouse: schema creation, type coercion, JSON, NaN, round-trip."""

from datetime import date, datetime

from oss_radar.warehouse.duckdb_backend import DuckDBWarehouse


def test_schema_and_roundtrip(tmp_path):
    wh = DuckDBWarehouse(path=str(tmp_path / "t.duckdb"))
    wh.init_schema()
    assert set(["snapshots", "predictions", "model_runs", "agent_activity"]).issubset(wh.table_names())

    rows = [{
        "run_id": "r1", "snapshot_date": date(2026, 6, 19), "name": "vllm", "category": "llm",
        "stars": 100, "download_velocity": float("nan"),  # NaN -> NULL
        "source_status": {"pypi": True, "osv": False},     # dict -> JSON string
        "ingested_at": datetime(2026, 6, 19, 12, 0, 0),
    }]
    assert wh.insert_rows("snapshots", rows) == 1
    df = wh.query_df("SELECT name, stars, download_velocity, source_status FROM snapshots")
    assert df.iloc[0]["name"] == "vllm"
    assert df.iloc[0]["stars"] == 100
    assert df.iloc[0]["download_velocity"] is None or df.iloc[0]["download_velocity"] != df.iloc[0]["download_velocity"]
    assert '"osv": false' in df.iloc[0]["source_status"]


def test_truncate_and_count(tmp_path):
    wh = DuckDBWarehouse(path=str(tmp_path / "t.duckdb"))
    wh.init_schema()
    wh.insert_rows("download_history", [{"name": "a", "date": date(2026, 1, 1), "downloads": 5}])
    assert wh.count("download_history") == 1
    wh.truncate("download_history")
    assert wh.count("download_history") == 0


def test_string_date_coercion(tmp_path):
    wh = DuckDBWarehouse(path=str(tmp_path / "t.duckdb"))
    wh.init_schema()
    # ISO strings must coerce into DATE/TIMESTAMP columns
    wh.insert_rows("snapshots", [{"run_id": "r", "snapshot_date": "2026-06-19", "name": "x"}])
    df = wh.query_df("SELECT snapshot_date FROM snapshots")
    assert str(df.iloc[0]["snapshot_date"]).startswith("2026-06-19")

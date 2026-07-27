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


def test_upsert_replaces_matching_key_and_keeps_history(tmp_path):
    wh = DuckDBWarehouse(path=str(tmp_path / "t.duckdb"))
    wh.init_schema()
    wh.upsert_rows("download_history", [
        {"name": "a", "date": date(2026, 1, 1), "downloads": 5},
        {"name": "a", "date": date(2026, 1, 2), "downloads": 7},
    ], ["name", "date"])
    # A revised API value replaces that package-day; a new day appends.
    assert wh.upsert_rows("download_history", [
        {"name": "a", "date": date(2026, 1, 2), "downloads": 9},
        {"name": "a", "date": date(2026, 1, 3), "downloads": 11},
        {"name": "a", "date": date(2026, 1, 3), "downloads": 12},
    ], ["name", "date"]) == 2
    df = wh.query_df("SELECT date, downloads FROM download_history ORDER BY date")
    assert list(df["downloads"]) == [5, 9, 12]


def test_upsert_rejects_null_keys(tmp_path):
    wh = DuckDBWarehouse(path=str(tmp_path / "t.duckdb"))
    wh.init_schema()
    try:
        wh.upsert_rows("download_history", [{"name": "a", "downloads": 1}], ["name", "date"])
    except ValueError as exc:
        assert "cannot contain NULL" in str(exc)
    else:
        raise AssertionError("NULL natural keys must be rejected")


def test_string_date_coercion(tmp_path):
    wh = DuckDBWarehouse(path=str(tmp_path / "t.duckdb"))
    wh.init_schema()
    # ISO strings must coerce into DATE/TIMESTAMP columns
    wh.insert_rows("snapshots", [{"run_id": "r", "snapshot_date": "2026-06-19", "name": "x"}])
    df = wh.query_df("SELECT snapshot_date FROM snapshots")
    assert str(df.iloc[0]["snapshot_date"]).startswith("2026-06-19")

"""Hermetic fixture-mode contracts for the end-to-end smoke path."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from datetime import date
from pathlib import Path

import pytest
import requests

from oss_radar import cli
from oss_radar import smoke as smoke_module
from oss_radar.config import Settings
from oss_radar.ingest.fixture import collect_fixture
from oss_radar.orchestrator import pipeline
from oss_radar.smoke import NetworkAccessDisabled, network_disabled, run_offline_smoke
from oss_radar.warehouse.duckdb_backend import DuckDBWarehouse

EXPECTED_PACKAGES = {
    "fixture-accelerating",
    "fixture-declining",
    "fixture-steady",
}
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_fixture_expands_to_three_snapshots_and_fixed_dense_history():
    result = collect_fixture("fixture-test")

    snapshots = result["snapshots"]
    history = result["history"]
    assert {row["name"] for row in snapshots} == EXPECTED_PACKAGES
    assert len(history) == 3 * 365
    assert {row["date"] for row in history} >= {
        date(2025, 1, 1),
        date(2025, 12, 31),
    }
    assert all(row["source_status"] == {"fixture": True} for row in snapshots)

    for snapshot in snapshots:
        series = [
            row["downloads"]
            for row in history
            if row["name"] == snapshot["name"]
        ]
        assert len(series) == 365
        assert snapshot["downloads_1d"] == series[-1]
        assert snapshot["downloads_7d"] == sum(series[-7:])
        assert snapshot["downloads_28d"] == sum(series[-28:])


def test_network_guard_rejects_dns_sockets_and_child_processes():
    left, right = socket.socketpair()
    attempts: list[str] = []
    try:
        with network_disabled(attempts):
            with pytest.raises(NetworkAccessDisabled, match="network access"):
                socket.getaddrinfo("example.invalid", 443)
            with pytest.raises(NetworkAccessDisabled, match="network access"):
                socket.create_connection(("example.invalid", 443))
            with pytest.raises(NetworkAccessDisabled, match="network access"):
                left.sendall(b"already-open socket")
            with pytest.raises(NetworkAccessDisabled, match="network access"):
                subprocess.run(["true"], check=True)
    finally:
        left.close()
        right.close()
    assert attempts == [
        "socket.getaddrinfo",
        "socket.create_connection",
        "socket.socket.sendall",
        "subprocess.Popen",
    ]


def test_offline_smoke_exercises_pipeline_without_live_connectors(
    tmp_path,
    monkeypatch,
    capsys,
):
    network_attempts: list[tuple] = []

    def forbidden_network(*args, **kwargs):
        network_attempts.append((args, kwargs))
        raise AssertionError("fixture smoke attempted an HTTP request")

    def forbidden_live_stage(*_args, **_kwargs):
        raise AssertionError("fixture smoke called a live-only pipeline stage")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)
    monkeypatch.setattr(pipeline, "collect", forbidden_live_stage)
    monkeypatch.setattr(pipeline, "heal", forbidden_live_stage)
    monkeypatch.setattr(pipeline, "audit_own_dependencies", forbidden_live_stage)

    output = tmp_path / "smoke"
    assert cli.main(["smoke", "--out", str(output)]) == 0
    streams = capsys.readouterr()
    summary = json.loads(streams.out)

    assert network_attempts == []
    assert summary["mode"] == "fixture-offline"
    assert {
        key: summary["counts"][key]
        for key in (
            "packages",
            "predictions",
            "training_rows",
            "risk_training_rows",
            "download_history_rows",
            "source_mode",
        )
    } == {
        "packages": 3,
        "predictions": 3,
        "training_rows": 213,
        "risk_training_rows": 3,
        "download_history_rows": 1095,
        "source_mode": "fixture",
    }
    assert summary["counts"]["activities"] >= 6
    assert {row["name"] for row in summary["predictions"]} == EXPECTED_PACKAGES
    assert len({row["momentum_score"] for row in summary["predictions"]}) > 1
    assert {row["risk_level"] for row in summary["predictions"]} >= {"low", "high"}
    assert all(
        row["growth_model_version"].startswith("growth-")
        for row in summary["predictions"]
    )

    artifacts = summary["artifacts"]
    for artifact in ("warehouse", "predictions", "report"):
        assert (tmp_path / "smoke" / artifacts[artifact].split("/")[-1]).is_file()
    assert (tmp_path / "smoke" / "models_local").is_dir()
    assert {row["name"] for row in json.loads(
        (tmp_path / "smoke" / "predictions.json").read_text()
    )} == EXPECTED_PACKAGES
    report = (tmp_path / "smoke" / "report.md").read_text()
    assert "Momentum movers" in report
    assert "Rising dependency risk" in report
    assert all(name in report for name in EXPECTED_PACKAGES)

    warehouse = DuckDBWarehouse(artifacts["warehouse"])
    assert len(warehouse.query_df("SELECT * FROM snapshots")) == 3
    assert len(warehouse.query_df("SELECT * FROM download_history")) == 1095
    assert len(warehouse.query_df("SELECT * FROM features")) == 3
    assert len(warehouse.query_df("SELECT * FROM predictions")) == 3
    assert not warehouse.query_df(
        "SELECT * FROM model_runs WHERE model_name = 'growth'"
    ).empty
    assert len(warehouse.query_df("SELECT * FROM agent_activity")) >= 6
    run = warehouse.query_df("SELECT status, counts FROM pipeline_runs").iloc[0]
    assert run["status"] == "success"
    assert json.loads(run["counts"])["source_mode"] == "fixture"
    audit = warehouse.query_df("SELECT payload FROM self_audit").iloc[0]["payload"]
    assert json.loads(audit)["skipped"] is True


def test_smoke_fails_if_fixture_code_swallows_a_blocked_attempt(tmp_path, monkeypatch):
    def swallow_blocked_attempt(*_args, **_kwargs):
        try:
            socket.getaddrinfo("example.invalid", 443)
        except NetworkAccessDisabled:
            pass
        return {}

    monkeypatch.setattr(smoke_module, "run_pipeline", swallow_blocked_attempt)

    with pytest.raises(NetworkAccessDisabled, match="attempted blocked.*swallowed"):
        run_offline_smoke(tmp_path / "smoke")


def test_smoke_refuses_to_replace_an_unowned_nonempty_directory(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "user-data.txt").write_text("keep me")

    with pytest.raises(ValueError, match="not created by `oss-radar smoke`"):
        run_offline_smoke(output)

    assert (output / "user-data.txt").read_text() == "keep me"


@pytest.mark.parametrize(
    ("settings", "dry_run"),
    [
        (Settings(backend="duckdb", duckdb_path="/tmp/fixture-guard.duckdb"), False),
        (Settings(backend="bigquery", gcp_project="fixture-guard"), True),
    ],
)
def test_fixture_source_mode_cannot_write_external_systems(settings, dry_run):
    with pytest.raises(ValueError, match="requires dry_run=True and backend='duckdb'"):
        pipeline.run_pipeline(settings, dry_run=dry_run, source_mode="fixture")


def test_live_demo_refuses_an_external_warehouse_backend():
    result = subprocess.run(
        ["bash", "scripts/demo_local.sh"],
        cwd=REPO_ROOT,
        env={**os.environ, "OSS_RADAR_BACKEND": "bigquery"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "demo is local-only and requires duckdb" in result.stderr

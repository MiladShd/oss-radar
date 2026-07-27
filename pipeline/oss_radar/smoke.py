"""Hermetic end-to-end smoke runner.

Unlike ``oss-radar run``, this path never consults public APIs or configured
cloud credentials. It uses the bundled connector fixture and a socket-level
guard so a future accidental network call fails the smoke run immediately.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import socket
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from oss_radar.config import Settings
from oss_radar.orchestrator import run_pipeline
from oss_radar.registry import ModelRegistry
from oss_radar.warehouse import get_warehouse

_OUTPUT_MARKER = ".oss-radar-smoke-output"


class NetworkAccessDisabled(RuntimeError):
    """Raised when fixture mode attempts Python network or child-process access."""


@contextmanager
def network_disabled(attempts: list[str] | None = None):
    """Fail closed on Python DNS/socket access and child-process escape paths."""

    observed = attempts if attempts is not None else []

    def blocker(operation: str):
        def blocked(*_args, **_kwargs):
            observed.append(operation)
            raise NetworkAccessDisabled(
                "network access is disabled for `oss-radar smoke`; use `oss-radar run` "
                "for live public sources"
            )

        return blocked

    module_calls = (
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket, "gethostbyaddr"),
        (socket, "gethostbyname"),
        (socket, "gethostbyname_ex"),
        (subprocess, "Popen"),
        (os, "popen"),
        (os, "system"),
        (os, "posix_spawn"),
        (os, "posix_spawnp"),
        (os, "spawnl"),
        (os, "spawnle"),
        (os, "spawnlp"),
        (os, "spawnlpe"),
        (os, "spawnv"),
        (os, "spawnve"),
        (os, "spawnvp"),
        (os, "spawnvpe"),
    )
    socket_calls = (
        "connect",
        "connect_ex",
        "recv",
        "recv_into",
        "recvfrom",
        "recvfrom_into",
        "send",
        "sendall",
        "sendmsg",
        "sendto",
    )
    with ExitStack() as stack:
        for target, name in module_calls:
            if hasattr(target, name):
                stack.enter_context(
                    patch.object(target, name, blocker(f"{target.__name__}.{name}"))
                )
        for name in socket_calls:
            if hasattr(socket.socket, name):
                stack.enter_context(
                    patch.object(socket.socket, name, blocker(f"socket.socket.{name}"))
                )
        yield observed


@contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def run_offline_smoke(out_dir: str | Path) -> dict:
    """Run the full fixture pipeline and emit stable, inspectable artifacts."""
    output = Path(out_dir).expanduser().resolve()
    _prepare_output(output)
    database = output / "oss-radar-smoke.duckdb"

    settings = Settings(
        env="smoke",
        backend="duckdb",
        duckdb_path=str(database),
        gcp_project="",
        gcs_bucket="",
        github_token="",
        anthropic_api_key="",
        watchlist_limit=0,
        random_seed=42,
        growth_horizon_days=70,
        min_train_rows=200,
        gate_enabled=False,
    )
    network_attempts: list[str] = []
    with (
        patch.dict(
            os.environ,
            {"GIT_SHA": "0000000000000000000000000000000000000000"},
        ),
        # MLflow's optional Git metadata lookup spawns a child process. Registry/model artifact
        # persistence is still exercised; the best-effort local tracking trace is a live-mode
        # concern and is intentionally outside the hermetic smoke contract.
        patch.object(ModelRegistry, "_mlflow_log", return_value=None),
        _working_directory(output),
        network_disabled(network_attempts),
    ):
        result = run_pipeline(settings, dry_run=True, source_mode="fixture")
    if network_attempts:
        attempted = ", ".join(sorted(set(network_attempts)))
        raise NetworkAccessDisabled(
            "fixture code attempted blocked network/process operations that were swallowed: "
            f"{attempted}"
        )

    warehouse = get_warehouse(settings)
    predictions = warehouse.query_df(
        "SELECT name, category, momentum_score, risk_score, growth_pred_70d, "
        "momentum_label, risk_level, momentum_reasons, risk_reasons, "
        "growth_model_version, risk_model_version "
        "FROM predictions WHERE run_id = ? ORDER BY name",
        (result["run_id"],),
    )
    _assert_meaningful(result, predictions)

    prediction_records = [_record(row) for row in predictions.to_dict("records")]
    predictions_path = output / "predictions.json"
    predictions_path.write_text(json.dumps(prediction_records, indent=2, sort_keys=True) + "\n")

    run_date = result["run_id"]
    report_source = (
        output
        / "reports"
        / f"{run_date[:4]}-{run_date[4:6]}-{run_date[6:8]}.md"
    )
    if not report_source.is_file():
        raise RuntimeError("fixture pipeline completed without producing an agent report")
    report_path = output / "report.md"
    shutil.copyfile(report_source, report_path)
    report_text = report_path.read_text()
    if not all(record["name"] in report_text for record in prediction_records):
        raise RuntimeError("fixture report does not include every scored package")

    return {
        "mode": "fixture-offline",
        "run_id": result["run_id"],
        "counts": result["counts"],
        "artifacts": {
            "warehouse": str(database),
            "predictions": str(predictions_path),
            "report": str(report_path),
            "models": str(output / "models_local"),
        },
        "predictions": prediction_records,
    }


def _prepare_output(output: Path) -> None:
    """Create or replace only a directory previously owned by the smoke command."""
    dangerous = {Path(output.anchor).resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if output in dangerous:
        raise ValueError(f"refusing unsafe smoke output directory: {output}")
    marker = output / _OUTPUT_MARKER
    if output.exists():
        if not output.is_dir():
            raise ValueError(f"smoke output exists and is not a directory: {output}")
        if any(output.iterdir()) and not marker.is_file():
            raise ValueError(
                "refusing to replace a non-empty directory not created by `oss-radar smoke`: "
                f"{output}"
            )
        shutil.rmtree(output)
    output.mkdir(parents=True)
    (output / _OUTPUT_MARKER).write_text(
        "Owned by `oss-radar smoke`; this directory is replaced on each run.\n"
    )


def _assert_meaningful(result: dict, predictions) -> None:
    counts = result.get("counts", {})
    if counts.get("source_mode") != "fixture":
        raise RuntimeError("smoke pipeline did not use fixture source mode")
    if counts.get("packages", 0) < 3 or counts.get("predictions") != counts.get("packages"):
        raise RuntimeError(f"fixture pipeline produced incomplete predictions: {counts}")
    if counts.get("training_rows", 0) < 200:
        raise RuntimeError(f"fixture pipeline did not exercise growth training: {counts}")
    if predictions.empty or predictions["name"].nunique() != counts["packages"]:
        raise RuntimeError("fixture prediction table is empty or contains duplicate packages")
    for column in ("momentum_score", "risk_score", "growth_pred_70d"):
        values = [float(value) for value in predictions[column]]
        if not values or not all(math.isfinite(value) for value in values):
            raise RuntimeError(f"fixture predictions contain non-finite {column}")
    if predictions["momentum_score"].nunique() < 2:
        raise RuntimeError("fixture momentum predictions are not meaningfully differentiated")
    if predictions["risk_level"].nunique() < 2:
        raise RuntimeError("fixture risk predictions are not meaningfully differentiated")
    if not predictions["growth_model_version"].fillna("").str.startswith("growth-").all():
        raise RuntimeError("fixture scoring did not use a trained growth model")


def _record(row: dict) -> dict:
    return {
        "name": str(row["name"]),
        "category": str(row["category"]),
        "momentum_score": round(float(row["momentum_score"]), 4),
        "risk_score": round(float(row["risk_score"]), 4),
        "growth_pred_70d": round(float(row["growth_pred_70d"]), 6),
        "momentum_label": str(row["momentum_label"]),
        "risk_level": str(row["risk_level"]),
        "momentum_reasons": _decode_list(row.get("momentum_reasons")),
        "risk_reasons": _decode_list(row.get("risk_reasons")),
        "growth_model_version": str(row["growth_model_version"]),
        "risk_model_version": str(row["risk_model_version"]),
    }


def _decode_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except json.JSONDecodeError:
            return []
    return []

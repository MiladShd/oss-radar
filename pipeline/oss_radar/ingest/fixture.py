"""Deterministic connector replacement used by the hermetic smoke command.

The checked-in manifest is deliberately compact: it describes three synthetic
package snapshots and fixed download curves. Expanding those curves here gives
the real feature/training path enough dated history without checking in more
than a thousand repetitive rows.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

_DEFAULT_FIXTURE = Path(__file__).with_name("fixtures") / "smoke.json"
_SCHEMA_VERSION = 1
_MIN_HISTORY_DAYS = 155


def collect_fixture(run_id: str, path: str | Path | None = None) -> dict:
    """Return fixture snapshots/history in the same contract as live ``collect``."""
    fixture_path = Path(path) if path is not None else _DEFAULT_FIXTURE
    payload = json.loads(fixture_path.read_text())
    _validate(payload)

    start = date.fromisoformat(payload["start_date"])
    n_days = int(payload["days"])
    end = start + timedelta(days=n_days - 1)
    ingested_at = datetime.combine(end, time(hour=12), tzinfo=UTC)
    weekly_pattern = [float(value) for value in payload["weekly_pattern"]]

    snapshots: list[dict] = []
    history: list[dict] = []
    for package in payload["packages"]:
        values = _download_curve(package["history"], n_days, weekly_pattern)
        name = str(package["name"])
        package_history = [
            {
                "name": name,
                "date": start + timedelta(days=offset),
                "downloads": downloads,
            }
            for offset, downloads in enumerate(values)
        ]
        history.extend(package_history)

        snapshot = {
            "run_id": run_id,
            "snapshot_date": end,
            "name": name,
            "category": package["category"],
            "primary_category": package["category"],
            "capabilities": list(package.get("capabilities", [])),
            "repo": package["repo"],
            "downloads_1d": values[-1],
            "downloads_7d": sum(values[-7:]),
            "downloads_28d": sum(values[-28:]),
            "download_velocity": sum(values[-7:]) / 7.0,
            "download_acceleration": _acceleration(values),
            "monthly_downloads": sum(values[-28:]),
            **package["snapshot"],
            "pushed_at": ingested_at,
            "created_at": datetime.combine(start, time(), tzinfo=UTC),
            "source_status": {"fixture": True},
            "ingested_at": ingested_at,
        }
        snapshots.append(snapshot)

    return {"snapshots": snapshots, "history": history}


def _download_curve(spec: dict, n_days: int, weekly_pattern: list[float]) -> list[int]:
    base = float(spec["base"])
    linear = float(spec["linear_growth"])
    quadratic = float(spec["quadratic_growth"])
    denominator = max(n_days - 1, 1)
    values = []
    for offset in range(n_days):
        progress = offset / denominator
        trend = 1.0 + linear * progress + quadratic * progress**2
        seasonal = weekly_pattern[offset % len(weekly_pattern)]
        # A small deterministic long wave avoids a trivially straight synthetic series.
        long_wave = 1.0 + 0.035 * math.sin(offset * 2.0 * math.pi / 31.0)
        values.append(max(1, int(round(base * trend * seasonal * long_wave))))
    return values


def _acceleration(values: list[int]) -> float:
    latest = sum(values[-7:])
    prior = sum(values[-14:-7])
    return (latest / prior - 1.0) if prior else 0.0


def _validate(payload: dict) -> None:
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(
            f"unsupported smoke fixture schema: {payload.get('schema_version')!r}"
        )
    packages = payload.get("packages")
    if not isinstance(packages, list) or len(packages) < 3:
        raise ValueError("smoke fixture requires at least three packages")
    names = [package.get("name") for package in packages]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("smoke fixture package names must be non-empty and unique")
    if int(payload.get("days", 0)) < _MIN_HISTORY_DAYS:
        raise ValueError(
            f"smoke fixture needs at least {_MIN_HISTORY_DAYS} download-history days"
        )
    pattern = payload.get("weekly_pattern")
    if not isinstance(pattern, list) or len(pattern) != 7:
        raise ValueError("smoke fixture weekly_pattern must contain seven values")
    for package in packages:
        if not isinstance(package.get("history"), dict):
            raise ValueError(f"missing history specification for {package.get('name')}")
        if not isinstance(package.get("snapshot"), dict):
            raise ValueError(f"missing snapshot for {package.get('name')}")

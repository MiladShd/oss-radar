"""Self-healing for the ingest stage.

Transient failures happen (a source rate-limits, a request times out). Rather than shipping a
hole, the Healer:
  1. retries the failed packages once, gently (single-threaded), and
  2. for anything still missing, carries forward that package's last good snapshot so the
     dashboard and risk features don't regress.
Both actions are bounded and logged by the Healer agent.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import structlog

from oss_radar.config import Settings
from oss_radar.config.packages import get_watchlist
from oss_radar.ingest import github
from oss_radar.ingest.collector import collect_one
from oss_radar.ingest.http import HttpClient

log = structlog.get_logger(__name__)
_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")


def identify_failures(snapshots: list[dict]) -> list[str]:
    """Packages whose core download signal failed to ingest."""
    return [s["name"] for s in snapshots if s.get("downloads_7d") is None]


def _carry_forward(wh, run_id: str, names: list[str], snapshots: list[dict],
                   idx: dict[str, int]) -> int:
    healed = 0
    now = datetime.now(UTC)
    for name in names:
        safe = _SAFE.sub("", name)[:80]
        try:
            prev = wh.query_df(
                f"SELECT * FROM snapshots WHERE name = '{safe}' AND downloads_7d IS NOT NULL "
                f"ORDER BY snapshot_date DESC LIMIT 1"
            )
        except Exception:  # noqa: BLE001
            continue
        if prev.empty:
            continue
        row = prev.iloc[0].to_dict()
        row["run_id"] = run_id
        row["snapshot_date"] = now.date()
        row["ingested_at"] = now
        snapshots[idx[name]] = row
        healed += 1
    return healed


def heal(result: dict, settings: Settings, wh, run_id: str) -> dict:
    snapshots, history = result["snapshots"], result["history"]
    failed = identify_failures(snapshots)
    stats = {"failed": len(failed), "recovered": 0, "carried_forward": 0}
    if not failed:
        return {"snapshots": snapshots, "history": history, "stats": stats}

    log.info("healing.retry", count=len(failed))
    http = HttpClient(timeout=settings.http_timeout)
    gh = github.make_client(token=settings.github_token, timeout=settings.http_timeout)
    watch = {p["name"]: p for p in get_watchlist(settings.watchlist_limit)}
    idx = {s["name"]: i for i, s in enumerate(snapshots)}

    for name in failed:
        pkg = watch.get(name)
        if not pkg:
            continue
        try:
            res = collect_one(pkg, http, gh, run_id)
        except Exception:  # noqa: BLE001
            continue
        if res["snapshot"].get("downloads_7d") is not None:
            snapshots[idx[name]] = res["snapshot"]
            history.extend(res["history"])
            stats["recovered"] += 1

    still = identify_failures(snapshots)
    if still and wh is not None:
        stats["carried_forward"] = _carry_forward(wh, run_id, still, snapshots, idx)

    return {"snapshots": snapshots, "history": history, "stats": stats}

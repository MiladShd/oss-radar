"""PyPI JSON API — release cadence, version count, and source-repo discovery.

Gotchas handled: some release versions have an empty file list (skip them); the repo
URL lives under a different project_urls key per package (multi-key scan); home_page
is frequently null.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from statistics import mean

from oss_radar.ingest.http import HttpClient

BASE = "https://pypi.org/pypi"
_REPO_KEY_PRIORITY = ("source", "repository", "source code", "code", "github", "homepage", "home")
_OWNER_REPO = re.compile(r"github\.com/([^/]+)/([^/#?]+)", re.IGNORECASE)


def parse_owner_repo(url: str | None) -> tuple[str, str] | None:
    if not url:
        return None
    m = _OWNER_REPO.search(url)
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    return owner, repo.removesuffix(".git")


def _discover_repo(info: dict) -> str | None:
    candidates: dict[str, str] = dict(info.get("project_urls") or {})
    if info.get("home_page"):
        candidates["home_page"] = info["home_page"]
    # priority keys first
    for key in _REPO_KEY_PRIORITY:
        for k, v in candidates.items():
            if v and k.lower() == key and "github.com" in v:
                return v
    # any github url
    for v in candidates.values():
        if v and "github.com" in v:
            return v
    return None


def fetch(client: HttpClient, package: str) -> dict:
    out: dict = {"_ok": False}
    data = client.get_json(f"{BASE}/{package}/json")
    if not data:
        return out
    info = data.get("info", {})
    releases = data.get("releases", {})

    rel_times: list[datetime] = []
    for _ver, files in releases.items():
        if not files:
            continue
        stamps = [f.get("upload_time_iso_8601") for f in files if f.get("upload_time_iso_8601")]
        if stamps:
            rel_times.append(datetime.fromisoformat(min(stamps).replace("Z", "+00:00")))
    rel_times.sort()

    days_since = cadence = None
    if rel_times:
        now = datetime.now(UTC)
        days_since = round((now - rel_times[-1]).total_seconds() / 86400, 1)
        if len(rel_times) >= 2:
            gaps = [(b - a).total_seconds() / 86400 for a, b in zip(rel_times, rel_times[1:], strict=False)]
            cadence = round(mean(gaps), 1)

    repo_url = _discover_repo(info)
    out.update(
        {
            "_ok": True,
            "latest_version": info.get("version"),
            "version_count": len([v for v, f in releases.items() if f]),
            "days_since_last_release": days_since,
            "release_cadence_days": cadence,
            "requires_dist_count": len(info.get("requires_dist") or []),
            "latest_yanked": bool(info.get("yanked")),
            "repo_url": repo_url,
            "license_pypi": info.get("license") or None,
        }
    )
    return out

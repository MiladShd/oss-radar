"""PyPI JSON API — release cadence, version count, and source-repo discovery.

Gotchas handled: some release versions have an empty file list (skip them); the repo
URL lives under a different project_urls key per package (multi-key scan); home_page
is frequently null.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from statistics import mean
from urllib.parse import urlparse

from oss_radar.ingest.http import HttpClient

BASE = "https://pypi.org/pypi"
_REPO_KEY_PRIORITY = ("source", "repository", "source code", "code", "github", "homepage", "home")
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_GITHUB_SCHEMES = frozenset({"git", "git+http", "git+https", "git+ssh", "http", "https", "ssh"})
_SLUG_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_SCP_GITHUB = re.compile(r"^(?:(?P<user>[^@\s/]+)@)?github\.com:(?P<path>.+)$", re.IGNORECASE)


def parse_owner_repo(url: str | None) -> tuple[str, str] | None:
    """Return a GitHub ``(owner, repo)`` only for an exact GitHub hostname."""
    if not isinstance(url, str) or not url.strip():
        return None
    raw = url.strip()
    scp_match = _SCP_GITHUB.fullmatch(raw)
    if scp_match:
        user = f"{scp_match.group('user')}@" if scp_match.group("user") else ""
        raw = f"ssh://{user}github.com/{scp_match.group('path')}"
    elif raw.startswith("//"):
        raw = f"https:{raw}"
    elif "://" not in raw:
        raw = f"https://{raw}"

    try:
        parsed = urlparse(raw)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.scheme.lower() not in _GITHUB_SCHEMES:
        return None
    if hostname not in _GITHUB_HOSTS:
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[:2]
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    if not _SLUG_COMPONENT.fullmatch(owner) or not _SLUG_COMPONENT.fullmatch(repo):
        return None
    return owner, repo


def _discover_repo(info: dict) -> str | None:
    candidates: dict[str, str] = dict(info.get("project_urls") or {})
    if info.get("home_page"):
        candidates["home_page"] = info["home_page"]
    # priority keys first
    for key in _REPO_KEY_PRIORITY:
        for k, v in candidates.items():
            if isinstance(v, str) and k.lower() == key and parse_owner_repo(v):
                return v
    # any github url
    for v in candidates.values():
        if isinstance(v, str) and parse_owner_repo(v):
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

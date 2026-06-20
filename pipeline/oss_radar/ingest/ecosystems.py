"""ecosyste.ms — reverse-dependency counts (deps.dev lacks these) plus FRESH repo stats.

The embedded ``repo_metadata`` block in the package payload is stale, so current stars/
forks/pushed_at/bus-factor come from the dedicated repos.ecosyste.ms endpoint.
"""

from __future__ import annotations

from urllib.parse import quote

from oss_radar.ingest.http import HttpClient
from oss_radar.ingest.pypi_metadata import parse_owner_repo

PKG_BASE = "https://packages.ecosyste.ms/api/v1/registries/pypi.org/packages"
REPO_BASE = "https://repos.ecosyste.ms/api/v1/hosts/GitHub/repositories"


def fetch_package(client: HttpClient, package: str) -> dict:
    out: dict = {"_ok": False}
    data = client.get_json(f"{PKG_BASE}/{quote(package, safe='')}")
    if not data:
        return out
    rankings = data.get("rankings") or {}
    out.update(
        {
            "_ok": True,
            "dependent_packages_count": data.get("dependent_packages_count"),
            "dependent_repos_count": data.get("dependent_repos_count"),
            "monthly_downloads": data.get("downloads"),
            "rank_average": rankings.get("average"),
            "status": data.get("status"),
            "repo_url": data.get("repository_url"),
            "versions_count_eco": data.get("versions_count"),
        }
    )
    return out


def fetch_repo(client: HttpClient, owner: str, repo: str) -> dict:
    out: dict = {"_ok": False}
    data = client.get_json(f"{REPO_BASE}/{quote(f'{owner}/{repo}', safe='')}")
    if not data:
        return out
    commit_stats = data.get("commit_stats") or {}
    out.update(
        {
            "_ok": True,
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "open_issues": data.get("open_issues_count"),
            "pushed_at": data.get("pushed_at"),
            "created_at": data.get("created_at"),
            "bus_factor": commit_stats.get("dds"),
            "archived": data.get("archived"),
        }
    )
    return out


def resolve_owner_repo(client: HttpClient, package: str) -> tuple[str, str] | None:
    """Resolve owner/repo from the ecosyste.ms repository_url."""
    pkg = fetch_package(client, package)
    return parse_owner_repo(pkg.get("repo_url"))

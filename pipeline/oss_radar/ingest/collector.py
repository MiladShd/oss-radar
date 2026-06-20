"""Per-package ingestion orchestrator.

Fans out across the watchlist, calls every connector, and merges the results into one
``snapshot`` row per package plus the backfilled daily ``download_history``. Source
precedence is chosen from the verified data quality of each API:

* stars/forks/open_issues  -> ecosyste.ms repos (freshest), fallback GitHub, fallback deps.dev
* commit volume + PR/issue velocity -> GitHub (only source)
* bus factor + archived     -> ecosyste.ms repos
* reverse deps + rank       -> ecosyste.ms package (deps.dev lacks these)
* scorecard                 -> deps.dev
* vulnerabilities           -> OSV
* release cadence/versions  -> PyPI metadata
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import structlog

from oss_radar.config import Settings, get_settings
from oss_radar.config.packages import get_watchlist
from oss_radar.ingest import depsdev, ecosystems, github, osv, pypi_downloads, pypi_metadata
from oss_radar.ingest.http import HttpClient
from oss_radar.ingest.pypi_metadata import parse_owner_repo

log = structlog.get_logger(__name__)


def _first(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def collect_one(pkg: dict, http: HttpClient, gh: HttpClient, run_id: str) -> dict:
    name, category = pkg["name"], pkg["category"]
    now = datetime.now(UTC)

    eco_pkg = ecosystems.fetch_package(http, name)
    md = pypi_metadata.fetch(http, name)
    dl = pypi_downloads.fetch(http, name)
    osv_d = osv.fetch(http, name)

    # resolve owner/repo: override > pypi metadata > ecosyste.ms
    owner_repo = (
        parse_owner_repo(f"github.com/{pkg['repo_override']}") if pkg.get("repo_override") else None
    ) or parse_owner_repo(md.get("repo_url")) or parse_owner_repo(eco_pkg.get("repo_url"))
    owner, repo = owner_repo if owner_repo else ("", "")

    eco_repo = ecosystems.fetch_repo(http, owner, repo) if owner else {}
    deps = depsdev.fetch(http, name, owner, repo)
    gh_d = github.fetch(gh, owner, repo) if owner else {}

    source_status = {
        "pypi_downloads": dl.get("_ok", False),
        "pypi_metadata": md.get("_ok", False),
        "ecosystems_pkg": eco_pkg.get("_ok", False),
        "ecosystems_repo": eco_repo.get("_ok", False),
        "depsdev": deps.get("_ok", False),
        "osv": osv_d.get("_ok", False),
        "github": gh_d.get("_ok", False),
    }

    snapshot = {
        "run_id": run_id,
        "snapshot_date": now.date(),
        "name": name,
        "category": category,
        "repo": f"{owner}/{repo}" if owner else None,
        # downloads
        "downloads_1d": dl.get("downloads_1d"),
        "downloads_7d": dl.get("downloads_7d"),
        "downloads_28d": dl.get("downloads_28d"),
        "download_velocity": dl.get("download_velocity"),
        "download_acceleration": dl.get("download_acceleration"),
        "monthly_downloads": eco_pkg.get("monthly_downloads"),
        # repo signals
        "stars": _first(eco_repo.get("stars"), gh_d.get("stars"), deps.get("stars_depsdev")),
        "forks": _first(eco_repo.get("forks"), gh_d.get("forks"), deps.get("forks_depsdev")),
        "open_issues": _first(eco_repo.get("open_issues"), deps.get("open_issues_depsdev")),
        "subscribers": gh_d.get("subscribers"),
        "pushed_at": _first(eco_repo.get("pushed_at"), gh_d.get("pushed_at")),
        "created_at": _first(eco_repo.get("created_at"), gh_d.get("created_at")),
        "commit_count_4w": gh_d.get("commit_count_4w"),
        "prs_merged_7d": gh_d.get("prs_merged_7d"),
        "issues_opened_7d": gh_d.get("issues_opened_7d"),
        "bus_factor": eco_repo.get("bus_factor"),
        "archived": _first(eco_repo.get("archived"), gh_d.get("archived")),
        # ecosystem
        "dependent_packages_count": eco_pkg.get("dependent_packages_count"),
        "dependent_repos_count": eco_pkg.get("dependent_repos_count"),
        "rank_average": eco_pkg.get("rank_average"),
        "status": eco_pkg.get("status"),
        # versions / releases
        "latest_version": _first(md.get("latest_version"), deps.get("default_version")),
        "version_count": _first(md.get("version_count"), deps.get("version_count_depsdev")),
        "days_since_last_release": md.get("days_since_last_release"),
        "release_cadence_days": md.get("release_cadence_days"),
        "dependency_count": deps.get("dependency_count"),
        "license": _first(deps.get("license_depsdev"), md.get("license_pypi")),
        # security
        "scorecard_overall": deps.get("scorecard_overall"),
        "scorecard_maintained": deps.get("scorecard_maintained"),
        "scorecard_branch_protection": deps.get("scorecard_branch_protection"),
        "scorecard_code_review": deps.get("scorecard_code_review"),
        "vuln_count": osv_d.get("vuln_count"),
        "vuln_new_14d": osv_d.get("vuln_new_14d"),
        "vuln_new_28d": osv_d.get("vuln_new_28d"),
        "max_severity": osv_d.get("max_severity"),
        # provenance
        "source_status": source_status,
        "ingested_at": now,
    }
    return {"snapshot": snapshot, "history": dl.get("history", [])}


def collect(run_id: str, settings: Settings | None = None, max_workers: int = 4) -> dict:
    settings = settings or get_settings()
    watchlist = get_watchlist(settings.watchlist_limit)
    http = HttpClient(timeout=settings.http_timeout)
    gh = github.make_client(token=settings.github_token, timeout=settings.http_timeout)

    snapshots: list[dict] = []
    history: list[dict] = []
    ok = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_one, pkg, http, gh, run_id): pkg for pkg in watchlist}
        for fut in as_completed(futures):
            pkg = futures[fut]
            try:
                res = fut.result()
                snapshots.append(res["snapshot"])
                history.extend(res["history"])
                if res["snapshot"]["downloads_7d"] is not None:
                    ok += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("collect.package_failed", package=pkg["name"], error=str(exc))

    log.info("collect.done", packages=len(snapshots), with_downloads=ok, history_rows=len(history))
    return {"snapshots": snapshots, "history": history}

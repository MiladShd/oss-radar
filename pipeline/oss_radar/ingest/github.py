"""GitHub REST — recent commit volume and PR/issue velocity.

ecosyste.ms already gives us fresh stars/forks/open_issues, so GitHub is used mainly for
signals it alone provides: 4-week commit volume and merged-PR / opened-issue counts. These
use the SEARCH rate bucket (10/min unauth, 30/min with a token), so a token is strongly
preferred in the cloud job.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from oss_radar.ingest.http import HttpClient

BASE = "https://api.github.com"
_GITHUB_REPO_URL = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:[/?#\s]|$)",
    re.IGNORECASE,
)


def parse_topics(raw: object) -> list[str]:
    """Return GitHub topic strings unchanged, ignoring malformed values."""
    if not isinstance(raw, list):
        return []
    return [topic for topic in raw if isinstance(topic, str) and topic]


def parse_moved_repository(repo_data: object) -> str | None:
    """Extract a one-hop GitHub successor from an archived repository description."""
    if not isinstance(repo_data, dict) or repo_data.get("archived") is not True:
        return None
    description = repo_data.get("description")
    if not isinstance(description, str) or not re.search(
        r"\bmov(?:e|ed|ing)\b", description, re.IGNORECASE
    ):
        return None
    match = _GITHUB_REPO_URL.search(description)
    return match.group(1) if match else None


def make_client(token: str = "", timeout: int = 30) -> HttpClient:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return HttpClient(timeout=timeout, extra_headers=headers)


def fetch(client: HttpClient, owner: str, repo: str, want_velocity: bool = True) -> dict:
    """Fetch GitHub signals while distinguishing complete and partial success.

    ``_ok`` means every requested component returned its expected response shape.
    Successfully parsed fields remain available during partial failure, while ``_partial``
    and ``_components`` ensure callers never report the overall GitHub source as healthy.
    """
    components = {
        "repository": False,
        "commit_activity": False,
    }
    if want_velocity:
        components.update({
            "pull_request_search": False,
            "issue_search": False,
        })
    out: dict = {
        "_ok": False,
        "_partial": False,
        "_components": components,
    }

    repo_data = client.get_json(f"{BASE}/repos/{owner}/{repo}")
    if isinstance(repo_data, dict):
        moved_from = repo_data.get("full_name") or f"{owner}/{repo}"
        successor = parse_moved_repository(repo_data)
        if successor and successor.casefold() != str(moved_from).casefold():
            successor_data = client.get_json(f"{BASE}/repos/{successor}")
            if isinstance(successor_data, dict):
                out["repository_moved_from"] = str(moved_from)
                repo_data = successor_data
    canonical_owner, canonical_repo = owner, repo
    if isinstance(repo_data, dict):
        components["repository"] = True
        full_name = repo_data.get("full_name")
        if (
            isinstance(full_name, str)
            and full_name.count("/") == 1
            and all(full_name.split("/", 1))
        ):
            canonical_owner, canonical_repo = full_name.split("/", 1)
        out["canonical_repo"] = f"{canonical_owner}/{canonical_repo}"
        out.setdefault("stars", repo_data.get("stargazers_count"))
        out.setdefault("forks", repo_data.get("forks_count"))
        out["subscribers"] = repo_data.get("subscribers_count")
        out.setdefault("pushed_at", repo_data.get("pushed_at"))
        out.setdefault("created_at", repo_data.get("created_at"))
        out.setdefault("archived", repo_data.get("archived"))
        # These already arrive in the repository response above; no extra API calls.
        out["github_topics"] = parse_topics(repo_data.get("topics"))
        out["primary_language"] = repo_data.get("language")

    # The participation endpoint provides the same 52 weekly commit totals needed
    # here without the commit_activity endpoint's asynchronous 202 cache contract.
    participation = client.get_json(
        f"{BASE}/repos/{canonical_owner}/{canonical_repo}/stats/participation"
    )
    weekly_commits = (
        participation.get("all")
        if isinstance(participation, dict)
        else None
    )
    if (
        isinstance(weekly_commits, list)
        and all(isinstance(value, int) and not isinstance(value, bool) for value in weekly_commits)
    ):
        components["commit_activity"] = True
        out["commit_count_4w"] = sum(weekly_commits[-4:])

    if want_velocity:
        since = (date.today() - timedelta(days=7)).isoformat()
        canonical_query = f"repo:{canonical_owner}/{canonical_repo}"
        prs = client.get_json(
            f"{BASE}/search/issues",
            params={
                "q": f"{canonical_query} type:pr is:merged merged:>={since}",
                "per_page": 1,
            },
        )
        if isinstance(prs, dict) and "total_count" in prs:
            components["pull_request_search"] = True
            out["prs_merged_7d"] = prs["total_count"]
        issues = client.get_json(
            f"{BASE}/search/issues",
            params={
                "q": f"{canonical_query} type:issue created:>={since}",
                "per_page": 1,
            },
        )
        if isinstance(issues, dict) and "total_count" in issues:
            components["issue_search"] = True
            out["issues_opened_7d"] = issues["total_count"]
    out["_ok"] = all(components.values())
    out["_partial"] = any(components.values()) and not out["_ok"]
    return out

"""GitHub REST — recent commit volume and PR/issue velocity.

ecosyste.ms already gives us fresh stars/forks/open_issues, so GitHub is used mainly for
signals it alone provides: 4-week commit volume and merged-PR / opened-issue counts. These
use the SEARCH rate bucket (10/min unauth, 30/min with a token), so a token is strongly
preferred in the cloud job.
"""

from __future__ import annotations

from datetime import date, timedelta

from oss_radar.ingest.http import HttpClient

BASE = "https://api.github.com"


def parse_topics(raw: object) -> list[str]:
    """Return GitHub topic strings unchanged, ignoring malformed values."""
    if not isinstance(raw, list):
        return []
    return [topic for topic in raw if isinstance(topic, str) and topic]


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
        components["repository"] = True
        out.setdefault("stars", repo_data.get("stargazers_count"))
        out.setdefault("forks", repo_data.get("forks_count"))
        out["subscribers"] = repo_data.get("subscribers_count")
        out.setdefault("pushed_at", repo_data.get("pushed_at"))
        out.setdefault("created_at", repo_data.get("created_at"))
        out.setdefault("archived", repo_data.get("archived"))
        # These already arrive in the repository response above; no extra API calls.
        out["github_topics"] = parse_topics(repo_data.get("topics"))
        out["primary_language"] = repo_data.get("language")

    commit_activity = client.get_json(f"{BASE}/repos/{owner}/{repo}/stats/commit_activity")
    if isinstance(commit_activity, list):
        components["commit_activity"] = True
        out["commit_count_4w"] = sum(w.get("total", 0) for w in commit_activity[-4:])

    if want_velocity:
        since = (date.today() - timedelta(days=7)).isoformat()
        prs = client.get_json(
            f"{BASE}/search/issues",
            params={"q": f"repo:{owner}/{repo} type:pr is:merged merged:>={since}", "per_page": 1},
        )
        if isinstance(prs, dict) and "total_count" in prs:
            components["pull_request_search"] = True
            out["prs_merged_7d"] = prs["total_count"]
        issues = client.get_json(
            f"{BASE}/search/issues",
            params={"q": f"repo:{owner}/{repo} type:issue created:>={since}", "per_page": 1},
        )
        if isinstance(issues, dict) and "total_count" in issues:
            components["issue_search"] = True
            out["issues_opened_7d"] = issues["total_count"]
    out["_ok"] = all(components.values())
    out["_partial"] = any(components.values()) and not out["_ok"]
    return out

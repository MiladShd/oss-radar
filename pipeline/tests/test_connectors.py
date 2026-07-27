"""Connector contracts for public API parsing and graceful degradation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from oss_radar.agents.crew import _data_engineer
from oss_radar.ingest import collector, github, osv, pypi_downloads, pypi_metadata
from oss_radar.ingest.http import HttpClient


class _GetClient:
    def __init__(self, payload):
        self.payload = payload
        self.urls: list[str] = []

    def get_json(self, url: str):
        self.urls.append(url)
        return self.payload


def test_pypistats_uses_calendar_windows_when_daily_history_has_gaps():
    client = _GetClient({
        "data": [
            {"category": "without_mirrors", "date": "2026-07-01", "downloads": 100},
            {"category": "without_mirrors", "date": "2026-07-02", "downloads": 1},
            {"category": "without_mirrors", "date": "2026-07-03", "downloads": 1},
            {"category": "without_mirrors", "date": "2026-07-04", "downloads": 1},
            {"category": "without_mirrors", "date": "2026-07-05", "downloads": 1},
            {"category": "without_mirrors", "date": "2026-07-06", "downloads": 1},
            # July 7 is absent. A mirrored row must not fill that calendar gap.
            {"category": "with_mirrors", "date": "2026-07-07", "downloads": 999},
            {"category": "without_mirrors", "date": "2026-07-08", "downloads": 1},
        ],
    })

    result = pypi_downloads.fetch(client, "Example_Package")

    assert client.urls == [
        f"{pypi_downloads.BASE}/example_package/overall",
    ]
    assert result["_ok"] is True
    assert result["downloads_1d"] == 1
    assert result["downloads_7d"] == 6
    assert result["downloads_28d"] == 106
    assert result["download_velocity"] == pytest.approx(6 / 7, abs=0.01)
    assert result["download_acceleration"] == -94
    assert len(result["history"]) == 7


def test_pypi_metadata_discovers_repo_and_parses_release_fields():
    client = _GetClient({
        "info": {
            "version": "2.0",
            "project_urls": {
                "Documentation": "https://docs.example.test/widget",
                "Source Code": "https://github.com/acme/widget",
            },
            "home_page": "https://example.test/widget",
            "requires_dist": ["httpx>=0.27", "pydantic>=2"],
            "yanked": True,
            "license": "Apache-2.0",
        },
        "releases": {
            "1.0": [{"upload_time_iso_8601": "2025-01-01T12:00:00Z"}],
            "2.0": [
                {"upload_time_iso_8601": "2025-01-11T12:00:00Z"},
                {"upload_time_iso_8601": "2025-01-12T12:00:00Z"},
            ],
            "3.0": [],
        },
    })

    result = pypi_metadata.fetch(client, "widget")

    assert client.urls == [f"{pypi_metadata.BASE}/widget/json"]
    assert result["_ok"] is True
    assert result["repo_url"] == "https://github.com/acme/widget"
    assert result["latest_version"] == "2.0"
    assert result["version_count"] == 2
    assert result["release_cadence_days"] == 10.0
    assert result["days_since_last_release"] > 0
    assert result["requires_dist_count"] == 2
    assert result["latest_yanked"] is True
    assert result["license_pypi"] == "Apache-2.0"


class _PostClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post_json(self, url: str, body: dict):
        self.calls.append((url, body))
        return self.payload


def test_osv_counts_every_vulnerability_and_tracks_recent_severity():
    now = datetime.now(UTC)
    client = _PostClient({
        "vulns": [
            {
                "published": (now - timedelta(days=7)).isoformat(),
                "database_specific": {"severity": "HIGH"},
            },
            {
                "published": (now - timedelta(days=21)).isoformat(),
                "database_specific": {"severity": "MODERATE"},
            },
            {
                "published": (now - timedelta(days=60)).isoformat(),
                "database_specific": {"severity": "CRITICAL"},
            },
            {
                "database_specific": {"severity": "LOW"},
            },
        ],
    })

    result = osv.fetch(client, "widget", version="2.0")

    assert client.calls == [(
        osv.URL,
        {"package": {"name": "widget", "ecosystem": "PyPI"}, "version": "2.0"},
    )]
    assert result["_ok"] is True
    assert result["vuln_count"] == 4
    assert result["vuln_new_14d"] == 1
    assert result["vuln_new_28d"] == 2
    assert result["max_severity"] == "CRITICAL"
    assert result["max_severity_new_28d"] == "HIGH"


@pytest.mark.parametrize("status_code", [403, 429])
def test_github_rate_limits_degrade_without_raising(monkeypatch, status_code):
    client = HttpClient()
    calls = []
    backoffs = []

    def rate_limited_response(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=status_code, headers={})

    monkeypatch.setattr(client, "_throttle", lambda _url: None)
    monkeypatch.setattr(client._request.retry, "sleep", backoffs.append)
    monkeypatch.setattr(client._session, "request", rate_limited_response)

    result = github.fetch(client, "acme", "widget")

    assert result["_ok"] is False
    assert "commit_count_4w" not in result
    assert "prs_merged_7d" not in result
    assert "issues_opened_7d" not in result
    assert len(calls) == 4  # only the first endpoint exhausts its retry budget
    assert backoffs == [1.0, 2.0, 4.0]


class _SuccessfulResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_partial_github_rate_limit_short_circuits_remaining_components(monkeypatch):
    client = HttpClient()
    calls = []
    backoffs = []

    def response(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/repos/acme/widget"):
            return _SuccessfulResponse({
                "stargazers_count": 10,
                "forks_count": 2,
                "subscribers_count": 3,
            })
        return SimpleNamespace(status_code=429, headers={"Retry-After": "120"})

    monkeypatch.setattr(client, "_throttle", lambda _url: None)
    monkeypatch.setattr(client._request.retry, "sleep", backoffs.append)
    monkeypatch.setattr(client._session, "request", response)

    result = github.fetch(client, "acme", "widget")

    assert result["stars"] == 10
    assert result["_partial"] is True
    assert result["_ok"] is False
    assert result["_components"] == {
        "repository": True,
        "commit_activity": False,
        "pull_request_search": False,
        "issue_search": False,
    }
    assert len(calls) == 5  # repository once, then four bounded commit retries
    assert backoffs == [1.0, 2.0, 4.0]


class _PartialGitHubClient:
    def get_json(self, url: str, params: dict | None = None):
        if url.endswith("/repos/acme/widget"):
            return {
                "stargazers_count": 10,
                "forks_count": 2,
                "subscribers_count": 3,
                "topics": ["mlops"],
                "language": "Python",
            }
        if url.endswith("/stats/participation"):
            return None
        query = (params or {}).get("q", "")
        if "type:pr" in query:
            return {"total_count": 2}
        if "type:issue" in query:
            return None
        raise AssertionError(f"unexpected GitHub request: {url}")


def test_github_partial_success_retains_data_but_fails_overall_health():
    result = github.fetch(_PartialGitHubClient(), "acme", "widget")

    assert result["stars"] == 10
    assert result["prs_merged_7d"] == 2
    assert "commit_count_4w" not in result
    assert "issues_opened_7d" not in result
    assert result["_components"] == {
        "repository": True,
        "commit_activity": False,
        "pull_request_search": True,
        "issue_search": False,
    }
    assert result["_partial"] is True
    assert result["_ok"] is False


class _RedirectedGitHubClient:
    def __init__(self):
        self.calls = []

    def get_json(self, url: str, params: dict | None = None):
        self.calls.append((url, params))
        if url.endswith("/repos/old-org/widget"):
            return {
                "full_name": "new-org/widget",
                "stargazers_count": 10,
                "topics": ["mlops"],
                "language": "Python",
            }
        if url.endswith("/repos/new-org/widget/stats/participation"):
            return {"all": [0] * 48 + [1, 0, 2, 1]}
        query = (params or {}).get("q", "")
        if "repo:new-org/widget" in query:
            return {"total_count": 1}
        raise AssertionError(f"unexpected GitHub request: {url} {params}")


def test_github_redirect_uses_canonical_repo_for_all_followup_calls():
    client = _RedirectedGitHubClient()

    result = github.fetch(client, "old-org", "widget")

    assert result["_ok"] is True
    assert result["canonical_repo"] == "new-org/widget"
    assert result["commit_count_4w"] == 4
    assert result["prs_merged_7d"] == 1
    assert result["issues_opened_7d"] == 1
    followups = client.calls[1:]
    assert all("old-org/widget" not in str(call) for call in followups)
    assert all("new-org/widget" in str(call) for call in followups)


class _MovedGitHubClient:
    def __init__(self):
        self.calls = []

    def get_json(self, url: str, params: dict | None = None):
        self.calls.append((url, params))
        if url.endswith("/repos/old-org/widget"):
            return {
                "full_name": "old-org/widget",
                "archived": True,
                "description": (
                    "This library moved to "
                    "https://github.com/new-org/monorepo/tree/main/packages/widget"
                ),
            }
        if url.endswith("/repos/new-org/monorepo"):
            return {
                "full_name": "new-org/monorepo",
                "archived": False,
                "stargazers_count": 50,
                "topics": ["monorepo"],
                "language": "Python",
            }
        if url.endswith("/repos/new-org/monorepo/stats/participation"):
            return {"all": [0] * 51 + [7]}
        if "repo:new-org/monorepo" in str((params or {}).get("q")):
            return {"total_count": 2}
        raise AssertionError(f"unexpected GitHub request: {url} {params}")


def test_github_archived_move_follows_one_successor_repository():
    client = _MovedGitHubClient()

    result = github.fetch(client, "old-org", "widget")

    assert result["_ok"] is True
    assert result["repository_moved_from"] == "old-org/widget"
    assert result["canonical_repo"] == "new-org/monorepo"
    assert result["archived"] is False
    assert result["commit_count_4w"] == 7
    assert github.parse_moved_repository({
        "archived": False,
        "description": "moved to https://github.com/new-org/monorepo",
    }) is None


def test_collector_uses_github_successor_for_other_repo_connectors(monkeypatch):
    repo_calls = []
    deps_calls = []
    monkeypatch.setattr(collector.ecosystems, "fetch_package", lambda *_: {"_ok": True})
    monkeypatch.setattr(
        collector.pypi_metadata,
        "fetch",
        lambda *_: {
            "_ok": True,
            "repo_url": "https://github.com/old-org/widget",
        },
    )
    monkeypatch.setattr(
        collector.pypi_downloads,
        "fetch",
        lambda *_: {"_ok": True, "downloads_7d": 10, "history": []},
    )
    monkeypatch.setattr(collector.osv, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(
        collector.github,
        "fetch",
        lambda *_: {
            "_ok": True,
            "canonical_repo": "new-org/monorepo",
            "repository_moved_from": "old-org/widget",
            "archived": False,
        },
    )

    def fetch_repo(_client, owner, repo):
        repo_calls.append((owner, repo))
        return {"_ok": True, "archived": False}

    def fetch_deps(_client, name, owner, repo):
        deps_calls.append((name, owner, repo))
        return {"_ok": True}

    monkeypatch.setattr(collector.ecosystems, "fetch_repo", fetch_repo)
    monkeypatch.setattr(collector.depsdev, "fetch", fetch_deps)

    snapshot = collector.collect_one(
        {"name": "widget", "category": "framework"},
        object(),
        object(),
        "run-1",
    )["snapshot"]

    assert snapshot["repo"] == "new-org/monorepo"
    assert snapshot["archived"] is False
    assert repo_calls == [("new-org", "monorepo")]
    assert deps_calls == [("widget", "new-org", "monorepo")]


def test_collector_records_each_connector_status(monkeypatch):
    monkeypatch.setattr(
        collector.ecosystems,
        "fetch_package",
        lambda *_: {"_ok": False},
    )
    monkeypatch.setattr(
        collector.pypi_metadata,
        "fetch",
        lambda *_: {
            "_ok": True,
            "repo_url": "https://github.com/acme/widget",
        },
    )
    monkeypatch.setattr(
        collector.pypi_downloads,
        "fetch",
        lambda *_: {"_ok": False, "history": []},
    )
    monkeypatch.setattr(collector.osv, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(
        collector.ecosystems,
        "fetch_repo",
        lambda *_: {"_ok": False},
    )
    monkeypatch.setattr(collector.depsdev, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(collector.github, "fetch", lambda *_: {"_ok": False})

    result = collector.collect_one(
        {"name": "widget", "category": "framework"},
        object(),
        object(),
        "run-1",
    )

    assert result["snapshot"]["source_status"] == {
        "pypi_downloads": False,
        "pypi_metadata": True,
        "ecosystems_pkg": False,
        "ecosystems_repo": False,
        "depsdev": True,
        "osv": True,
        "github": False,
    }


def test_collector_omits_repository_sources_that_were_not_attempted(monkeypatch):
    monkeypatch.setattr(collector.ecosystems, "fetch_package", lambda *_: {"_ok": True})
    monkeypatch.setattr(collector.pypi_metadata, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(
        collector.pypi_downloads,
        "fetch",
        lambda *_: {"_ok": True, "downloads_7d": 10, "history": []},
    )
    monkeypatch.setattr(collector.osv, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(collector.depsdev, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(
        collector.ecosystems,
        "fetch_repo",
        lambda *_: pytest.fail("repository connector should not be called"),
    )
    monkeypatch.setattr(
        collector.github,
        "fetch",
        lambda *_: pytest.fail("GitHub connector should not be called"),
    )

    snapshot = collector.collect_one(
        {"name": "widget", "category": "framework"},
        object(),
        object(),
        "run-1",
    )["snapshot"]

    assert "ecosystems_repo" not in snapshot["source_status"]
    assert "github" not in snapshot["source_status"]
    assert snapshot["source_status"]["pypi_metadata"] is True


class _RecordingContext:
    dry_run = True
    settings = SimpleNamespace(github_token="", github_repo="MiladShd/oss-radar")

    def __init__(self):
        self.activities = []

    def record(self, agent, action, status, summary, artifact_url=""):
        self.activities.append({
            "agent": agent,
            "action": action,
            "status": status,
            "summary": summary,
            "artifact_url": artifact_url,
        })


def test_partial_github_failure_propagates_to_collector_and_source_health(monkeypatch):
    partial = github.fetch(_PartialGitHubClient(), "acme", "widget")
    monkeypatch.setattr(
        collector.ecosystems,
        "fetch_package",
        lambda *_: {"_ok": True},
    )
    monkeypatch.setattr(
        collector.pypi_metadata,
        "fetch",
        lambda *_: {
            "_ok": True,
            "repo_url": "https://github.com/acme/widget",
        },
    )
    monkeypatch.setattr(
        collector.pypi_downloads,
        "fetch",
        lambda *_: {"_ok": True, "downloads_7d": 10, "history": []},
    )
    monkeypatch.setattr(collector.osv, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(
        collector.ecosystems,
        "fetch_repo",
        lambda *_: {"_ok": True},
    )
    monkeypatch.setattr(collector.depsdev, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(collector.github, "fetch", lambda *_: partial)

    snapshot = collector.collect_one(
        {"name": "widget", "category": "framework"},
        object(),
        object(),
        "run-1",
    )["snapshot"]
    healthy = {
        **snapshot,
        "name": "healthy-widget",
        "source_status": {**snapshot["source_status"], "github": True},
    }
    context = _RecordingContext()
    source_health = _data_engineer(context, pd.DataFrame([snapshot, healthy]))

    assert snapshot["stars"] == 10
    assert snapshot["prs_merged_7d"] == 2
    assert snapshot["source_status"]["github"] is False
    assert source_health["source_ok_rates"]["github"] == 0.5
    assert source_health["degraded_sources"] == ["github"]
    assert "OSS_RADAR_GITHUB_TOKEN" in source_health["github_token_hint"]
    assert context.activities[0]["status"] == "warning"

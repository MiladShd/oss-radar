"""Watchlist taxonomy, balanced sampling, and repository metadata coverage."""

from collections import Counter, defaultdict
from datetime import date

import pytest

from oss_radar.config.packages import (
    CAPABILITIES,
    CATEGORIES,
    PACKAGE_CAPABILITIES,
    REPO_OVERRIDES,
    WATCHLIST,
    get_watchlist,
    parse_capabilities,
)
from oss_radar.ingest import collector, github
from oss_radar.warehouse.duckdb_backend import DuckDBWarehouse


def test_limited_watchlist_is_category_balanced_and_deterministic():
    sample = get_watchlist(limit=12)
    counts = Counter(item["primary_category"] for item in sample)

    assert sample == get_watchlist(limit=12)
    assert set(counts) == set(CATEGORIES)
    assert set(counts.values()) == {2}
    assert [item["primary_category"] for item in sample[:6]] == list(CATEGORIES)


def test_limited_watchlist_prefers_unique_known_repositories():
    sample = get_watchlist(limit=40)
    known_repos = [
        str(item["repo_override"]).casefold()
        for item in sample
        if item["repo_override"]
    ]

    assert len(known_repos) == len(set(known_repos))
    assert len(get_watchlist(limit=999)) == len(WATCHLIST)


def test_taxonomy_is_backward_compatible_and_capabilities_are_well_formed():
    watchlist = get_watchlist()
    names = {str(item["name"]) for item in watchlist}
    categories_by_name = {
        str(item["name"]): str(item["primary_category"]) for item in watchlist
    }
    tagged_categories: dict[str, set[str]] = defaultdict(set)
    tagged_packages: Counter[str] = Counter()

    assert len(names) == len(watchlist)
    assert set(PACKAGE_CAPABILITIES).issubset(names)
    for item in watchlist:
        assert item["category"] == item["primary_category"]
        assert item["primary_category"] in CATEGORIES
        assert isinstance(item["capabilities"], list)
        assert set(item["capabilities"]).issubset(CAPABILITIES)
        for capability in item["capabilities"]:
            tagged_packages[capability] += 1
            tagged_categories[capability].add(categories_by_name[str(item["name"])])

    # A capability is deliberately cross-cutting, not a disguised tiny category.
    assert set(tagged_packages) == set(CAPABILITIES)
    assert all(count >= 5 for count in tagged_packages.values())
    assert all(len(categories) >= 2 for categories in tagged_categories.values())


def test_capability_parser_normalizes_deduplicates_and_rejects_unknown_tags():
    assert parse_capabilities(
        "inference-serving-runtime, evaluation_observability, inference_serving_runtime"
    ) == ["inference_serving_runtime", "evaluation_observability"]
    assert parse_capabilities(None) == []
    with pytest.raises(ValueError, match="unknown capability"):
        parse_capabilities(["not-a-real-capability"])


def test_repo_overrides_are_well_formed():
    for package, repo in REPO_OVERRIDES.items():
        assert package in {name for name, _ in WATCHLIST}
        assert repo.count("/") == 1
        assert not repo.startswith("/")
        assert not repo.endswith("/")
    assert {
        package: REPO_OVERRIDES[package]
        for package in (
            "instructor",
            "metagpt",
            "pinecone-client",
            "hnswlib",
            "great-expectations",
            "pandera",
        )
    } == {
        "instructor": "567-labs/instructor",
        "metagpt": "FoundationAgents/MetaGPT",
        "pinecone-client": "pinecone-io/python-sdk",
        "hnswlib": "nmslib/hnswlib",
        "great-expectations": "fivetran/great_expectations",
        "pandera": "unionai-oss/pandera",
    }


class _GitHubClient:
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def get_json(self, url: str, params: dict | None = None):
        self.calls.append((url, params))
        if url.endswith("/repos/acme/tool"):
            return {
                "stargazers_count": 10,
                "forks_count": 2,
                "subscribers_count": 3,
                "topics": ["AI", "rag", 7, ""],
                "language": "Python",
            }
        if url.endswith("/repos/acme/tool/stats/participation"):
            return {"all": [0] * 50 + [1, 2]}
        raise AssertionError(f"unexpected GitHub request: {url}")


def test_github_topics_and_language_reuse_existing_repo_request():
    client = _GitHubClient()
    result = github.fetch(client, "acme", "tool", want_velocity=False)

    # Topic casing/order remain raw; malformed non-string values are ignored.
    assert result["github_topics"] == ["AI", "rag"]
    assert result["primary_language"] == "Python"
    assert result["commit_count_4w"] == 3
    assert [url for url, _ in client.calls] == [
        f"{github.BASE}/repos/acme/tool",
        f"{github.BASE}/repos/acme/tool/stats/participation",
    ]
    assert github.parse_topics("ai,rag") == []


def test_collector_propagates_curated_and_github_taxonomy(monkeypatch):
    monkeypatch.setattr(collector.ecosystems, "fetch_package", lambda *_: {"_ok": True})
    monkeypatch.setattr(
        collector.pypi_metadata,
        "fetch",
        lambda *_: {"_ok": True, "repo_url": "https://github.com/acme/tool"},
    )
    monkeypatch.setattr(
        collector.pypi_downloads,
        "fetch",
        lambda *_: {"_ok": True, "downloads_7d": 42, "history": []},
    )
    monkeypatch.setattr(collector.osv, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(collector.ecosystems, "fetch_repo", lambda *_: {"_ok": True})
    monkeypatch.setattr(collector.depsdev, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(
        collector.github,
        "fetch",
        lambda *_: {
            "_ok": True,
            "github_topics": ["rag", "vector-search"],
            "primary_language": "Python",
        },
    )
    package = {
        "name": "demo",
        "category": "llm",
        "primary_category": "llm",
        "capabilities": ["inference_serving_runtime"],
        "repo_override": "acme/tool",
    }

    snapshot = collector.collect_one(package, object(), object(), "run-1")["snapshot"]

    assert snapshot["category"] == "llm"
    assert snapshot["primary_category"] == "llm"
    assert snapshot["capabilities"] == ["inference_serving_runtime"]
    assert snapshot["github_topics"] == ["rag", "vector-search"]
    assert snapshot["primary_language"] == "Python"


def test_collector_propagates_recent_only_vulnerability_severity(monkeypatch):
    monkeypatch.setattr(collector.ecosystems, "fetch_package", lambda *_: {"_ok": True})
    monkeypatch.setattr(collector.pypi_metadata, "fetch", lambda *_: {"_ok": True})
    monkeypatch.setattr(
        collector.pypi_downloads,
        "fetch",
        lambda *_: {"_ok": True, "downloads_7d": 42, "history": []},
    )
    monkeypatch.setattr(
        collector.osv,
        "fetch",
        lambda *_: {
            "_ok": True,
            "vuln_count": 3,
            "vuln_new_28d": 1,
            "max_severity": "CRITICAL",
            "max_severity_new_28d": "HIGH",
        },
    )
    monkeypatch.setattr(collector.depsdev, "fetch", lambda *_: {"_ok": True})
    package = {"name": "demo", "category": "llm"}

    snapshot = collector.collect_one(package, object(), object(), "run-1")["snapshot"]

    assert snapshot["max_severity"] == "CRITICAL"
    assert snapshot["max_severity_new_28d"] == "HIGH"


def test_collect_keeps_an_exception_as_a_healable_failed_snapshot(monkeypatch):
    package = {
        "name": "demo",
        "category": "agents",
        "primary_category": "agents",
        "capabilities": ["workflow_orchestration"],
    }
    monkeypatch.setattr(collector, "get_watchlist", lambda *_: [package])
    monkeypatch.setattr(
        collector,
        "collect_one",
        lambda *_: (_ for _ in ()).throw(RuntimeError("connector explosion")),
    )

    result = collector.collect("run-1", max_workers=1)

    assert result["history"] == []
    assert len(result["snapshots"]) == 1
    assert result["snapshots"][0]["name"] == "demo"
    assert result["snapshots"][0]["downloads_7d"] is None
    assert result["snapshots"][0]["source_status"]["pypi_downloads"] is False


def test_legacy_duckdb_snapshot_schema_migrates_additively(tmp_path):
    warehouse = DuckDBWarehouse(path=str(tmp_path / "legacy.duckdb"))
    warehouse._con.execute(
        'CREATE TABLE snapshots ("run_id" VARCHAR, "snapshot_date" DATE, '
        '"name" VARCHAR, "category" VARCHAR)'
    )

    warehouse.init_schema()
    columns = {
        row[1] for row in warehouse._con.execute('PRAGMA table_info("snapshots")').fetchall()
    }
    assert {
        "category",
        "primary_category",
        "capabilities",
        "github_topics",
        "primary_language",
    }.issubset(columns)

    # Old callers may omit every new field; row preparation fills them with NULL.
    warehouse.insert_rows(
        "snapshots",
        [{
            "run_id": "legacy",
            "snapshot_date": date(2026, 7, 1),
            "name": "demo",
            "category": "llm",
        }],
    )
    row = warehouse.query_df(
        "SELECT category, primary_category, capabilities, github_topics "
        "FROM snapshots WHERE run_id = 'legacy'"
    ).iloc[0]
    assert row["category"] == "llm"
    assert row["primary_category"] is None
    assert row["capabilities"] is None
    assert row["github_topics"] is None
    warehouse.close()

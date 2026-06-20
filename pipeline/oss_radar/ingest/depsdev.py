"""Google deps.dev v3 — versions, resolved dependency count, OpenSSF Scorecard.

Gotchas handled: the scorecard field is ``scorecard`` (optional, absent on some popular
repos); the ``:dependencies`` suffix is a literal colon and 404s for newest/unresolved
versions; scorecard check scores of -1 mean 'unknown' (kept as None, never averaged as 0).
"""

from __future__ import annotations

from urllib.parse import quote

from oss_radar.ingest.http import HttpClient

BASE = "https://api.deps.dev/v3"


def _scorecard_check(scorecard: dict, name: str):
    for c in scorecard.get("checks", []):
        if c.get("name") == name:
            score = c.get("score")
            return None if score is None or score < 0 else score
    return None


def fetch(client: HttpClient, package: str, owner: str = "", repo: str = "") -> dict:
    out: dict = {"_ok": False}

    pkg = client.get_json(f"{BASE}/systems/pypi/packages/{quote(package, safe='')}")
    default_ver = None
    if pkg and pkg.get("versions"):
        versions = pkg["versions"]
        out["version_count_depsdev"] = len(versions)
        default = next((v for v in versions if v.get("isDefault")), versions[-1])
        default_ver = default.get("versionKey", {}).get("version")
        out["_ok"] = True

    # resolved transitive dependency count (404s on unresolved/newest versions)
    if default_ver:
        deps = client.get_json(
            f"{BASE}/systems/pypi/packages/{quote(package, safe='')}/versions/{quote(default_ver, safe='')}:dependencies"
        )
        if deps and "nodes" in deps:
            out["dependency_count"] = sum(1 for n in deps["nodes"] if n.get("relation") != "SELF")

    # project: scorecard (optional) + stars/forks/issues/license
    if owner and repo:
        proj = client.get_json(f"{BASE}/projects/{quote(f'github.com/{owner}/{repo}', safe='')}")
        if proj:
            out["_ok"] = True
            out["stars_depsdev"] = proj.get("starsCount")
            out["forks_depsdev"] = proj.get("forksCount")
            out["open_issues_depsdev"] = proj.get("openIssuesCount")
            out["license_depsdev"] = proj.get("license")
            sc = proj.get("scorecard")
            if sc:
                out["scorecard_overall"] = sc.get("overallScore")
                out["scorecard_maintained"] = _scorecard_check(sc, "Maintained")
                out["scorecard_branch_protection"] = _scorecard_check(sc, "Branch-Protection")
                out["scorecard_code_review"] = _scorecard_check(sc, "Code-Review")
    return out

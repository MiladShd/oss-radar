"""Maintain only narrowly allowlisted OSS Radar GitHub automation.

Once ``scripts/configure_github_rules.sh --apply`` has installed the pull-request-only
GitHub Actions bypass, the workflow token can complete bot PRs without weakening the
signed-commit rule for direct pushes. Keep the policy here deliberately stricter than
that permission: a PR must match a known bot branch, title, labels, author, exact file
set, and green checks before an explicit admin squash merge is attempted.

The same run also consolidates duplicate model-drift issues. It only touches issues
with the exact automation title and labels, authored by the repository owner, and
retains the oldest issue as the auditable canonical thread.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import date
from typing import Any


CONFIG_PATH = "pipeline/oss_radar/config/active_features.json"
ALLOWED_FEATURES = frozenset({"recent_share", "trend_slope_7", "dow_volatility_7"})
EXPECTED_CHECKS = frozenset({"test", "preview", "analyze"})
MIN_FEATURE_LIFT = 0.01
PASSING_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
PENDING_STATES = frozenset({"", "EXPECTED", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING"})
DRIFT_TITLE = re.compile(r"\[oss-radar\] Prediction drift detected \(high\)")


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def gh_try(*args: str) -> tuple[bool, str]:
    try:
        return True, subprocess.check_output(
            ["gh", *args], stderr=subprocess.STDOUT, text=True
        ).strip()
    except subprocess.CalledProcessError as exc:
        return False, exc.output.strip()


def classify_pr(pr: dict[str, Any]) -> dict[str, str] | None:
    """Return a merge policy only for an exact daily-report or model-feature PR."""
    title = pr.get("title") or ""
    branch = pr.get("headRefName") or ""
    labels = {label["name"] for label in pr.get("labels", [])}

    daily_title = re.fullmatch(r"OSS Radar daily brief \u2014 (\d{4}-\d{2}-\d{2})", title)
    daily_branch = re.fullmatch(r"oss-radar/daily-(\d{4}-\d{2}-\d{2})", branch)
    if (
        daily_title
        and daily_branch
        and daily_title.group(1) == daily_branch.group(1)
        and {"oss-radar", "automated"}.issubset(labels)
    ):
        date_str = daily_title.group(1)
        try:
            # Reject impossible dates even when the branch/title regexes agree.
            parsed = str(date.fromisoformat(date_str))
        except ValueError:
            return None
        return {"kind": "daily", "path": f"reports/{parsed}.md", "key": parsed}

    feature_title = re.fullmatch(
        r"Enable growth feature `([a-z0-9_]+)` \(\u0394spearman \+(\d+\.\d{3})\)",
        title,
    )
    if feature_title:
        feature = feature_title.group(1)
        measured_lift = float(feature_title.group(2))
        expected_branch = f"oss-radar/feature-{feature.replace('_', '-')}"
        if (
            feature in ALLOWED_FEATURES
            and measured_lift >= MIN_FEATURE_LIFT
            and branch == expected_branch
            and {"oss-radar", "self-improvement", "model"}.issubset(labels)
        ):
            return {"kind": "feature", "path": CONFIG_PATH, "key": feature}

    return None


def checks_state(checks: list[dict[str, Any]]) -> tuple[str, str]:
    """Classify the complete check rollup as ready, pending, or failed."""
    if not checks:
        return "pending", "no checks reported yet"

    seen: set[str] = set()
    pending: list[str] = []
    failed: list[str] = []
    for check in checks:
        name = check.get("name") or check.get("context") or "check"
        seen.add(name)
        state = str(
            check.get("conclusion") or check.get("state") or check.get("status") or ""
        ).upper()
        if state in PASSING_CONCLUSIONS:
            if name in EXPECTED_CHECKS and state != "SUCCESS":
                failed.append(f"{name}:{state} (required check did not run successfully)")
            continue
        detail = f"{name}:{state or 'UNKNOWN'}"
        if state in PENDING_STATES:
            pending.append(detail)
        else:
            failed.append(detail)

    if failed:
        return "failed", ", ".join(failed)
    missing = sorted(EXPECTED_CHECKS - seen)
    if missing:
        pending.append(f"missing checks: {', '.join(missing)}")
    if pending:
        return "pending", ", ".join(pending)
    return "ready", "all required checks passed"


def validate_feature_config(
    base_config: Any, head_config: Any, feature: str
) -> tuple[bool, str]:
    """Require the feature PR to add exactly its named feature and nothing else."""
    if not isinstance(base_config, dict) or not isinstance(head_config, dict):
        return False, "active feature config is not a JSON object"
    base_download = base_config.get("download")
    if not isinstance(base_download, list) or not all(isinstance(v, str) for v in base_download):
        return False, "base download feature list is invalid"
    if feature in base_download:
        return False, f"feature {feature} is already active on main"

    expected = copy.deepcopy(base_config)
    expected["download"] = [*base_download, feature]
    if head_config != expected:
        return False, "feature PR changes more than the one allowlisted download feature"
    return True, "feature config is an exact one-feature addition"


def read_repo_json(repo: str, path: str, ref: str) -> Any:
    payload = json.loads(gh(
        "api",
        f"repos/{repo}/contents/{path}",
        "--method",
        "GET",
        "-f",
        f"ref={ref}",
    ))
    raw = base64.b64decode(payload["content"].replace("\n", ""))
    return json.loads(raw.decode("utf-8"))


def classify_drift_issue(issue: dict[str, Any], repo_owner: str) -> bool:
    """Return whether an issue is safe for automated duplicate consolidation."""
    title = issue.get("title") or ""
    labels = {label["name"] for label in issue.get("labels", [])}
    author = (issue.get("author") or {}).get("login") or ""
    return (
        DRIFT_TITLE.fullmatch(title) is not None
        and {"oss-radar", "model-drift"}.issubset(labels)
        and author.casefold() == repo_owner.casefold()
    )


def dedupe_drift_issues(repo: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Close exact duplicate drift issues while preserving one canonical thread."""
    repo_owner = repo.split("/", maxsplit=1)[0]
    issues = json.loads(gh(
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "100",
        "--label",
        "oss-radar",
        "--label",
        "model-drift",
        "--json",
        "number,title,labels,author,url",
    ))
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        if classify_drift_issue(issue, repo_owner):
            groups[issue["title"]].append(issue)

    closed: list[str] = []
    failures: list[tuple[str, str]] = []
    for group in groups.values():
        ordered = sorted(group, key=lambda issue: int(issue["number"]))
        if len(ordered) < 2:
            continue
        canonical, *duplicates = ordered
        canonical_number = str(canonical["number"])
        canonical_url = canonical.get("url") or f"https://github.com/{repo}/issues/{canonical_number}"
        closed_urls: list[str] = []
        for duplicate in duplicates:
            number = str(duplicate["number"])
            ok, output = gh_try(
                "issue",
                "close",
                number,
                "--repo",
                repo,
                "--reason",
                "completed",
                "--comment",
                (
                    "Closing as a duplicate generated by the previous non-idempotent "
                    f"drift automation. Continuing the investigation in {canonical_url}."
                ),
            )
            if not ok:
                failures.append((number, output))
                continue
            url = duplicate.get("url") or f"https://github.com/{repo}/issues/{number}"
            closed.append(number)
            closed_urls.append(url)

        if closed_urls:
            body = (
                "<!-- oss-radar-drift-dedup -->\n"
                "Consolidated duplicate automation incidents into this canonical thread:\n"
                + "\n".join(f"- {url}" for url in closed_urls)
            )
            ok, output = gh_try(
                "issue",
                "comment",
                canonical_number,
                "--repo",
                repo,
                "--body",
                body,
            )
            if not ok:
                failures.append((canonical_number, f"canonical comment failed: {output}"))

    return closed, failures


def run(repo: str) -> int:
    prs = json.loads(gh(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "number,title,headRefName,baseRefName,isDraft,isCrossRepository,labels,author",
    ))
    merged: list[str] = []
    merged_feature = False
    skipped: list[tuple[str, str]] = []
    merge_failures: list[tuple[str, str]] = []

    classified: list[tuple[dict[str, Any], dict[str, str]]] = []
    for pr in prs:
        policy = classify_pr(pr)
        if policy is not None:
            classified.append((pr, policy))

    # Daily reports touch independent date-named files. Landing the backlog oldest-first
    # keeps main's public report history chronological and makes repeated runs deterministic.
    classified.sort(
        key=lambda item: (
            0 if item[1]["kind"] == "daily" else 1,
            item[1]["key"],
            int(item[0]["number"]),
        )
    )

    for pr, policy in classified:

        number = str(pr["number"])
        if pr.get("isDraft"):
            skipped.append((number, "draft"))
            continue
        if pr.get("baseRefName") != "main":
            skipped.append((number, "base is not main"))
            continue
        if pr.get("isCrossRepository"):
            skipped.append((number, "cross-repository head is not allowed"))
            continue
        author = (pr.get("author") or {}).get("login") or ""
        repo_owner = repo.split("/", maxsplit=1)[0]
        if author.casefold() != repo_owner.casefold():
            skipped.append((number, f"author {author or 'unknown'} is not the repository owner"))
            continue

        detail = json.loads(gh(
            "pr",
            "view",
            number,
            "--repo",
            repo,
            "--json",
            "files,statusCheckRollup,mergeable,mergeStateStatus,headRefOid",
        ))
        head_oid = str(detail.get("headRefOid") or "")
        if re.fullmatch(r"[0-9a-fA-F]{40}", head_oid) is None:
            skipped.append((number, "head commit is unavailable"))
            continue
        files = [file["path"] for file in detail.get("files", [])]
        if files != [policy["path"]]:
            skipped.append((number, f"unexpected files: {files}"))
            continue

        if policy["kind"] == "feature":
            base_config = read_repo_json(repo, CONFIG_PATH, "main")
            head_config = read_repo_json(repo, CONFIG_PATH, pr["headRefName"])
            valid, reason = validate_feature_config(base_config, head_config, policy["key"])
            if not valid:
                skipped.append((number, reason))
                continue

        mergeable = detail.get("mergeable")
        if mergeable == "CONFLICTING":
            skipped.append((number, "merge conflict"))
            continue
        if mergeable != "MERGEABLE":
            skipped.append((number, f"mergeability is {mergeable or 'unknown'}"))
            continue

        state, reason = checks_state(detail.get("statusCheckRollup") or [])
        if state != "ready":
            skipped.append((number, reason))
            continue

        ok, output = gh_try(
            "pr",
            "merge",
            number,
            "--repo",
            repo,
            "--admin",
            "--squash",
            "--delete-branch",
            "--match-head-commit",
            head_oid,
        )
        if not ok:
            merge_state = detail.get("mergeStateStatus") or "unknown"
            merge_failures.append((number, f"{merge_state}: {output}"))
            continue
        merged.append(number)
        if policy["kind"] == "feature":
            merged_feature = True

    if merged_feature:
        # Pushes created with GITHUB_TOKEN do not start new push-triggered workflows. Dispatch CD
        # explicitly so an allowlisted model-feature merge cannot leave production on old code.
        ok, output = gh_try(
            "workflow",
            "run",
            "deploy.yml",
            "--repo",
            repo,
            "--ref",
            "main",
        )
        if not ok:
            merge_failures.append(("deploy", f"feature merged but deploy dispatch failed: {output}"))

    closed_issues, issue_failures = dedupe_drift_issues(repo)

    print("merged:", ", ".join(f"#{number}" for number in merged) if merged else "none")
    if skipped:
        print("skipped:")
        for number, reason in skipped:
            print(f"- #{number}: {reason}")
    if merge_failures:
        print("merge failures:")
        for number, reason in merge_failures:
            print(f"- #{number}: {reason}")
    print(
        "deduplicated drift issues:",
        ", ".join(f"#{number}" for number in closed_issues) if closed_issues else "none",
    )
    if issue_failures:
        print("issue cleanup failures:")
        for number, reason in issue_failures:
            print(f"- #{number}: {reason}")
    if merge_failures or issue_failures:
        return 1
    return 0


def main() -> int:
    repo = os.environ.get("REPO")
    if not repo:
        print("REPO is required")
        return 2
    return run(repo)


if __name__ == "__main__":
    raise SystemExit(main())

"""GitHub operations for the agent layer (issues + the daily report PR).

All functions degrade gracefully: with no token / repo / network they return None and the
caller logs a skipped activity, so the pipeline never fails because GitHub is unreachable.
"""

from __future__ import annotations

import base64

import requests
import structlog

log = structlog.get_logger(__name__)

_CREATE_COMMIT_MUTATION = """
mutation CreateSignedCommit($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit {
      oid
      url
      signature {
        isValid
        wasSignedByGitHub
      }
    }
  }
}
"""


def _repo(token: str, repo_full: str):
    try:
        from github import Github

        return Github(token).get_repo(repo_full)
    except Exception as exc:  # noqa: BLE001
        log.warning("github.repo_failed", repo=repo_full, error=str(exc))
        return None


def _signed_file_commit(
    token: str,
    repo_full: str,
    branch: str,
    expected_head_oid: str,
    path: str,
    content: str,
    message: str,
) -> str:
    """Create a GitHub-verified commit that adds or replaces one file."""
    response = requests.post(
        "https://api.github.com/graphql",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "query": _CREATE_COMMIT_MUTATION,
            "variables": {
                "input": {
                    "branch": {
                        "repositoryNameWithOwner": repo_full,
                        "branchName": branch,
                    },
                    "expectedHeadOid": expected_head_oid,
                    "message": {"headline": message},
                    "fileChanges": {
                        "additions": [{
                            "path": path,
                            "contents": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                        }],
                    },
                },
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if errors := payload.get("errors"):
        messages = "; ".join(str(error.get("message", "unknown GraphQL error"))
                             for error in errors)
        raise RuntimeError(messages)
    commit = payload["data"]["createCommitOnBranch"]["commit"]
    signature = commit.get("signature") or {}
    if not signature.get("isValid") or not signature.get("wasSignedByGitHub"):
        raise RuntimeError("GitHub did not return a valid GitHub-signed commit")
    return commit["oid"]


def open_issue(token: str, repo_full: str, title: str, body: str,
               labels: list[str] | None = None) -> str | None:
    repo = _repo(token, repo_full)
    if not repo:
        return None
    try:
        issue = repo.create_issue(title=title, body=body, labels=labels or [])
        return issue.html_url
    except Exception as exc:  # noqa: BLE001
        log.warning("github.issue_failed", error=str(exc))
        return None


def open_or_comment_issue(
    token: str,
    repo_full: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str | None:
    """Create an issue, or append to the matching open issue instead of duplicating it."""
    repo = _repo(token, repo_full)
    if not repo:
        return None
    try:
        issue_labels = labels or []
        for issue in repo.get_issues(state="open", labels=issue_labels):
            if issue.title == title:
                issue.create_comment(body)
                return issue.html_url
        issue = repo.create_issue(title=title, body=body, labels=issue_labels)
        return issue.html_url
    except Exception as exc:  # noqa: BLE001
        log.warning("github.issue_upsert_failed", error=str(exc))
        return None


def close_open_issues(
    token: str,
    repo_full: str,
    labels: list[str],
    comment: str,
) -> list[str]:
    """Close open issues matching all labels, adding a final audit comment."""
    repo = _repo(token, repo_full)
    if not repo:
        return []
    closed = []
    try:
        for issue in repo.get_issues(state="open", labels=labels):
            issue.create_comment(comment)
            issue.edit(state="closed")
            closed.append(issue.html_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("github.issue_close_failed", error=str(exc))
    return closed


def open_daily_pr(token: str, repo_full: str, branch: str, report_path: str,
                  report_md: str, title: str, body: str) -> str | None:
    """Create/refresh a branch with a verified daily-report commit and a PR."""
    repo = _repo(token, repo_full)
    if not repo:
        return None
    try:
        base = repo.default_branch
        base_sha = repo.get_branch(base).commit.sha
        existing_prs = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch}"))

        # Daily branches are fully bot-owned. Reset before writing so retries remove any
        # stale or unsigned commit and always compare exactly one report with current main.
        try:
            ref = repo.get_git_ref(f"heads/{branch}")
            ref.edit(sha=base_sha, force=True)
        except Exception:
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)

        _signed_file_commit(
            token,
            repo_full,
            branch,
            base_sha,
            report_path,
            report_md,
            f"chore: daily report {branch}",
        )

        # open PR if one doesn't already exist for this branch
        if existing_prs:
            pr = existing_prs[0]
            pr.edit(title=title, body=body, base=base, state="open")
            try:
                pr.add_to_labels("oss-radar", "automated")
            except Exception:  # noqa: BLE001
                pass
            return pr.html_url
        pr = repo.create_pull(title=title, body=body, head=branch, base=base)
        try:
            pr.add_to_labels("oss-radar", "automated")
        except Exception:  # noqa: BLE001
            pass
        return pr.html_url
    except Exception as exc:  # noqa: BLE001
        log.warning("github.pr_failed", error=str(exc))
        return None


def open_file_pr(token: str, repo_full: str, branch: str, path: str, content: str,
                 title: str, body: str, labels: list[str] | None = None) -> str | None:
    """Open or refresh a PR that creates/updates a single file on a bot branch.

    Used by the self-improvement agent to propose enabling a feature. The branch is reset
    to the current default-branch tip before writing the proposal, so a reused PR never
    accumulates stale commits or keeps testing against an obsolete base. Idempotence is
    still keyed by branch name, so the same proposal never opens duplicate PRs.
    """
    repo = _repo(token, repo_full)
    if not repo:
        return None
    try:
        base = repo.default_branch
        existing = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch}"))
        existing_pr = existing[0] if existing else None
        base_sha = repo.get_branch(base).commit.sha

        # These branches are fully bot-owned. Repointing an existing branch to the latest
        # base keeps the eventual PR to exactly one generated-file change and re-runs CI
        # against current code instead of an old snapshot of main.
        try:
            ref = repo.get_git_ref(f"heads/{branch}")
            ref.edit(sha=base_sha, force=True)
        except Exception:
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)

        _signed_file_commit(
            token,
            repo_full,
            branch,
            base_sha,
            path,
            content,
            f"feat: {title}",
        )

        if existing_pr:
            pr = existing_pr
            # Re-open defensively in case GitHub briefly closed the PR while its bot
            # branch pointed at the base commit during the refresh.
            pr.edit(title=title, body=body, base=base, state="open")
            try:
                pr.add_to_labels(*(labels or ["oss-radar", "automated"]))
            except Exception:  # noqa: BLE001
                pass
            return pr.html_url

        pr = repo.create_pull(title=title, body=body, head=branch, base=base)
        try:
            pr.add_to_labels(*(labels or ["oss-radar", "automated"]))
        except Exception:  # noqa: BLE001
            pass
        return pr.html_url
    except Exception as exc:  # noqa: BLE001
        log.warning("github.file_pr_failed", error=str(exc))
        return None

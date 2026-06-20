"""GitHub operations for the agent layer (issues + the daily report PR).

All functions degrade gracefully: with no token / repo / network they return None and the
caller logs a skipped activity, so the pipeline never fails because GitHub is unreachable.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


def _repo(token: str, repo_full: str):
    try:
        from github import Github

        return Github(token).get_repo(repo_full)
    except Exception as exc:  # noqa: BLE001
        log.warning("github.repo_failed", repo=repo_full, error=str(exc))
        return None


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


def open_daily_pr(token: str, repo_full: str, branch: str, report_path: str,
                  report_md: str, title: str, body: str) -> str | None:
    """Create/refresh a branch with the daily report and open (or reuse) a PR."""
    repo = _repo(token, repo_full)
    if not repo:
        return None
    try:
        base = repo.default_branch
        base_sha = repo.get_branch(base).commit.sha
        # create branch if absent
        try:
            repo.get_branch(branch)
        except Exception:
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)

        # create or update the report file on the branch
        try:
            existing = repo.get_contents(report_path, ref=branch)
            repo.update_file(report_path, f"chore: daily report {branch}", report_md,
                             existing.sha, branch=branch)
        except Exception:
            repo.create_file(report_path, f"chore: daily report {branch}", report_md,
                             branch=branch)

        # open PR if one doesn't already exist for this branch
        existing_prs = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch}"))
        if existing_prs:
            return existing_prs[0].html_url
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
    """Open (or reuse) a PR that creates/updates a single file on a branch.

    Used by the self-improvement agent to propose enabling a feature. Idempotent on the
    branch name, so the same proposal never opens duplicate PRs.
    """
    repo = _repo(token, repo_full)
    if not repo:
        return None
    try:
        base = repo.default_branch
        # reuse an existing open PR for this proposal if present
        existing = list(repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch}"))
        if existing:
            return existing[0].html_url

        base_sha = repo.get_branch(base).commit.sha
        try:
            repo.get_branch(branch)
        except Exception:
            repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base_sha)

        try:
            cur = repo.get_contents(path, ref=branch)
            repo.update_file(path, f"feat: {title}", content, cur.sha, branch=branch)
        except Exception:
            repo.create_file(path, f"feat: {title}", content, branch=branch)

        pr = repo.create_pull(title=title, body=body, head=branch, base=base)
        try:
            pr.add_to_labels(*(labels or ["oss-radar", "automated"]))
        except Exception:  # noqa: BLE001
            pass
        return pr.html_url
    except Exception as exc:  # noqa: BLE001
        log.warning("github.file_pr_failed", error=str(exc))
        return None

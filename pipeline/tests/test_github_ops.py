import pytest

from oss_radar.agents import github_ops


class FakeIssue:
    def __init__(self, title="issue"):
        self.title = title
        self.html_url = f"https://github.test/{title}"
        self.comments = []
        self.state = "open"

    def create_comment(self, body):
        self.comments.append(body)

    def edit(self, state):
        self.state = state


class FakeRepo:
    def __init__(self, issues=None):
        self.issues = issues or []
        self.created = []

    def get_issues(self, state, labels):
        assert state == "open"
        assert labels
        return self.issues

    def create_issue(self, title, body, labels):
        issue = FakeIssue(title)
        issue.body = body
        issue.labels = labels
        self.created.append(issue)
        return issue


class FakePull:
    def __init__(self):
        self.html_url = "https://github.test/pull/7"
        self.edits = []
        self.labels = []

    def edit(self, **kwargs):
        self.edits.append(kwargs)

    def add_to_labels(self, *labels):
        self.labels.extend(labels)


class FakeRef:
    def __init__(self):
        self.edits = []

    def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeFileRepo:
    default_branch = "main"

    def __init__(self, pull=None, branch_exists=True):
        self.owner = type("Owner", (), {"login": "owner"})()
        self.pull = pull
        self.branch_exists = branch_exists
        self.ref = FakeRef()
        self.created_refs = []
        self.created_pulls = []

    def get_branch(self, branch):
        assert branch == "main"
        return type("Branch", (), {"commit": type("Commit", (), {"sha": "new-main-sha"})()})()

    def get_git_ref(self, ref):
        assert ref == "heads/oss-radar/feature-recent-share"
        if not self.branch_exists:
            raise RuntimeError("missing")
        return self.ref

    def create_git_ref(self, **kwargs):
        self.created_refs.append(kwargs)

    def get_pulls(self, state, head):
        assert state == "open"
        assert head == "owner:oss-radar/feature-recent-share"
        return [self.pull] if self.pull else []

    def create_pull(self, **kwargs):
        pull = FakePull()
        self.created_pulls.append(kwargs)
        return pull


class FakeDailyRepo(FakeFileRepo):
    def get_git_ref(self, ref):
        assert ref == "heads/oss-radar/daily-2026-07-27"
        return self.ref

    def get_pulls(self, state, head):
        assert state == "open"
        assert head == "owner:oss-radar/daily-2026-07-27"
        return [self.pull] if self.pull else []


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.raised = False

    def raise_for_status(self):
        self.raised = True

    def json(self):
        return self.payload


def test_signed_file_commit_uses_graphql_base64_and_expected_head(monkeypatch):
    response = FakeResponse({
        "data": {
            "createCommitOnBranch": {
                "commit": {
                    "oid": "signed-sha",
                    "signature": {"isValid": True, "wasSignedByGitHub": True},
                },
            },
        },
    })
    request = {}

    def fake_post(url, **kwargs):
        request["url"] = url
        request.update(kwargs)
        return response

    monkeypatch.setattr(github_ops.requests, "post", fake_post)

    oid = github_ops._signed_file_commit(
        "secret-token",
        "owner/repo",
        "bot-branch",
        "base-sha",
        "reports/today.md",
        "hello\n",
        "chore: report",
    )

    assert oid == "signed-sha"
    assert response.raised
    assert request["url"] == "https://api.github.com/graphql"
    assert request["timeout"] == 30
    assert request["headers"]["Authorization"] == "Bearer secret-token"
    assert request["json"]["variables"]["input"] == {
        "branch": {
            "repositoryNameWithOwner": "owner/repo",
            "branchName": "bot-branch",
        },
        "expectedHeadOid": "base-sha",
        "message": {"headline": "chore: report"},
        "fileChanges": {
            "additions": [{
                "path": "reports/today.md",
                "contents": "aGVsbG8K",
            }],
        },
    }


def test_signed_file_commit_surfaces_graphql_errors(monkeypatch):
    response = FakeResponse({"errors": [{"message": "head changed"}]})
    monkeypatch.setattr(github_ops.requests, "post", lambda *args, **kwargs: response)

    with pytest.raises(RuntimeError, match="head changed"):
        github_ops._signed_file_commit(
            "token", "owner/repo", "branch", "old-sha", "file", "content", "message")


def test_open_or_comment_issue_reuses_matching_open_issue(monkeypatch):
    issue = FakeIssue("[oss-radar] Prediction drift detected (high)")
    repo = FakeRepo([issue])
    monkeypatch.setattr(github_ops, "_repo", lambda token, repo_full: repo)

    url = github_ops.open_or_comment_issue(
        "token", "owner/repo", issue.title, "still high", labels=["oss-radar", "model-drift"])

    assert url == issue.html_url
    assert issue.comments == ["still high"]
    assert repo.created == []


def test_open_or_comment_issue_creates_when_no_match(monkeypatch):
    repo = FakeRepo([FakeIssue("other")])
    monkeypatch.setattr(github_ops, "_repo", lambda token, repo_full: repo)

    url = github_ops.open_or_comment_issue(
        "token", "owner/repo", "new", "body", labels=["oss-radar", "model-drift"])

    assert url == "https://github.test/new"
    assert len(repo.created) == 1
    assert repo.created[0].body == "body"


def test_close_open_issues_comments_and_closes(monkeypatch):
    issues = [FakeIssue("a"), FakeIssue("b")]
    repo = FakeRepo(issues)
    monkeypatch.setattr(github_ops, "_repo", lambda token, repo_full: repo)

    closed = github_ops.close_open_issues(
        "token", "owner/repo", labels=["oss-radar", "model-drift"], comment="recovered")

    assert closed == [issue.html_url for issue in issues]
    assert [issue.comments for issue in issues] == [["recovered"], ["recovered"]]
    assert [issue.state for issue in issues] == ["closed", "closed"]


def test_open_daily_pr_resets_branch_and_writes_verified_commit(monkeypatch):
    pull = FakePull()
    repo = FakeDailyRepo(pull=pull)
    commits = []
    monkeypatch.setattr(github_ops, "_repo", lambda token, repo_full: repo)
    monkeypatch.setattr(
        github_ops,
        "_signed_file_commit",
        lambda *args: commits.append(args) or "signed-sha",
    )

    url = github_ops.open_daily_pr(
        "token",
        "owner/repo",
        "oss-radar/daily-2026-07-27",
        "reports/2026-07-27.md",
        "# Daily report\n",
        "OSS Radar daily brief — 2026-07-27",
        "Automated report.",
    )

    assert url == pull.html_url
    assert repo.ref.edits == [{"sha": "new-main-sha", "force": True}]
    assert commits == [(
        "token",
        "owner/repo",
        "oss-radar/daily-2026-07-27",
        "new-main-sha",
        "reports/2026-07-27.md",
        "# Daily report\n",
        "chore: daily report oss-radar/daily-2026-07-27",
    )]
    assert pull.edits == [{
        "title": "OSS Radar daily brief — 2026-07-27",
        "body": "Automated report.",
        "base": "main",
        "state": "open",
    }]
    assert pull.labels == ["oss-radar", "automated"]
    assert repo.created_pulls == []


def test_open_file_pr_refreshes_existing_branch_and_pull_request(monkeypatch):
    pull = FakePull()
    repo = FakeFileRepo(pull=pull)
    commits = []
    monkeypatch.setattr(github_ops, "_repo", lambda token, repo_full: repo)
    monkeypatch.setattr(
        github_ops,
        "_signed_file_commit",
        lambda *args: commits.append(args) or "signed-sha",
    )

    url = github_ops.open_file_pr(
        "token",
        "owner/repo",
        "oss-radar/feature-recent-share",
        "pipeline/oss_radar/config/active_features.json",
        '{"download": ["recent_share"]}\n',
        "Enable growth feature `recent_share` (\u0394spearman +0.013)",
        "fresh experiment details",
        labels=["oss-radar", "self-improvement", "model"],
    )

    assert url == pull.html_url
    assert repo.ref.edits == [{"sha": "new-main-sha", "force": True}]
    assert repo.created_refs == []
    assert commits == [(
        "token",
        "owner/repo",
        "oss-radar/feature-recent-share",
        "new-main-sha",
        "pipeline/oss_radar/config/active_features.json",
        '{"download": ["recent_share"]}\n',
        "feat: Enable growth feature `recent_share` (Δspearman +0.013)",
    )]
    assert pull.edits == [{
        "title": "Enable growth feature `recent_share` (\u0394spearman +0.013)",
        "body": "fresh experiment details",
        "base": "main",
        "state": "open",
    }]
    assert pull.labels == ["oss-radar", "self-improvement", "model"]
    assert repo.created_pulls == []


def test_open_file_pr_creates_missing_branch_and_new_pull_request(monkeypatch):
    repo = FakeFileRepo(branch_exists=False)
    commits = []
    monkeypatch.setattr(github_ops, "_repo", lambda token, repo_full: repo)
    monkeypatch.setattr(
        github_ops,
        "_signed_file_commit",
        lambda *args: commits.append(args) or "signed-sha",
    )

    url = github_ops.open_file_pr(
        "token",
        "owner/repo",
        "oss-radar/feature-recent-share",
        "pipeline/oss_radar/config/active_features.json",
        '{"download": ["recent_share"]}\n',
        "Enable growth feature `recent_share` (\u0394spearman +0.013)",
        "experiment details",
    )

    assert url == "https://github.test/pull/7"
    assert repo.created_refs == [{
        "ref": "refs/heads/oss-radar/feature-recent-share",
        "sha": "new-main-sha",
    }]
    assert commits == [(
        "token",
        "owner/repo",
        "oss-radar/feature-recent-share",
        "new-main-sha",
        "pipeline/oss_radar/config/active_features.json",
        '{"download": ["recent_share"]}\n',
        "feat: Enable growth feature `recent_share` (Δspearman +0.013)",
    )]
    assert repo.created_pulls == [{
        "title": "Enable growth feature `recent_share` (\u0394spearman +0.013)",
        "body": "experiment details",
        "head": "oss-radar/feature-recent-share",
        "base": "main",
    }]

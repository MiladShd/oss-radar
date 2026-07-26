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


class FakeFile:
    sha = "old-file-sha"


class FakeFileRepo:
    default_branch = "main"

    def __init__(self, pull=None, branch_exists=True):
        self.owner = type("Owner", (), {"login": "owner"})()
        self.pull = pull
        self.branch_exists = branch_exists
        self.ref = FakeRef()
        self.created_refs = []
        self.updated_files = []
        self.created_files = []
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

    def get_contents(self, path, ref):
        assert path == "pipeline/oss_radar/config/active_features.json"
        assert ref == "oss-radar/feature-recent-share"
        return FakeFile()

    def update_file(self, *args, **kwargs):
        self.updated_files.append((args, kwargs))

    def create_file(self, *args, **kwargs):
        self.created_files.append((args, kwargs))

    def get_pulls(self, state, head):
        assert state == "open"
        assert head == "owner:oss-radar/feature-recent-share"
        return [self.pull] if self.pull else []

    def create_pull(self, **kwargs):
        pull = FakePull()
        self.created_pulls.append(kwargs)
        return pull


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


def test_open_file_pr_refreshes_existing_branch_and_pull_request(monkeypatch):
    pull = FakePull()
    repo = FakeFileRepo(pull=pull)
    monkeypatch.setattr(github_ops, "_repo", lambda token, repo_full: repo)

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
    assert len(repo.updated_files) == 1
    assert repo.updated_files[0][1]["branch"] == "oss-radar/feature-recent-share"
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
    monkeypatch.setattr(github_ops, "_repo", lambda token, repo_full: repo)

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
    assert repo.created_pulls == [{
        "title": "Enable growth feature `recent_share` (\u0394spearman +0.013)",
        "body": "experiment details",
        "head": "oss-radar/feature-recent-share",
        "base": "main",
    }]

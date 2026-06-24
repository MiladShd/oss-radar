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

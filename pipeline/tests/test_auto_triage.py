import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "auto_triage.py"
_SPEC = spec_from_file_location("oss_radar_auto_triage", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
auto_triage = module_from_spec(_SPEC)
_SPEC.loader.exec_module(auto_triage)


def _labels(*names):
    return [{"name": name} for name in names]


def _passing_checks():
    return [
        {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "preview", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "analyze", "status": "COMPLETED", "conclusion": "SUCCESS"},
    ]


def test_classify_pr_requires_matching_daily_identity():
    pr = {
        "title": "OSS Radar daily brief \u2014 2026-07-20",
        "headRefName": "oss-radar/daily-2026-07-20",
        "labels": _labels("oss-radar", "automated"),
    }

    assert auto_triage.classify_pr(pr) == {
        "kind": "daily",
        "path": "reports/2026-07-20.md",
        "key": "2026-07-20",
    }
    pr["headRefName"] = "oss-radar/daily-2026-07-19"
    assert auto_triage.classify_pr(pr) is None
    pr["title"] = "OSS Radar daily brief \u2014 2026-02-31"
    pr["headRefName"] = "oss-radar/daily-2026-02-31"
    assert auto_triage.classify_pr(pr) is None


def test_classify_pr_allows_only_known_model_feature_branches():
    pr = {
        "title": "Enable growth feature `trend_slope_7` (\u0394spearman +0.013)",
        "headRefName": "oss-radar/feature-trend-slope-7",
        "labels": _labels("oss-radar", "self-improvement", "model"),
    }

    assert auto_triage.classify_pr(pr) == {
        "kind": "feature",
        "path": auto_triage.CONFIG_PATH,
        "key": "trend_slope_7",
    }
    pr["title"] = "Enable growth feature `trend_slope_7` (\u0394spearman -0.013)"
    assert auto_triage.classify_pr(pr) is None
    pr["title"] = "Enable growth feature `trend_slope_7` (\u0394spearman +0.009)"
    assert auto_triage.classify_pr(pr) is None
    pr["title"] = "Enable growth feature `arbitrary_code` (\u0394spearman +9.999)"
    pr["headRefName"] = "oss-radar/feature-arbitrary-code"
    assert auto_triage.classify_pr(pr) is None


def test_validate_feature_config_rejects_any_additional_change():
    base = {"download": ["log_d7"], "risk": ["log_stars"]}
    head = {"download": ["log_d7", "recent_share"], "risk": ["log_stars"]}

    assert auto_triage.validate_feature_config(base, head, "recent_share")[0]
    head["risk"].append("unreviewed_risk_feature")
    valid, reason = auto_triage.validate_feature_config(base, head, "recent_share")
    assert not valid
    assert "more than" in reason


def test_checks_state_requires_every_expected_check():
    assert auto_triage.checks_state(_passing_checks())[0] == "ready"
    assert auto_triage.checks_state(_passing_checks()[:-1])[0] == "pending"

    failed = _passing_checks()
    failed[0]["conclusion"] = "FAILURE"
    assert auto_triage.checks_state(failed)[0] == "failed"

    skipped = _passing_checks()
    skipped[1]["conclusion"] = "SKIPPED"
    assert auto_triage.checks_state(skipped)[0] == "failed"


def test_run_uses_normal_squash_and_fails_loudly_when_merge_fails(monkeypatch, capsys):
    pr = {
        "number": 42,
        "title": "OSS Radar daily brief \u2014 2026-07-20",
        "headRefName": "oss-radar/daily-2026-07-20",
        "baseRefName": "main",
        "isDraft": False,
        "isCrossRepository": False,
        "author": {"login": "owner"},
        "labels": _labels("oss-radar", "automated"),
    }
    detail = {
        "files": [{"path": "reports/2026-07-20.md"}],
        "statusCheckRollup": _passing_checks(),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "headRefOid": "a" * 40,
    }

    def fake_gh(*args):
        if args[:2] == ("pr", "list"):
            return json.dumps([pr])
        if args[:2] == ("pr", "view"):
            return json.dumps(detail)
        raise AssertionError(args)

    merge_calls = []

    def fake_gh_try(*args):
        merge_calls.append(args)
        return False, "required signed commit"

    monkeypatch.setattr(auto_triage, "gh", fake_gh)
    monkeypatch.setattr(auto_triage, "gh_try", fake_gh_try)
    monkeypatch.setattr(auto_triage, "dedupe_drift_issues", lambda repo: ([], []))

    assert auto_triage.run("owner/repo") == 1
    assert merge_calls == [(
        "pr", "merge", "42", "--repo", "owner/repo",
        "--squash", "--delete-branch",
        "--match-head-commit", "a" * 40,
    )]
    assert "merge failures" in capsys.readouterr().out


def test_run_updates_allowlisted_behind_branch_before_merging(monkeypatch, capsys):
    pr = {
        "number": 42,
        "title": "OSS Radar daily brief \u2014 2026-07-20",
        "headRefName": "oss-radar/daily-2026-07-20",
        "baseRefName": "main",
        "isDraft": False,
        "isCrossRepository": False,
        "author": {"login": "owner"},
        "labels": _labels("oss-radar", "automated"),
    }
    detail = {
        "files": [{"path": "reports/2026-07-20.md"}],
        "statusCheckRollup": _passing_checks(),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BEHIND",
        "headRefOid": "a" * 40,
    }

    def fake_gh(*args):
        if args[:2] == ("pr", "list"):
            return json.dumps([pr])
        if args[:2] == ("pr", "view"):
            return json.dumps(detail)
        raise AssertionError(args)

    calls = []

    def fake_gh_try(*args):
        calls.append(args)
        return True, ""

    monkeypatch.setattr(auto_triage, "gh", fake_gh)
    monkeypatch.setattr(auto_triage, "gh_try", fake_gh_try)
    monkeypatch.setattr(auto_triage, "dedupe_drift_issues", lambda repo: ([], []))

    assert auto_triage.run("owner/repo") == 0
    assert calls == [(
        "api", "--method", "PUT", "repos/owner/repo/pulls/42/update-branch",
        "-f", f"expected_head_sha={'a' * 40}",
    )]
    assert "waiting for checks" in capsys.readouterr().out


def test_feature_merge_explicitly_dispatches_deploy(monkeypatch):
    pr = {
        "number": 43,
        "title": "Enable growth feature `recent_share` (\u0394spearman +0.013)",
        "headRefName": "oss-radar/feature-recent-share",
        "baseRefName": "main",
        "isDraft": False,
        "isCrossRepository": False,
        "author": {"login": "owner"},
        "labels": _labels("oss-radar", "self-improvement", "model"),
    }
    detail = {
        "files": [{"path": auto_triage.CONFIG_PATH}],
        "statusCheckRollup": _passing_checks(),
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "b" * 40,
    }
    base = {"download": ["log_d7"], "risk": ["log_stars"]}
    head = {"download": ["log_d7", "recent_share"], "risk": ["log_stars"]}

    def fake_gh(*args):
        if args[:2] == ("pr", "list"):
            return json.dumps([pr])
        if args[:2] == ("pr", "view"):
            return json.dumps(detail)
        raise AssertionError(args)

    calls = []

    def fake_gh_try(*args):
        calls.append(args)
        return True, ""

    monkeypatch.setattr(auto_triage, "gh", fake_gh)
    monkeypatch.setattr(
        auto_triage,
        "read_repo_json",
        lambda repo, path, ref: base if ref == "main" else head,
    )
    monkeypatch.setattr(auto_triage, "gh_try", fake_gh_try)
    monkeypatch.setattr(auto_triage, "dedupe_drift_issues", lambda repo: ([], []))

    assert auto_triage.run("owner/repo") == 0
    assert calls == [
        (
            "pr", "merge", "43", "--repo", "owner/repo",
            "--squash", "--delete-branch",
            "--match-head-commit", "b" * 40,
        ),
        (
            "workflow", "run", "deploy.yml", "--repo", "owner/repo", "--ref", "main",
        ),
    ]


def test_dedupe_drift_issues_closes_only_exact_owner_duplicates(monkeypatch):
    exact = {
        "title": "[oss-radar] Prediction drift detected (high)",
        "labels": _labels("oss-radar", "model-drift"),
        "author": {"login": "owner"},
    }
    issues = [
        {**exact, "number": 27, "url": "https://example.test/issues/27"},
        {**exact, "number": 31, "url": "https://example.test/issues/31"},
        {**exact, "number": 36, "url": "https://example.test/issues/36"},
        {
            **exact,
            "number": 40,
            "url": "https://example.test/issues/40",
            "author": {"login": "contributor"},
        },
        {
            **exact,
            "number": 41,
            "url": "https://example.test/issues/41",
            "labels": _labels("oss-radar"),
        },
    ]

    def fake_gh(*args):
        assert args[:2] == ("issue", "list")
        assert ("--limit", "100") == args[args.index("--limit"):args.index("--limit") + 2]
        return json.dumps(issues)

    calls = []

    def fake_gh_try(*args):
        calls.append(args)
        return True, ""

    monkeypatch.setattr(auto_triage, "gh", fake_gh)
    monkeypatch.setattr(auto_triage, "gh_try", fake_gh_try)

    closed, failures = auto_triage.dedupe_drift_issues("owner/repo")

    assert closed == ["31", "36"]
    assert failures == []
    assert [call[:3] for call in calls] == [
        ("issue", "close", "31"),
        ("issue", "close", "36"),
        ("issue", "comment", "27"),
    ]
    assert "https://example.test/issues/27" in calls[0][-1]
    assert "https://example.test/issues/31" in calls[2][-1]
    assert "https://example.test/issues/36" in calls[2][-1]


def test_dedupe_drift_issues_reports_close_failures(monkeypatch):
    issues = [
        {
            "number": 27,
            "title": "[oss-radar] Prediction drift detected (high)",
            "labels": _labels("oss-radar", "model-drift"),
            "author": {"login": "owner"},
            "url": "https://example.test/issues/27",
        },
        {
            "number": 31,
            "title": "[oss-radar] Prediction drift detected (high)",
            "labels": _labels("oss-radar", "model-drift"),
            "author": {"login": "owner"},
            "url": "https://example.test/issues/31",
        },
    ]
    monkeypatch.setattr(auto_triage, "gh", lambda *args: json.dumps(issues))
    monkeypatch.setattr(auto_triage, "gh_try", lambda *args: (False, "permission denied"))

    closed, failures = auto_triage.dedupe_drift_issues("owner/repo")

    assert closed == []
    assert failures == [("31", "permission denied")]

"""Dependency-audit parsing + verdict logic (the version-aware honesty rules)."""
from oss_radar.audit import parse_requirements
from oss_radar.audit.auditor import _verdict


def test_parse_extracts_name_and_pinned_version():
    deps = parse_requirements(
        "transformers==4.30.0\n"
        "langchain>=0.1\n"
        "# a comment\n"
        "fastapi[all]==0.100.0\n"
        "-r dev-requirements.txt\n"
        "git+https://github.com/x/y.git\n"
        "flask ; python_version > '3.8'\n"
    )
    d = dict(deps)
    assert d["transformers"] == "4.30.0"
    assert d["langchain"] is None          # non-== specifier => no pinned version
    assert d["fastapi"] == "0.100.0"       # extras stripped, version kept
    assert d["flask"] is None              # env marker stripped
    assert len(deps) == 4                  # -r include and VCS url skipped


def test_parse_dedupes_and_normalizes():
    deps = parse_requirements("Scikit_Learn==1.0\nscikit-learn==1.1\n")
    assert len(deps) == 1 and deps[0][0] == "scikit-learn"


def test_verdict_is_version_aware():
    # a pinned version actually exposed to a high/critical vuln => critical
    assert _verdict(5, 3, "CRITICAL", 5, False, 0.0, "active")[0] == "critical"
    # lifetime CVEs on an UNPINNED package must NOT auto-critical — only "watch", honestly framed
    v, reason = _verdict(5, 38, "CRITICAL", 5, False, 0.0, "historical")
    assert v == "watch" and "pin a version" in reason
    # clean, fresh, maintained => healthy
    assert _verdict(10, 0, None, 10, False, 5.0, "active")[0] == "healthy"
    # stale release => watch
    assert _verdict(10, 0, None, 400, False, 0.0, "historical")[0] == "watch"
    # archived upstream => critical regardless
    assert _verdict(0, 0, None, 1, True, 0.0, "active")[0] == "critical"

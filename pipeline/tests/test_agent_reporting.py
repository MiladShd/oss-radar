"""Agent report contracts, including deterministic source-health disclosure."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

from oss_radar.agents.crew import run_crew


class _FakeLLM:
    def __init__(self, output: str | None):
        self.output = output

    @property
    def available(self) -> bool:
        return self.output is not None

    def generate(self, *_args, **_kwargs) -> str | None:
        return self.output


@pytest.mark.parametrize("llm_output", [None, "Generated operational summary."])
@pytest.mark.parametrize(
    ("backend", "expected_hint", "unexpected_hint"),
    [
        ("duckdb", "export OSS_RADAR_GITHUB_TOKEN", "Secret Manager"),
        ("bigquery", "oss-radar-github-token", "export OSS_RADAR_GITHUB_TOKEN"),
    ],
)
def test_daily_report_propagates_degraded_source_health(
    tmp_path,
    monkeypatch,
    llm_output,
    backend,
    expected_hint,
    unexpected_hint,
):
    monkeypatch.chdir(tmp_path)
    settings = SimpleNamespace(
        feature_lift_margin=0.01,
        min_train_rows=200,
        random_seed=42,
        github_token="",
        github_repo="MiladShd/oss-radar",
        backend=backend,
        env="cloud" if backend == "bigquery" else "local",
    )
    snapshots = pd.DataFrame([
        {
            "name": "alpha",
            "downloads_7d": 10,
            "source_status": json.dumps({
                "github": False,
                "osv": True,
                "pypi_downloads": True,
            }),
        },
        {
            "name": "beta",
            "downloads_7d": 20,
            "source_status": {
                "github": True,
                "osv": True,
                "pypi_downloads": True,
            },
        },
    ])
    predictions = pd.DataFrame([
        {
            "name": "alpha",
            "momentum_score": 70.0,
            "growth_pred_70d": 0.1,
            "risk_score": 30.0,
            "risk_level": "low",
            "top_reasons": ["download growth", "healthy maintenance"],
        },
    ])

    result = run_crew(
        "run-1",
        settings,
        _FakeLLM(llm_output),
        snapshots,
        predictions,
        model_metrics={},
        dry_run=True,
    )

    report = result["report_md"]
    assert report.count("## Source health") == 1
    assert "| `github` | 50% | degraded" in report
    assert "| `osv` | 100% | healthy |" in report
    assert expected_hint in report
    assert unexpected_hint not in report
    if backend == "bigquery":
        assert "docs/OPERATIONS.md" in report
    assert result["engineering"]["degraded_sources"] == ["github"]
    assert result["engineering"]["github_token_hint"]
    activity = next(
        item
        for item in result["activities"]
        if item["agent"] == "DataEngineer"
    )
    assert activity["status"] == "warning"
    assert expected_hint in activity["summary"]
    assert next((tmp_path / "reports").glob("*.md")).read_text() == report

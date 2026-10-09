"""The OSS Radar agent crew.

Agents *manage* the pipeline; they are not the model. Each records what it did to the
activity log (surfaced on the dashboard timeline), and the Risk Analyst + MLOps agents
produce the daily report and open a GitHub PR for it.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import structlog

from oss_radar.agents import github_ops
from oss_radar.agents.context import AgentContext
from oss_radar.agents.improver import run_improver
from oss_radar.source_health import github_recovery_guidance
from oss_radar.textfmt import (
    count,
    join_and,
    label_mode,
    metric_label,
    percent,
    source_label,
    version_label,
)

log = structlog.get_logger(__name__)

REPORT_SYSTEM = (
    "You are the Risk Analyst for OSS Radar, an open-source intelligence platform. "
    "Write a crisp, factual daily brief in GitHub-flavored Markdown for engineers choosing "
    "and monitoring Python/AI dependencies. Be specific and quantitative; no hype, no preamble. "
    "Output only the Markdown report."
)

SOURCE_HEALTH_THRESHOLD = 0.7


def _is_cloud(settings) -> bool:
    return bool(
        getattr(settings, "is_cloud", False)
        or getattr(settings, "backend", "") == "bigquery"
        or getattr(settings, "env", "") == "cloud"
    )


# --- Data Engineer: ingestion freshness / source health ---
def _data_engineer(ctx: AgentContext, snapshots: pd.DataFrame) -> dict:
    import json

    rates: dict[str, float] = {}
    if not snapshots.empty and "source_status" in snapshots:
        per_source: dict[str, list[int]] = {}
        for raw in snapshots["source_status"].dropna():
            try:
                d = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:  # noqa: BLE001
                continue
            for src, ok in (d or {}).items():
                per_source.setdefault(src, []).append(1 if ok else 0)
        rates = {s: round(sum(v) / len(v), 3) for s, v in per_source.items() if v}

    down = [s for s, r in rates.items() if r == 0.0]
    degraded = [s for s, r in rates.items() if 0 < r < SOURCE_HEALTH_THRESHOLD]
    status = "error" if down else "warning" if degraded else "ok"
    github_hint = github_recovery_guidance(is_cloud=_is_cloud(ctx.settings))
    if not len(snapshots):
        summary = "No package snapshots were ingested in this run."
    else:
        per_source = ", ".join(f"{source_label(s)} {percent(r)}" for s, r in sorted(rates.items()))
        if down:
            problem = f"{join_and([source_label(s) for s in down])} returned no data"
        elif degraded:
            problem = (
                f"{join_and([source_label(s) for s in degraded])} fell below the "
                f"{percent(SOURCE_HEALTH_THRESHOLD)} health threshold"
            )
        else:
            problem = ""
        summary = (
            f"Ingested {count(len(snapshots))} packages from {len(rates)} data sources. "
            + (f"{problem[0].upper() + problem[1:]}. " if problem else "All sources are healthy. ")
            + f"Success rate by source: {per_source}."
        )
    if rates.get("github", 1.0) < SOURCE_HEALTH_THRESHOLD:
        summary += f" {github_hint}"
    ctx.record("DataEngineer", "check_ingestion_freshness", status, summary)

    if down and not ctx.dry_run and ctx.settings.github_token:
        guidance = (
            f"\n\n{github_hint}"
            if "github" in down
            else ""
        )
        url = github_ops.open_issue(
            ctx.settings.github_token, ctx.settings.github_repo,
            title=f"[oss-radar] Data source down: {', '.join(down)}",
            body=("The daily pipeline observed a 0% success rate for: "
                  f"{', '.join(down)}.\n\nSource success rates this run:\n"
                  + "\n".join(f"- `{s}`: {int(r*100)}%" for s, r in sorted(rates.items()))
                  + guidance),
            labels=["oss-radar", "data-incident"],
        )
        if url:
            ctx.record(
                "DataEngineer", "open_issue", "ok",
                f"Opened a GitHub incident for the failing source(s): "
                f"{join_and([source_label(s) for s in down])}.", url)
    return {
        "source_ok_rates": rates,
        "degraded_sources": sorted(down + degraded),
        "github_token_hint": (
            github_hint
            if rates.get("github", 1.0) < SOURCE_HEALTH_THRESHOLD
            else ""
        ),
    }


# --- Healer: report on self-healing actions taken during ingest ---
def _healer(ctx: AgentContext, heal_stats: dict | None) -> None:
    if not heal_stats or not heal_stats.get("failed"):
        ctx.record("Healer", "self_heal_ingest", "ok", "All sources responded normally, so no self-healing was needed.")
        return
    s = heal_stats
    status = "ok" if s.get("recovered", 0) or s.get("carried_forward", 0) else "warning"
    ctx.record(
        "Healer", "self_heal_ingest", status,
        f"{count(s['failed'])} package(s) failed during ingestion. "
        f"{count(s.get('recovered', 0))} recovered after a retry and "
        f"{count(s.get('carried_forward', 0))} were filled in from their last good snapshot.",
    )


# --- Data Quality: nulls / dupes / coverage ---
def _data_quality(ctx: AgentContext, snapshots: pd.DataFrame) -> dict:
    n = len(snapshots)
    dupes = int(snapshots["name"].duplicated().sum()) if n else 0
    with_downloads = int(snapshots["downloads_7d"].notna().sum()) if n else 0
    coverage = round(with_downloads / n, 3) if n else 0.0
    key_cols = ["stars", "dependent_repos_count", "scorecard_overall", "bus_factor"]
    null_rates = {c: round(snapshots[c].isna().mean(), 3) for c in key_cols if c in snapshots}
    status = "ok" if (dupes == 0 and coverage >= 0.8) else "warning"
    names = {"stars": "stars", "dependent_repos_count": "dependent repos",
             "scorecard_overall": "OpenSSF scorecard", "bus_factor": "bus factor"}
    verdict = (
        "Feature table passed its checks"
        if status == "ok"
        else "Feature table needs attention"
    )
    summary = (
        f"{verdict}: {percent(coverage)} of packages have download data and "
        f"{count(dupes)} duplicate package(s) were found. Missing-data rates: "
        + ", ".join(f"{names.get(c, c)} {percent(r)}" for c, r in null_rates.items())
        + "."
    )
    ctx.record("DataQuality", "validate_feature_table", status, summary)
    return {"coverage": coverage, "duplicates": dupes, "null_rates": null_rates}


# --- Data Scientist: training + champion/challenger + drift monitoring ---
def _data_scientist(ctx: AgentContext, model_metrics: dict) -> None:
    for name, m in model_metrics.items():
        primary = "spearman" if name == "growth" else "auc"
        val = m.get(primary)
        label = metric_label(primary)
        val_str = f"{label} {val:.3f}" if isinstance(val, (int, float)) and val == val else f"{label} unavailable"
        note = m.get("promotion_note") or (
            "Promoted: it is now the champion." if m.get("is_champion") else "Kept as challenger."
        )
        n_train = m.get("n_train") or m.get("n_samples")
        serving_metric = m.get(f"serving_{primary}")
        serving_value = (f" ({label} {serving_metric:.3f})"
                         if isinstance(serving_metric, (int, float)) and serving_metric == serving_metric
                         else "")
        labels = (f" Training labels: {label_mode(m['label_mode'])}."
                  if name == "risk" and m.get("label_mode") else "")
        ctx.record(
            "DataScientist", f"retrain_{name}_model", "ok",
            f"Retrained the {name} model on {count(n_train)} rows ({val_str}). {note} "
            f"The model in production is the one {version_label(m.get('serving'))}{serving_value}.{labels}",
        )


def _model_monitor(ctx: AgentContext, drift: dict | None) -> None:
    if not drift or not drift.get("available"):
        ctx.record("DataScientist", "monitor_drift", "ok",
                   "No earlier run to compare against, so this run becomes the baseline for drift checks.")
        return
    sev = drift.get("severity", "low")
    summary = (
        f"Prediction drift versus the previous run is {sev}: momentum PSI "
        f"{drift.get('momentum_score_psi')}, risk PSI {drift.get('risk_score_psi')}, and "
        f"{percent(drift.get('label_churn', 0))} of labels changed. "
        "(PSI below 0.10 is stable; above 0.25 is significant.)"
    )
    ctx.record("DataScientist", "monitor_drift", "warning" if sev == "high" else "ok", summary)
    if sev == "high":
        ctx.record("DataScientist", "recommend_action", "warning",
                   "Significant drift detected and flagged for feature review. The models retrain on every run.")
        if not ctx.dry_run and ctx.settings.github_token:
            issue_body = summary + "\n\nRecommend reviewing input features and confirming the retrain."
            url = github_ops.open_or_comment_issue(
                ctx.settings.github_token, ctx.settings.github_repo,
                title=f"[oss-radar] Prediction drift detected ({sev})",
                body=issue_body,
                labels=["oss-radar", "model-drift"])
            if url:
                ctx.record("DataScientist", "track_issue", "ok", "Opened or updated a GitHub issue to track the drift investigation.", url)
    elif sev == "low" and not ctx.dry_run and ctx.settings.github_token:
        closed = github_ops.close_open_issues(
            ctx.settings.github_token, ctx.settings.github_repo,
            labels=["oss-radar", "model-drift"],
            comment=summary + "\n\nDrift has returned to the low band; closing this investigation.",
        )
        if closed:
            ctx.record("DataScientist", "close_issue", "ok",
                       f"Closed {len(closed)} drift issue(s) because drift returned to normal.", closed[0])


# --- Risk Analyst: the daily human-readable report ---
def _movers(preds: pd.DataFrame, by: str, n: int = 6, asc: bool = False) -> pd.DataFrame:
    if preds.empty or by not in preds:
        return preds
    return preds.sort_values(by, ascending=asc).head(n)


def _metric_text(value) -> str:
    return f"{value:.3f}" if isinstance(value, (int, float)) and value == value else "n/a"


def _row_reasons(row: pd.Series, kind: str) -> list[str]:
    key = "momentum_reasons" if kind == "momentum" else "risk_reasons"
    value = row.get(key)
    if isinstance(value, list):
        return value
    legacy = row.get("top_reasons")
    if not isinstance(legacy, list):
        return []
    return legacy[:2] if kind == "momentum" else legacy[-2:]


def _source_health_markdown(engineering: dict | None) -> str:
    rates = (engineering or {}).get("source_ok_rates") or {}
    if not rates:
        return ""
    github_hint = (engineering or {}).get("github_token_hint") or ""
    lines = [
        "## Source health",
        "",
        "| Source | Success | Status |",
        "|---|---:|---|",
    ]
    for source, rate in sorted(rates.items()):
        if rate == 0:
            status = "down"
        elif rate < SOURCE_HEALTH_THRESHOLD:
            status = "degraded"
        else:
            status = "healthy"
        if source == "github" and rate < SOURCE_HEALTH_THRESHOLD and github_hint:
            status += f" — {github_hint}"
        lines.append(f"| `{source}` | {rate:.0%} | {status} |")
    return "\n".join(lines)


def _template_report(date_str: str, preds: pd.DataFrame, model_metrics: dict,
                     quality: dict, engineering: dict | None = None) -> str:
    mom = _movers(preds, "momentum_score")
    risk = _movers(preds, "risk_score")
    lines = [f"# OSS Radar — Daily Brief {date_str}", ""]
    gm = model_metrics.get("growth", {})
    rm = model_metrics.get("risk", {})
    lines.append(
        f"_Tracked {len(preds)} packages · served growth spearman "
        f"{_metric_text(gm.get('serving_spearman'))} · served risk auc "
        f"{_metric_text(rm.get('serving_auc'))} · "
        f"download coverage {quality.get('coverage', 0)*100:.0f}%_"
    )
    lines += ["", "## 🚀 Momentum movers", "", "| Package | Momentum | Pred 70d growth | Why |",
              "|---|---|---|---|"]
    for _, r in mom.iterrows():
        reasons = ", ".join(_row_reasons(r, "momentum"))
        growth_change = math.expm1(float(r["growth_pred_70d"]))
        lines.append(
            f"| `{r['name']}` | {r['momentum_score']:.0f} | "
            f"{growth_change:+.1%} | {reasons} |"
        )
    lines += ["", "## ⚠️ Rising dependency risk", "", "| Package | Risk | Level | Why |", "|---|---|---|---|"]
    for _, r in risk.iterrows():
        reasons = ", ".join(_row_reasons(r, "risk"))
        lines.append(f"| `{r['name']}` | {r['risk_score']:.0f} | {r['risk_level']} | {reasons} |")
    source_health = _source_health_markdown(engineering)
    if source_health:
        lines += ["", source_health]
    lines += ["", "_Generated by the OSS Radar agent crew._"]
    return "\n".join(lines)


def _risk_analyst(ctx: AgentContext, date_str: str, preds: pd.DataFrame,
                  model_metrics: dict, quality: dict, engineering: dict | None = None) -> str:
    template = _template_report(date_str, preds, model_metrics, quality, engineering)
    report = template
    if ctx.llm.available:
        mom_columns = [
            "name", "momentum_score", "growth_pred_70d",
            "momentum_reasons", "top_reasons",
        ]
        risk_columns = [
            "name", "risk_score", "risk_level", "risk_composite_score",
            "risk_classifier_probability", "risk_reasons", "top_reasons",
        ]
        mom = _movers(preds, "momentum_score")[
            [column for column in mom_columns if column in preds]
        ]
        risk = _movers(preds, "risk_score")[
            [column for column in risk_columns if column in preds]
        ]
        prompt = (
            f"Date: {date_str}. Tracked {len(preds)} packages.\n"
            f"Served growth model spearman={model_metrics.get('growth', {}).get('serving_spearman')}, "
            f"served risk model auc={model_metrics.get('risk', {}).get('serving_auc')}.\n\n"
            f"Top momentum movers (momentum_score 0-100, growth_pred_70d is forecast 70-day download momentum):\n"
            f"{mom.to_string(index=False)}\n\n"
            f"Top dependency-risk risers (risk_score 0-100):\n{risk.to_string(index=False)}\n\n"
            "Write the daily brief: a 2-3 sentence summary, then a 'Momentum' section and a "
            "'Dependency risk' section, each calling out the most notable 2-3 packages with the "
            "concrete reason. Keep it under 300 words. Markdown only."
        )
        llm_out = ctx.llm.generate(REPORT_SYSTEM, prompt)
        if llm_out:
            report = f"# OSS Radar — Daily Brief {date_str}\n\n{llm_out}\n"
            source_health = _source_health_markdown(engineering)
            if source_health:
                report += f"\n{source_health}\n"
            report += "\n_Generated by the OSS Radar agent crew._"
    src = "claude" if (ctx.llm.available and report is not template) else "template"
    ctx.record("RiskAnalyst", "write_daily_report", "ok",
               f"Wrote the daily brief {'with Claude-assisted prose' if src == 'claude' else 'from the standard template'}, "
               f"covering {count(len(preds))} packages.")
    return report


# --- MLOps: persist report + open the daily PR ---
def _mlops(ctx: AgentContext, date_str: str, report_md: str) -> str | None:
    path = Path("reports") / f"{date_str}.md"
    path.parent.mkdir(exist_ok=True)
    path.write_text(report_md)
    ctx.record("MLOps", "publish_report", "ok", f"Saved the daily report to {path}.", "")

    if ctx.dry_run or not ctx.settings.github_token:
        ctx.record("MLOps", "open_pull_request", "skipped",
                   "Skipped opening a pull request because this is a dry run or no GitHub token is set.")
        return None
    url = github_ops.open_daily_pr(
        ctx.settings.github_token, ctx.settings.github_repo,
        branch=f"oss-radar/daily-{date_str}", report_path=f"reports/{date_str}.md",
        report_md=report_md, title=f"OSS Radar daily brief — {date_str}",
        body=(f"Automated daily brief generated by the OSS Radar agent crew for {date_str}.\n\n"
              "This PR adds the day's report. Merging it keeps a public, versioned history of "
              "momentum/risk movers and what the agents did."),
    )
    ctx.record("MLOps", "open_pull_request", "ok" if url else "warning",
               "Opened a pull request with the daily report." if url
               else "Could not open the pull request: GitHub returned no URL.", url or "")
    return url


def run_crew(run_id: str, settings, llm, snapshots: pd.DataFrame, predictions: pd.DataFrame,
             model_metrics: dict, drift: dict | None = None, heal_stats: dict | None = None,
             train_df=None, active_download: list[str] | None = None, dry_run: bool = False) -> dict:
    ctx = AgentContext(run_id=run_id, settings=settings, llm=llm, dry_run=dry_run)
    date_str = datetime.now(UTC).date().isoformat()

    _healer(ctx, heal_stats)
    engineering = _data_engineer(ctx, snapshots)
    quality = _data_quality(ctx, snapshots)
    _data_scientist(ctx, model_metrics)
    _model_monitor(ctx, drift)
    run_improver(ctx, train_df, active_download or [])
    report_md = _risk_analyst(
        ctx,
        date_str,
        predictions,
        model_metrics,
        quality,
        engineering,
    )
    pr_url = _mlops(ctx, date_str, report_md)

    return {
        "activities": ctx.activities,
        "report_md": report_md,
        "pr_url": pr_url,
        "engineering": engineering,
        "quality": quality,
    }

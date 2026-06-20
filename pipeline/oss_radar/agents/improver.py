"""ImprovementScientist agent — proposes new model features and opens a PR when they help.

Closing the loop: the agent runs offline experiments over the candidate feature catalog,
and when a candidate measurably lifts the growth model's held-out Spearman it opens a PR
enabling it in ``active_features.json``. It only *proposes* — the change reaches the running
model only after CI, the PR-preview re-run, and a merge. Safe self-improvement by design.
"""

from __future__ import annotations

import json

import pandas as pd

from oss_radar.agents import github_ops
from oss_radar.agents.context import AgentContext
from oss_radar.config.active_features import with_candidate
from oss_radar.features import CANDIDATE_DOWNLOAD_FEATURES
from oss_radar.models.experiment import best_candidate, evaluate_candidates

AGENT = "ImprovementScientist"


def _experiment_table(results: list[dict]) -> str:
    rows = ["| candidate | base | new | Δ spearman |", "|---|--:|--:|--:|"]
    for r in results:
        d = f"{r['delta']:+}" if r["delta"] is not None else "n/a"
        rows.append(f"| `{r['candidate']}` | {r['base']} | {r['new']} | {d} |")
    return "\n".join(rows)


def run_improver(ctx: AgentContext, train_df: pd.DataFrame | None, active_download: list[str]) -> None:
    margin = ctx.settings.feature_lift_margin
    if train_df is None or len(train_df) < ctx.settings.min_train_rows:
        ctx.record(AGENT, "feature_experiment", "ok", "Insufficient data for feature experiments; skipped.")
        return

    candidates = [c for c in CANDIDATE_DOWNLOAD_FEATURES if c not in active_download]
    if not candidates:
        ctx.record(AGENT, "feature_experiment", "ok", "All candidate features already active.")
        return

    results = evaluate_candidates(train_df, active_download, candidates, seed=ctx.settings.random_seed)
    summary = ", ".join(
        f"{r['candidate']} Δ{r['delta']:+.3f}" if r["delta"] is not None else f"{r['candidate']} n/a"
        for r in results
    )
    ctx.record(AGENT, "feature_experiment", "ok",
               f"Tested {len(results)} candidate features against held-out Spearman: {summary}.")

    best = best_candidate(results, margin)
    if not best:
        ctx.record(AGENT, "propose_feature", "ok",
                   f"No candidate beat the +{margin:.3f} lift bar; active feature set unchanged.")
        return

    if ctx.dry_run or not ctx.settings.github_token:
        ctx.record(AGENT, "open_pull_request", "skipped",
                   f"Would propose enabling '{best['candidate']}' (Δspearman {best['delta']:+.3f}); "
                   "PR skipped (dry-run / no token).")
        return

    content = json.dumps(with_candidate(best["candidate"]), indent=2) + "\n"
    body = (
        f"The **ImprovementScientist** agent measured that adding the `{best['candidate']}` feature lifts the "
        f"growth model's held-out Spearman from **{best['base']:.3f} → {best['new']:.3f}** "
        f"(Δ {best['delta']:+.3f}).\n\nThis PR enables it in `active_features.json`. CI and the PR-preview bot "
        f"will re-run the pipeline on this branch so you can confirm the lift before merging.\n\n"
        f"### Experiment\n{_experiment_table(results)}\n\n"
        "<sub>Automated proposal by the OSS Radar self-improvement agent.</sub>"
    )
    url = github_ops.open_file_pr(
        ctx.settings.github_token, ctx.settings.github_repo,
        branch=f"oss-radar/feature-{best['candidate'].replace('_', '-')}",
        path="pipeline/oss_radar/config/active_features.json", content=content,
        title=f"Enable growth feature `{best['candidate']}` (Δspearman {best['delta']:+.3f})",
        body=body, labels=["oss-radar", "self-improvement", "model"],
    )
    ctx.record(AGENT, "open_pull_request", "ok" if url else "warning",
               (f"Proposed enabling '{best['candidate']}' (Δspearman {best['delta']:+.3f})."
                if url else "PR creation returned no URL."), url or "")

"""Render docs/sample-report.md from a local dry-run warehouse.

This produces the durable sample artifact linked from the README — top momentum
and risk movers, model metrics, per-source coverage, and what the agent crew did
on the run. It reads the latest run from the local DuckDB warehouse and contains
only model output (no tokens, no local paths, no transient URLs).

Refresh before a launch post:

    make demo                       # populates ./oss_radar.duckdb from a real run
    python scripts/demo_report.py   # regenerates docs/sample-report.md
"""

from __future__ import annotations

import json
from pathlib import Path

from oss_radar.warehouse import get_warehouse

OUT = Path("docs/sample-report.md")
TOP = 8
SOURCE_LABELS = {
    "pypi_downloads": "pypistats (downloads)",
    "pypi_metadata": "PyPI JSON (releases)",
    "ecosystems_pkg": "ecosyste.ms (package)",
    "ecosystems_repo": "ecosyste.ms (repo)",
    "depsdev": "deps.dev (+ Scorecard)",
    "osv": "OSV.dev (vulnerabilities)",
    "github": "GitHub REST (activity)",
}


def _reasons(val) -> str:
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except json.JSONDecodeError:
            return ""
    return ", ".join(val) if isinstance(val, list) else ""


def _fmt(x, spec: str = ".3f") -> str:
    try:
        f = float(x)
        return "n/a" if f != f else format(f, spec)
    except (TypeError, ValueError):
        return "n/a"


def main() -> None:
    wh = get_warehouse()
    preds = wh.query_df(
        "SELECT * FROM predictions WHERE run_id = "
        "(SELECT run_id FROM predictions ORDER BY predicted_at DESC LIMIT 1)"
    )
    if preds.empty:
        raise SystemExit("No predictions in the warehouse — run `make demo` first.")
    run_id = str(preds["run_id"].iloc[0])
    run_date = str(preds["predicted_at"].max())[:10]

    # --- champion model metrics for this run ---
    mr = wh.query_df(
        "SELECT model_name, metric_name, metric_value FROM model_runs "
        f"WHERE run_id = '{run_id}' AND is_champion = TRUE"
    )
    metrics: dict[str, dict[str, float]] = {}
    for _, r in mr.iterrows():
        metrics.setdefault(r["model_name"], {})[r["metric_name"]] = r["metric_value"]
    g, rk = metrics.get("growth", {}), metrics.get("risk", {})

    # --- per-source coverage from the latest snapshots ---
    snaps = wh.query_df(
        "SELECT source_status FROM snapshots WHERE run_id = "
        "(SELECT run_id FROM snapshots ORDER BY snapshot_date DESC LIMIT 1)"
    )
    cov: dict[str, int] = {}
    n_pkgs = 0
    for s in snaps["source_status"].dropna():
        d = json.loads(s) if isinstance(s, str) else s
        n_pkgs += 1
        for k, v in (d or {}).items():
            cov[k] = cov.get(k, 0) + (1 if v else 0)
    download_cov = (cov.get("pypi_downloads", 0) / n_pkgs * 100) if n_pkgs else 0.0

    # --- agent crew activity ---
    acts = wh.query_df(
        f"SELECT agent, action, status, summary FROM agent_activity "
        f"WHERE run_id = '{run_id}' ORDER BY ts"
    )

    mom = preds.sort_values("momentum_score", ascending=False).head(TOP)
    risk = preds.sort_values("risk_score", ascending=False).head(TOP)

    L: list[str] = [
        "# OSS Radar — sample output",
        "",
        f"> A real, unedited run of the full pipeline (ingest → features → train → score → agents) "
        f"on **{len(preds)} packages** against free public data sources, written to a local DuckDB "
        f"warehouse. Generated from run `{run_id}` ({run_date}). "
        "Refresh with `make demo && python scripts/demo_report.py`.",
        "",
        f"_Tracked {len(preds)} packages · growth model Spearman {_fmt(g.get('spearman'))} · "
        f"risk model AUC {_fmt(rk.get('auc'))} · download coverage {download_cov:.0f}%_",
        "",
        "## 🚀 Top momentum movers",
        "",
        "| Package | Momentum | Pred 7d growth | Why |",
        "|---|--:|--:|---|",
    ]
    for _, r in mom.iterrows():
        L.append(
            f"| `{r['name']}` | {r['momentum_score']:.0f} | {r['growth_pred_7d']:+.1%} "
            f"| {_reasons(r['top_reasons'])} |"
        )

    L += ["", "## ⚠️ Top rising dependency risk", "",
          "| Package | Risk | Level | Why |", "|---|--:|---|---|"]
    for _, r in risk.iterrows():
        L.append(
            f"| `{r['name']}` | {r['risk_score']:.0f} | {r['risk_level']} "
            f"| {_reasons(r['top_reasons'])} |"
        )

    L += ["", "## 📈 Model metrics (held-out)", "",
          f"- **Growth** (LightGBM regressor): Spearman {_fmt(g.get('spearman'))}, "
          f"MAE {_fmt(g.get('mae'))}, RMSE {_fmt(g.get('rmse'))}, R² {_fmt(g.get('r2'))} "
          f"· trained on {_fmt(g.get('n_train'), '.0f')} rows, tested on {_fmt(g.get('n_test'), '.0f')}.",
          f"- **Risk** (LightGBM classifier): ROC-AUC {_fmt(rk.get('auc'))} "
          f"on {_fmt(rk.get('n_samples'), '.0f')} packages ({_fmt(rk.get('n_positive'), '.0f')} at-risk).",
          "",
          "_Metrics are deliberately modest on this small watchlist and improve as daily snapshot "
          "history accumulates — they are tracked openly rather than hidden._",
          "",
          "## 🛰️ Source coverage", "",
          f"Share of the {n_pkgs} packages successfully ingested per free public source:",
          "", "| Source | Coverage |", "|---|--:|"]
    for key in SOURCE_LABELS:
        if key in cov:
            L.append(f"| {SOURCE_LABELS[key]} | {cov[key] / n_pkgs * 100:.0f}% |")

    L += ["", "## 🤖 What the agent crew did", "",
          "| Agent | Action | Status | Summary |", "|---|---|---|---|"]
    for _, r in acts.iterrows():
        summary = str(r["summary"]).replace("|", "\\|")
        L.append(f"| {r['agent']} | {r['action']} | {r['status']} | {summary} |")

    L += ["", "_Generated by the OSS Radar agent crew. With an Anthropic key the RiskAnalyst "
          "writes a prose brief; in template mode (shown here) it renders this structured report._", ""]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L))
    print(f"wrote {OUT} ({len(preds)} packages, run {run_id})")


if __name__ == "__main__":
    main()

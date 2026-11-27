"""Render a standalone evidence report from frozen offline benchmark outputs."""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

LABELS = {"seasonal_naive": "Seasonal naïve", "damped_ets": "Damped ETS", "pooled_lightgbm": "Pooled LightGBM"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    result = json.loads((args.results/"results.json").read_text())
    audit = json.loads((args.results/"data_audit.json").read_text())
    predictions = pd.read_csv(args.results/"predictions.csv")
    metrics = pd.read_csv(args.results/"metrics_by_period.csv")
    names = list(LABELS)
    aggregate = pd.DataFrame(result["aggregates"])
    final = aggregate.loc[aggregate.split == "final"].set_index("model").loc[names]
    ppm = predictions.loc[predictions.split == "final"].assign(
        absolute_pp=lambda d: (d.predicted_growth_percent-d.actual_growth_percent).abs()
    ).groupby("model").absolute_pp.median()
    colors = ["#2ee6c4", "#ffb347", "#5cc8ff"]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.facecolor": "#10171f", "figure.facecolor": "#080b10",
                         "text.color": "#e8f0f5", "axes.labelcolor": "#e8f0f5",
                         "xtick.color": "#a8bbc7", "ytick.color": "#e8f0f5",
                         "axes.edgecolor": "#26384a", "svg.fonttype": "none"})
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5), layout="constrained")
    values = [final.mae_log_growth.to_numpy(), final.mean_top_10_hits.to_numpy()]
    for ax, data, title, xlabel, limit in zip(axes, values,
            ["Growth-estimate error", "Actual movers found"],
            ["Mean absolute log-growth error · lower is better", "Mean hits in the actual top 10 · higher is better"],
            [.29, 10], strict=True):
        ax.barh(range(3), data, color=colors, height=.5)
        ax.set_yticks(range(3), LABELS.values())
        ax.invert_yaxis()
        ax.set_xlim(0, limit)
        ax.set_title(title, loc="left", fontsize=14, pad=16, weight="bold")
        ax.set_xlabel(xlabel, fontsize=10, labelpad=12)
        ax.spines[["top", "right"]].set_visible(False)
        for i, value in enumerate(data):
            ax.text(value+limit*.025, i, f"{value:.3f}" if limit < 1 else f"{value:.1f} / 10", va="center", fontsize=11)
    fig.suptitle("28-day download-growth benchmark · two final periods, 91 packages", fontsize=15, x=.02, ha="left")
    fig.savefig(args.out/"model-comparison.png", dpi=180, bbox_inches="tight")
    fig.savefig(args.out/"model-comparison.svg", bbox_inches="tight")
    plt.close(fig)
    svg = (args.out/"model-comparison.svg").read_text()
    svg = svg[svg.index("<svg"):].replace("<svg ", '<svg role="img" aria-label="Error and top-10 overlap for three models on two final periods" ', 1)
    table = "".join(f'<tr><th>{LABELS[n]}</th><td>{final.loc[n,"mae_log_growth"]:.3f}</td><td>{ppm[n]:.2f} pp</td><td>{final.loc[n,"mean_top_10_hits"]:.1f} / 10</td></tr>' for n in names)
    periods = "".join(f'<tr><td>{html.escape(r.cutoff)}</td><td>{LABELS[r.model]}</td><td>{r.mae_log_growth:.3f}</td><td>{r.top_10_hits} / 10</td></tr>' for r in metrics.loc[metrics.split == "final"].itertuples())
    interval = result["paired_comparison"]["package_cluster_95_percent_interval"]
    fallback = result["ets_fallbacks"]
    doc = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OSS Radar · Offline forecasting benchmark</title><style>
:root{color-scheme:dark}body{margin:0;background:#080b10;color:#e8f0f5;font:16px/1.65 system-ui,sans-serif}main{max-width:1100px;margin:auto;padding:32px 24px 60px}h1{font-size:34px;line-height:1.2;max-width:800px}h2{font-size:24px;margin-top:36px}h3{font-size:18px}p{max-width:78ch}.muted,small{color:#a8bbc7}.panel{background:#10171f;border:1px solid #26384a;border-radius:14px;padding:24px;margin:24px 0}.lead{font-size:21px}.scroll{overflow:auto}svg{max-width:100%;height:auto;min-width:680px}table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}th,td{text-align:left;border-bottom:1px solid #26384a;padding:12px 8px}a{color:#2ee6c4;text-underline-offset:4px}code{overflow-wrap:anywhere}summary{cursor:pointer}li{margin:8px 0}.two{display:grid;grid-template-columns:1fr 1fr;gap:32px}@media(max-width:700px){h1{font-size:28px}.two{grid-template-columns:1fr}.panel{padding:18px}}@media print{body{background:white;color:black}.panel{background:white;border-color:#777}.muted,small{color:#333}a{color:black}}
</style><main><p class="muted">OSS Radar · frozen offline experiment · data through October 30, 2026</p><h1>Repeating the recent weekly pattern beat both trained candidates.</h1><p class="lead">In this limited 28-day experiment, extra model complexity did not earn a production promotion.</p><p>The question: can we predict package download growth over the next 28 days better than a simple weekly-repeat forecast? Downloads measure activity, including automated installs; they do not establish adoption or unique users.</p><div class="panel"><h2 style="margin-top:0">The measured result</h2><div class="scroll">__CHART__</div><table><thead><tr><th>Method</th><th>Mean absolute log-growth error ↓</th><th>Median absolute growth error ↓</th><th>Actual top-10 overlap ↑</th></tr></thead><tbody>__TABLE__</tbody></table><p class="muted">182 package-period cases: 91 packages in each of two final windows. The median error is in percentage points of growth; the primary log error is not a percentage. Top-10 overlap is averaged across the two origins. No prediction intervals were fitted or validated.</p></div><h2>What this means</h2><p><strong>Seasonal naïve had the lowest magnitude error in both final periods.</strong> ETS had slightly better average top-10 overlap, illustrating why ranking and numerical accuracy must be assessed separately. That small ranking difference over two origins does not establish a better watchlist.</p><p>ETS narrowly beat seasonal naïve on development error (0.136640 vs 0.136861), so the frozen protocol selected ETS as the baseline before final comparison. On the final periods, LightGBM had 14.9% higher error than that selected baseline. The package-cluster interval for that paired error difference was __INTERVAL__, conditional on these two periods; it includes zero and does not estimate uncertainty across future periods.</p><p><strong>No production model was changed.</strong> This is a different target from the current 70-day, smoothed production model, so these numbers cannot be used to claim an improvement over that production model.</p><details><summary>Inspect the final-period results</summary><div class="scroll"><table><thead><tr><th>Last observed date</th><th>Method</th><th>Log-growth error</th><th>Top-10 hits</th></tr></thead><tbody>__PERIODS__</tbody></table></div><p class="muted">Final outcome windows: September 5–October 2 and October 3–30. Predictions were simulated at noon UTC on September 4 and October 2.</p></details><h2>Why these methods?</h2><div class="two"><div><h3>Seasonal naïve</h3><p>Repeats the last observed seven-day sequence. It asks whether recent weekly structure is already a useful forecast.</p><h3>Damped-trend ETS</h3><p>Uses the last 84 observations to estimate level, additive weekly seasonality, and a weakening trend. Forecasts are constrained to non-negative counts. There were __FALLBACKS__ failed-fit fallbacks out of 455 fits.</p></div><div><h3>Pooled LightGBM</h3><p>Uses ten predeclared, causal download features across packages. Its small tree structure and training budget were fixed before the first fit. There was no hyperparameter search, early stopping, target clipping, package ID, or final-period tuning.</p><h3>Same information and outcome</h3><p>All methods see the same observation cutoff and are scored against the same raw 28-day download totals, expressed as log growth relative to the last observed 28-day total.</p></div></div><h2>Evaluation controls</h2><ul><li>Protocol committed before the first benchmark fit, with a fingerprint of the frozen input CSV.</li><li>Three development periods; two final periods excluded from selection and tuning.</li><li>One-day availability lag: forecast issued the day after the last observation, with its outcome starting the following day. The unobserved issuance day is bridged, not included as a known input.</li><li>Every training outcome must end on or before the simulated information cutoff. Development outcomes complete before the first final cutoff.</li><li>Final outcome windows do not overlap each other. They may still be dependent; two origins are not two hundred independent experiments.</li><li>All 91 packages are scored for every method in every period. Missing data is never zero-filled. Difficult outcomes are retained.</li></ul><h2>Data audit and limits</h2><p>The export contains __ROWS__ rows: 91 packages × 277 calendar days, January 27–October 30. There are no duplicate package-days, missing days, nulls, negative values, or zero counts. This is structural completeness, not proof of data accuracy.</p><p>Recent source reconciliation checked vllm, numpy, and feast against pypistats without_mirrors: all 84 checked daily counts matched. This is the same upstream provider, not an independent validation. The exported history is not versioned by historical availability and may have been revised or backfilled; the one-day lag is a simulation assumption.</p><p>The same curated packages occur in training and evaluation. These findings do not establish performance on unseen packages, the full Python ecosystem, or future market conditions. A few extreme trend forecasts produced large ETS errors; no difficult package was removed to improve its score.</p><h2>Decision and next experiment</h2><p><strong>Do not promote either trained candidate from this result.</strong> Retain seasonal naïve as a prospective reference and freeze its inputs, baseline forecasts, and candidate forecasts before future outcomes begin. Longer reconciled history would provide more evaluation periods. Any revised model must be treated as a new experiment, with new untouched outcomes; these final periods have now been seen.</p><p>Published method evidence supports trying these families, not assuming they win on PyPI data: <a href="https://otexts.com/fpp3/toolbox.html">forecasting benchmarks and evaluation</a>, <a href="https://otexts.com/fpp3/exponential-smoothing.html">ETS</a>, <a href="https://doi.org/10.1016/j.ijforecast.2021.11.013">M5 LightGBM findings</a>, <a href="https://packaging.python.org/en/latest/guides/analyzing-pypi-package-downloads/">PyPI data caveats</a>.</p><details><summary>Reproducibility fingerprints</summary><p>Experiment commit: <code>386f177</code></p><p>Data SHA-256: <code>__DATAHASH__</code></p><p>Protocol SHA-256: <code>__PROTOCOLHASH__</code></p><p>Protocol, environment versions, per-period metrics, and every forecast are included with this report.</p></details></main></html>'''
    for token, value in {"__CHART__": svg, "__TABLE__": table, "__INTERVAL__": f"[{interval[0]:.3f}, {interval[1]:.3f}]",
                         "__PERIODS__": periods, "__FALLBACKS__": str(fallback), "__ROWS__": f"{audit['rows']:,}",
                         "__DATAHASH__": result["data_sha256"], "__PROTOCOLHASH__": result["protocol_sha256"]}.items():
        doc = doc.replace(token, value)
    (args.out/"benchmark-report.html").write_text(doc)
    print(args.out/"benchmark-report.html")


if __name__ == "__main__":
    main()

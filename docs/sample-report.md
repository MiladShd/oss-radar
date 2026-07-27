# OSS Radar — historical pre-operationalization production baseline

> **Baseline, not current evaluation evidence.** This capture predates the cohort-aware promotion and serving
> fixes now in the repository. In this older deployment, growth was compared with a best-ever value from a
> different moving cohort; risk `0.620` came from the retired row-random evaluation with first/latest
> variable-horizon labels; and the held risk candidate was still blended into predictions. The capture is retained
> to make that operational gap auditable. Refresh it after the operationalization release is deployed before using
> it as evidence of current model behavior.

> Captured from the public production APIs for run `20260726T093245Z` on 2026-07-26. This is an
> evidence snapshot of the real scheduled BigQuery/Cloud Run pipeline: 91 packages ingested, 91
> predictions written, and the run completed successfully in about 9 minutes 18 seconds.

_Tracked 91 packages · average momentum 54.7 · 16 rising · 3 high-risk · download coverage 100%_

## 🚀 Top momentum movers

| Package | Category | Momentum | Predicted 70d momentum | Why |
|---|---|--:|--:|---|
| `browser-use` | agents | 94.4 | +156.8% | 8-week trend rising; prior-8-week growth; recent vulnerabilities; key-person risk |
| `zenml` | MLOps | 83.6 | +72.1% | 8-week trend rising; steady downloads; recent vulnerabilities; weak security posture |
| `ibis-framework` | data engineering | 82.7 | +68.4% | strong 12-week base; monthly growth; release staleness; key-person risk |
| `anthropic` | LLM | 73.3 | +39.9% | strong weekly base; 12-week trend rising; key-person risk; weak security posture |
| `langsmith` | agents | 72.4 | +38.0% | strong weekly base; weaker prior-8-week comparison; recent vulnerabilities; key-person risk |
| `weaviate-client` | retrieval/vector DB | 72.2 | +37.3% | monthly growth; strong weekly base; key-person risk; weak security posture |
| `litellm` | LLM | 70.7 | +34.2% | 12-week trend rising; recent vulnerabilities; key-person risk |
| `huggingface-hub` | LLM | 70.3 | +33.2% | monthly growth; 12-week trend rising; key-person risk; weak security posture |

`growth_pred_70d` is log-growth. The percentages above use
`100 × expm1(growth_pred_70d)`; formatting the raw log value directly as a percent would understate the change.

## ⚠️ Highest dependency risk

| Package | Category | Risk | Level | Why |
|---|---|--:|---|---|
| `metagpt` | agents | 73.7 | high | 8-week trend falling; weekly slowdown; recent vulnerabilities; release staleness |
| `smolagents` | agents | 69.0 | high | 8-week and monthly downloads falling; recent vulnerabilities; issue pressure |
| `hnswlib` | retrieval/vector DB | 68.5 | high | small weekly base; weekly slowdown; release staleness; weak security posture |
| `litellm` | LLM | 65.8 | medium | recent vulnerabilities; key-person risk; small 12-week base |
| `agno` | agents | 64.6 | medium | 8-week and monthly downloads falling; recent vulnerabilities; key-person risk |
| `pyautogen` | agents | 64.3 | medium | 8-week and monthly downloads falling; release staleness; issue pressure |
| `mlflow` | MLOps | 64.1 | medium | 8-week trend falling; recent vulnerabilities; weak security posture |
| `tensorflow` | ML framework | 63.8 | medium | 8-week and monthly downloads falling; recent vulnerabilities; release staleness |

The “Why” text combines growth SHAP phrases and transparent composite-risk drivers. It does not explain the
classifier contribution that this historical build blended into `risk_score`.

## 📈 Model decision, not marketing

The older run did **not** report continuous improvement, but its comparison fields must be read as historical:

- **Growth challenger:** held-out Spearman `0.842`; package-disjoint Spearman `0.666` and R² `0.104`.
  The generalization gate rejected it. The displayed `0.891` “champion” value came from another moving cohort, so
  it is not a fair head-to-head comparison.
- **Risk challenger:** legacy row-random AUC `0.620` on 91 packages with 41 labels built from the old
  first/latest variable-horizon logic. Calling this package-disjoint or exact-14-day evaluation would be wrong.
  The recorded `0.760` belonged to that retired lineage, and this held candidate was nevertheless blended into
  the risk scores shown above.
- **Drift:** low for this run—momentum PSI `0.030`, risk PSI `0.037`, label churn `2.7%`.
- **Feature experiment:** none of the three candidates cleared the `+0.010` lift bar
  (`recent_share +0.000`, `dow_volatility_7 -0.011`, `trend_slope_7 -0.034`).

These numbers motivated the operationalization: the current code uses exact +14-day labels, package-disjoint OOF
Platt calibration, a stable untouched risk holdout, a `0.55` new-lineage AUC floor, current-cohort incumbent
re-scoring, versioned benchmark provenance, and serving that excludes held candidates. It also re-applies explicit
archived/removed and recent high/critical vulnerability safety floors after classifier blending, so the historical
risk table is not a preview of current scoring behavior. Elapsed calendar time alone is still not evidence of
better forecasting.

## 🛰️ Source coverage

Six public providers are surfaced as seven source-health checks because ecosyste.ms package and repository
metadata are checked separately.

| Signal | Coverage |
|---|--:|
| pypistats downloads | 100% |
| PyPI release metadata | 100% |
| ecosyste.ms package metadata | 100% |
| ecosyste.ms repository metadata | 93% |
| deps.dev + OpenSSF Scorecard | 100% |
| OSV vulnerabilities | 100% |
| GitHub activity | 100% |

## 🤖 Operations activity

| Component | Result |
|---|---|
| Healer | All core sources healthy; no retry or carry-forward required. |
| Data quality | 100% download coverage, zero duplicates; Scorecard 40% null and bus-factor 23% null. |
| Growth training | Challenger rejected by validation gate; prior registered champion retained. |
| Risk training | Legacy variable-horizon candidate was held, although this old build still blended it into scoring. |
| Drift monitor | Low band; no new investigation required. |
| Feature experiment | No candidate cleared the proposal threshold. |
| Risk analyst | Deterministic template brief generated for all 91 packages. |
| MLOps | Daily report written and PR #76 opened. |

## Provenance note

The deployed workload reported `git_sha: unknown` for this historical run; that was an operational gap, not hidden
from the sample. The operationalization release adds full-SHA image tags, workload-label provenance,
zero-traffic dashboard verification, and `/health` SHA checking. The full SHA is baked into each image and the
Cloud Run resources carry matching labels. Future captures can therefore tie the data run and serving revision to
an exact commit.

Refresh a local reproducible sample with:

```bash
make demo
.venv/bin/python scripts/demo_report.py
```

Or inspect the current production evidence through the public `/api/overview`, `/api/runs`, `/api/models`, and
`/api/agents` endpoints linked from the live dashboard.

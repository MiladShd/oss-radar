# Architecture

## Repository layout

```
oss-radar/
├── pipeline/oss_radar/
│   ├── config/          # settings (12-factor) + curated watchlist
│   ├── ingest/          # 6 source connectors + HTTP client + collector
│   ├── warehouse/       # backend-agnostic schema + DuckDB & BigQuery backends
│   ├── features/        # growth (time-series) + risk (cross-sectional) feature builders
│   ├── models/          # LightGBM growth + risk, SHAP scoring
│   ├── registry/        # champion/challenger promotion, GCS artifacts, MLflow
│   ├── agents/          # Claude wrapper + the 5-agent crew + GitHub ops
│   ├── orchestrator/    # the end-to-end daily pipeline
│   └── cli.py           # `oss-radar run | init-warehouse | info`
├── dashboard/app/       # FastAPI backend + single-file SPA
├── infra/               # Terraform + Cloud Build config
├── scripts/             # deploy.sh, pr_comment.py
└── .github/workflows/   # CI + PR-preview bot
```

## Design principles

**One image runs everywhere.** A single `OSS_RADAR_BACKEND` switch selects the warehouse — `duckdb` (a local
file, used by tests, local dev and the PR-preview bot) or `bigquery` (the Cloud Run job/service). The exact same
code path runs in all three.

**Connectors never raise.** Every `fetch(...)` returns a flat dict with an `_ok` flag and `None` for missing
fields. A down source degrades the snapshot, it doesn't crash the run. The shared HTTP client enforces a per-host
rate floor (with per-host locks so concurrent workers don't burst) and retries 429/403/5xx with exponential backoff.

**Source precedence is chosen from verified data quality** (see the comment block in `collector.py`): fresh
stars/forks from ecosyste.ms, velocity from GitHub, scorecard from deps.dev, reverse-deps from ecosyste.ms, vulns
from OSV. Each source was validated against real packages before a line of connector code was written.

**Portable warehouse.** Tables are defined once as `(name, type)` tuples and mapped to DuckDB / BigQuery types.
JSON columns are stored as serialized strings for portability. Queries stick to a portable SQL subset; date math
happens in pandas.

## Warehouse tables

| Table | Purpose |
|---|---|
| `snapshots` | point-in-time signals, one row per package per run (builds star/issue deltas over time) |
| `download_history` | 180-day daily download series (rebuilt each run; powers training labels + sparklines) |
| `features` | engineered package-day rows (scoring rows + labels) |
| `predictions` | momentum & risk scores + reasons, per run |
| `model_runs` | one row per metric per trained model — the model-improvement history |
| `agent_activity` | what each agent did, per run — the dashboard timeline |
| `pipeline_runs` | per-run status, stage durations, counts, git sha |

## Cloud topology

```
Cloud Scheduler (daily cron)
        │ POST jobs:run  (OAuth, scheduler SA → run.invoker)
        ▼
Cloud Run Job  "oss-radar-pipeline"   ── reads ──> 6 public APIs
   (2 vCPU / 4Gi, ML image)           ── writes ─> BigQuery + GCS (models)
                                       ── opens ──> GitHub PR / issue
        ▲ env from Secret Manager (GitHub token, Anthropic key)

Cloud Run Service "oss-radar-dashboard"  (public, scale-to-zero, slim image)
        └── reads BigQuery ──> FastAPI JSON API ──> single-file SPA
```

All durable infrastructure (BigQuery dataset, GCS bucket, service accounts + least-privilege IAM, both Cloud Run
resources, the scheduler, and IAM bindings) is declared in [`infra/terraform`](../infra/terraform). Images are built
for `linux/amd64` by Cloud Build; secrets are created out-of-band by `deploy.sh` so no secret material is ever in
Terraform state.

# Architecture

## Repository layout

```
oss-radar/
├── pipeline/oss_radar/
│   ├── config/          # settings (12-factor) + curated watchlist
│   ├── ingest/          # 6 source connectors + HTTP client + collector
│   ├── warehouse/       # backend-agnostic schema + DuckDB & BigQuery backends
│   ├── features/        # growth (time-series) + risk (cross-sectional) feature builders
│   ├── models/          # LightGBM growth + calibrated risk, SHAP/scoped scoring
│   ├── registry/        # cohort-aware promotion, durable GCS artifacts, local MLflow trace
│   ├── agents/          # optional Claude wrapper + 7-agent operations crew + GitHub ops
│   ├── orchestrator/    # the end-to-end daily pipeline
│   └── cli.py           # `oss-radar run | init-warehouse | info`
├── dashboard/app/       # FastAPI backend + single-file SPA
├── infra/               # Terraform + Cloud Build config
├── scripts/             # deploy, report preview, validation, and auto-triage helpers
└── .github/workflows/   # CI, CodeQL, PR preview, auto-triage, and keyless deployment
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

**Risk is learned and policy-bounded.** The risk classifier uses package-disjoint grouped OOF predictions for
Platt calibration, then evaluates calibrated probabilities on a stable reserved-package holdout. The 60/40
composite/classifier blend cannot cross below explicit archived/removed or recent high/critical vulnerability
safety floors.

## Warehouse tables

| Table | Purpose |
|---|---|
| `snapshots` | point-in-time signals, one row per package per run (builds star/issue deltas over time) |
| `download_history` | expanding daily download series, upserted by package/date from each API window (powers training labels + sparklines) |
| `features` | engineered package-day rows (scoring rows + labels) |
| `predictions` | momentum & risk scores + reasons, per run |
| `model_runs` | one row per metric per candidate/monitor — evaluation and selection history |
| `agent_activity` | what each agent did, per run — the dashboard timeline |
| `pipeline_runs` | per-run status, stage durations, counts, git sha |

## Cloud topology

```
Cloud Scheduler (daily cron)
        │ POST jobs:run  (OAuth, scheduler SA → run.invoker)
        ▼
Cloud Run Job  "oss-radar-pipeline"   ── reads ──> 6 public providers / 7 health checks
   (2 vCPU / 4Gi, ML image)           ── writes ─> BigQuery + GCS (models)
                                       ── opens ──> GitHub PR / issue
        ▲ env from Secret Manager (GitHub token, Anthropic key)

Cloud Run Service "oss-radar-dashboard"  (public, scale-to-zero, slim image)
        └── reads BigQuery ──> FastAPI JSON API ──> single-file SPA

Cloud Run Job "oss-radar-pipeline-smoke"  (release-only, DuckDB /tmp)
        └── no-role service account · no secrets · dry-run sample
```

Infrastructure shape (BigQuery dataset, GCS buckets, service accounts + least-privilege IAM, Cloud Run
resources, schedulers, and IAM bindings) is declared in [`infra/terraform`](../infra/terraform). Durable run/model
decisions live in BigQuery and promoted artifacts in GCS; Cloud MLflow uses local `file:` storage and is only a
best-effort ephemeral trace. Terraform uses a versioned, public-access-blocked GCS backend and manages the
immutable Artifact Registry repository. Pipeline and dashboard images are built independently for `linux/amd64`
by a dedicated Cloud Build service account, resolved to digests, and deployed by digest. Secret versions are
published out-of-band by `deploy.sh` so no secret material enters Terraform state: Terraform owns only the
protected Secret Manager containers/IAM, never versions or values. Both artifact and build-source buckets enforce
public-access prevention, `force_destroy = false`, and `prevent_destroy`.

All third-party GitHub Actions are pinned to exact commits. Workload Identity Federation is restricted to the
immutable repository and owner identities, `main`, and the exact `deploy.yml` `workflow_ref`. The bootstrap refuses
Terraform plans containing delete/replace actions. CD stages the dashboard at zero traffic and executes the pipeline
image in the isolated no-role smoke job before production update; failures restore the captured pipeline image and
dashboard revision. Terraform protects durable/control-plane resources from destroy and lifecycle-ignores CD-owned
release fields so a stale infrastructure apply cannot roll a release back.

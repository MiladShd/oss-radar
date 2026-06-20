# Contributing to OSS Radar

Thanks for your interest! OSS Radar runs on free public data, so you can develop the whole thing locally.

## Setup

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r pipeline/requirements.txt && pip install -e pipeline
pip install ruff pytest
```

## Develop

```bash
# fast local run (no cloud, no side effects)
OSS_RADAR_GITHUB_TOKEN=$(gh auth token) python -m oss_radar.cli run --dry-run --limit 12

# dashboard against the local DuckDB
pip install -r dashboard/requirements.txt
uvicorn dashboard.app.main:app --reload --port 8099
```

## Before you open a PR

```bash
pytest pipeline/tests -q
cd pipeline && ruff check oss_radar
```

CI runs the same checks. When you open the PR, the **PR-preview** workflow runs the pipeline on your branch and
comments the resulting momentum/risk movers — a quick way to see the effect of your change end-to-end.

## Good first contributions

- **Add packages** to the watchlist in [`pipeline/oss_radar/config/packages.py`](pipeline/oss_radar/config/packages.py).
- **New features** in [`pipeline/oss_radar/features/engineering.py`](pipeline/oss_radar/features/engineering.py)
  (add to `DOWNLOAD_FEATURES` / `RISK_FEATURES`).
- **New data source** — add a connector under `pipeline/oss_radar/ingest/` following the existing pattern
  (each `fetch(...)` returns a flat dict and never raises; it returns `{"_ok": False}` on failure).
- **Dashboard** improvements in [`dashboard/app/static/index.html`](dashboard/app/static/index.html).

## Conventions

- Connectors must degrade gracefully — a failed source returns partial data, never an exception.
- Keep warehouse SQL to the portable subset (it must run on both DuckDB and BigQuery); do heavier transforms in pandas.
- Match the surrounding code style; `ruff` is the source of truth.

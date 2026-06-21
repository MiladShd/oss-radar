# Deploying OSS Radar to GCP

## Prerequisites

- `gcloud` authenticated, a GCP project with billing, and `gh` authenticated (for the agent's GitHub PRs).
- `terraform >= 1.5` and Docker not required locally — images build on **Cloud Build**.
- These APIs enabled (the deploy assumes they are): `run`, `cloudscheduler`, `bigquery`, `artifactregistry`,
  `secretmanager`, `storage`, `cloudbuild`, `iam`, `cloudresourcemanager`.

## One command

```bash
echo "your-project-id" > .gcp_project          # or export OSS_RADAR_PROJECT=...
ANTHROPIC_API_KEY=sk-ant-...  ./scripts/deploy.sh
```

`scripts/deploy.sh`:

1. Ensures the **Artifact Registry** repo exists.
2. Creates/updates the **Secret Manager** secrets (`oss-radar-github-token` from `gh auth token`,
   `oss-radar-anthropic-key` from `$ANTHROPIC_API_KEY` — or a `DISABLED` sentinel for template-mode agents).
3. Builds both images on **Cloud Build** (`linux/amd64`) and pushes them to Artifact Registry.
4. Runs `terraform apply` to provision BigQuery, GCS, service accounts + IAM, both Cloud Run resources, and the
   daily Cloud Scheduler job.
5. Prints the public dashboard URL.

## First run

The scheduler triggers the pipeline daily. To populate data immediately:

```bash
gcloud run jobs execute oss-radar-pipeline --region us-central1 --wait
```

The dashboard is empty until the first run completes (it degrades gracefully and shows "run the pipeline to
populate").

## Enabling Claude agents later

If you deployed in template mode, switch the agents to Claude without a redeploy:

```bash
printf '%s' "sk-ant-..." | gcloud secrets versions add oss-radar-anthropic-key --data-file=-
```

The next run picks up the new secret version automatically.

## Cost

Designed to be cheap: Cloud Run **scales to zero**, the daily job runs a few minutes, and BigQuery storage for this
dataset is tiny. Expect a few dollars/month. The pipeline uses **free** data APIs — no BigQuery public-dataset scans
or paid quotas are required.

## Teardown

```bash
cd infra/terraform && terraform destroy   # removes all managed infra
# then, if you used a dedicated project:
gcloud projects delete "$(cat ../../.gcp_project)"
```

`force_destroy`/`delete_contents_on_destroy` are set on the bucket and dataset so teardown is clean.

## Configuration reference

All settings are env vars prefixed `OSS_RADAR_` (see `pipeline/oss_radar/config/settings.py`). For local
runs, copy [`.env.example`](../.env.example) to `.env` and edit; the table below is the cloud-relevant subset:

| Var | Default | Notes |
|---|---|---|
| `OSS_RADAR_BACKEND` | `duckdb` | `duckdb` \| `bigquery` |
| `OSS_RADAR_GCP_PROJECT` | discovered | required for BigQuery |
| `OSS_RADAR_BQ_DATASET` | `oss_radar` | |
| `OSS_RADAR_GITHUB_REPO` | `MiladShd/oss-radar` | where the agent opens PRs |
| `OSS_RADAR_GITHUB_TOKEN` | — | lifts GitHub limits; from Secret Manager in cloud |
| `OSS_RADAR_ANTHROPIC_API_KEY` | — | `sk-ant-...` enables Claude agents |
| `OSS_RADAR_WATCHLIST_LIMIT` | `0` (all) | cap packages for quick runs |

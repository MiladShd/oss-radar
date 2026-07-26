# Deploying OSS Radar to GCP

## Prerequisites

- `gcloud auth login` and `gcloud auth application-default login` completed for a principal allowed to enable
  APIs, create service accounts and Workload Identity Federation resources, and grant the IAM roles declared in
  Terraform. The second login supplies Application Default Credentials to the Terraform Google provider.
- A GCP project with billing, and `gh` authenticated with permission to write Actions repository variables and
  the agent's GitHub token secret.
- `terraform >= 1.5` and `jq`; Docker is not required locally because images build on **Cloud Build**.
- A clean Git working tree at a full lowercase 40-character commit SHA. Dirty or SHA-overridden production
  releases are unsupported. With the default deployment dispatch enabled, local HEAD must exactly equal remote
  `main`; use `DISPATCH_DEPLOY=0` only when performing infrastructure/build bootstrap before that commit is on
  `main`.

## One command

```bash
echo "your-project-id" > .gcp_project          # or export OSS_RADAR_PROJECT=...
./scripts/deploy.sh                            # template-mode reporting
```

For an optional AI-written brief, read the key without placing it in shell history:

```bash
read -rsp "Anthropic API key: " ANTHROPIC_API_KEY && echo
export ANTHROPIC_API_KEY
./scripts/deploy.sh
unset ANTHROPIC_API_KEY
```

`scripts/deploy.sh`:

1. Enables the required Google Cloud APIs.
2. Creates or hardens `<project>-oss-radar-tfstate` with uniform access, public-access prevention, and object
   versioning. Existing remote state is always authoritative; otherwise a validated local state is backed up and
   migrated interactively to `oss-radar/prod`. If neither state exists while production resources do, the script
   aborts for explicit recovery/import instead of assuming ownership.
3. Imports pre-existing Secret Manager containers and the `oss-radar` Artifact Registry repository when needed;
   Terraform thereafter manages those containers (never secret versions/values) and the repository with immutable
   tags and `prevent_destroy`.
4. Materializes every targeted/full Terraform plan and refuses any action containing `delete` (including a
   replacement) before applying it.
5. Provisions a dedicated `oss-radar-builder` identity plus a seven-day source-staging bucket, then independently
   builds or reuses the pipeline and dashboard full-SHA images. Component-wise builds make a partial upload safely
   resumable; only IAM-propagation permission failures receive bounded retries.
6. For a verified release (or a first greenfield stack), publishes **Secret Manager versions**
   (`oss-radar-github-token` from `gh auth token`,
   `oss-radar-anthropic-key` from `$ANTHROPIC_API_KEY`) without putting values in Terraform state. On the first
   template-mode deployment it creates a `DISABLED` sentinel; later runs with no key preserve the existing
   version. Existing-stack `DISPATCH_DEPLOY=0` bootstrap preserves every live secret version.
7. On an existing stack, applies only the deployment control plane first, leaving production runtimes untouched.
   A greenfield stack must be created in full before its first workflow can capture a release.
8. Writes the five required GitHub Actions variables.
9. Reuses an in-flight/successful exact-SHA deploy or dispatches `deploy.yml`, waits for it with
   `gh run watch --exit-status`, and verifies the workflow name, SHA, and conclusion.
10. Only after a successful release, applies the full protected Terraform plan and requires a final zero-drift
    plan. Set `DISPATCH_DEPLOY=0` to stop after control-plane/build bootstrap on an existing stack.
11. Prints the public dashboard URL.

The first authenticated run is the CD bootstrap. It must create the infrastructure/variables before
`.github/workflows/deploy.yml` can authenticate. The script waits for the verified canary/smoke/rollback result
and will not perform full reconciliation after a failed release. No service-account JSON key is created or stored
in GitHub. If the GitHub variables are managed elsewhere, use
`CONFIGURE_GITHUB_VARS=0` and set these repository variables yourself from Terraform's
`github_actions_variables` output:

| GitHub variable | Value |
|---|---|
| `GCP_PROJECT_ID` | GCP project id |
| `GCP_REGION` | Cloud Run and Artifact Registry region |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `terraform output -raw github_workload_identity_provider` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `terraform output -raw github_deploy_service_account` |
| `GCP_BUILD_SERVICE_ACCOUNT` | `terraform output -raw cloud_build_service_account` |

Workload Identity and IAM changes can take several minutes to propagate. If the first workflow run starts
immediately after bootstrap and authentication is denied, rerun it after propagation. Terraform state is remote
under `gs://<project>-oss-radar-tfstate/oss-radar/prod`, with bucket versioning, uniform access, and public-access
prevention. The backend bucket is intentionally bootstrapped outside Terraform so the stack cannot destroy its own
state store.

## Automatic deployments

Pushes to `main` trigger `.github/workflows/deploy.yml` only when `pipeline/**`, `dashboard/**`,
`infra/cloudbuild.yaml`, or the deployment workflow itself changes; it can also be dispatched manually. Terraform
changes do **not** auto-apply.

Every third-party Action is pinned to an exact commit SHA. The workflow exchanges GitHub's short-lived OIDC token
for a deployer identity restricted by repository name/id, owner id, `main`, and the exact
`deploy.yml@refs/heads/main` `workflow_ref`.

Pipeline and dashboard images build independently under the dedicated builder. Each build embeds the full SHA,
checks import/provenance inside the image, and publishes one immutable component tag. The workflow resolves each
tag to an `@sha256:` digest and deploys those digests. It captures the current production pipeline image/SHA,
any legacy runtime SHA override, and dashboard revision before making changes. Updates explicitly remove runtime
`GIT_SHA` so it cannot override image-baked provenance. The dashboard candidate receives zero production traffic
until `/healthz` reports the exact full SHA, `/api/system-health` returns a valid non-error contract, and the SPA
shell contains its required markers and passes a JavaScript syntax check.

Before production pipeline update, the workflow points the permanent `oss-radar-pipeline-smoke` job at the new
digest and executes it. That job uses a dedicated service account with no project roles, a DuckDB database under
`/tmp`, an eight-package dry run, no application secrets, dry-run GitHub behavior, and the optional LLM disabled.
Only after it succeeds is the production job updated and its digest/SHA label verified. Dashboard traffic is then
promoted and checked again, then the temporary `candidate` tag is removed. A failure or cancellation restores the
captured pipeline image/SHA, the prior runtime SHA override when one existed, previous dashboard revision and
service SHA label, and prior candidate-tag state, and reports rollback failure instead of silently ignoring it.

The deployer can submit Cloud Builds and update existing Cloud Run resources. It can act as the two production
runtime identities, dedicated builder, and no-role smoke identity, but cannot read application secrets or change
project IAM. Source uploads go to the dedicated `<project>-oss-radar-build-source` bucket; the builder can read
that bucket, write the Terraform-managed immutable Artifact Registry repository, and write build logs. It is not
the project's default Compute Engine or Cloud Build identity.

Terraform owns infrastructure shape and protects the durable/control-plane resources. Artifact Registry, BigQuery,
both Secret Manager containers, both data/source buckets, production job, isolated smoke job, and dashboard service
use `prevent_destroy`; Cloud Run deletion protection is enabled, the dataset does not delete contents, and both
buckets enforce public-access prevention with `force_destroy = false`. Release-time images and SHA labels are
lifecycle-ignored where CD owns them, preventing a later infrastructure apply with stale image variables from
rolling production backward. The bootstrap's plan gate adds another invariant: normal bootstrap/apply refuses
every delete or replacement.

## First run

The scheduler triggers the pipeline daily. To populate data immediately:

```bash
gcloud run jobs execute oss-radar-pipeline --region us-central1 --wait
```

The dashboard is empty until the first run completes. That state is explicit: the API and UI report that the
warehouse is not yet populated and point to the pipeline command instead of rendering an ambiguous blank screen.
Public responses use a 60-second cache, and package-detail warehouse reads are parameterized.

## Enabling Claude agents later

If you deployed in template mode, switch the agents to Claude without a redeploy:

```bash
read -rsp "Anthropic API key: " ANTHROPIC_API_KEY && echo
printf '%s' "$ANTHROPIC_API_KEY" \
  | gcloud secrets versions add oss-radar-anthropic-key --data-file=-
unset ANTHROPIC_API_KEY
```

The next run picks up the new secret version automatically.

## Cost

Designed to be cheap: Cloud Run **scales to zero**, the daily job runs a few minutes, and BigQuery storage for this
dataset is tiny. Expect a few dollars/month. The pipeline uses **free** data APIs — no BigQuery public-dataset scans
or paid quotas are required. Full-SHA image tags are intentionally immutable and are not automatically expired,
so Artifact Registry storage grows with releases. Review it periodically and remove only unreferenced digests
under a deliberate retention policy; preserving a small rollback window is part of the release design.

## Protected teardown

A routine `terraform destroy` is intentionally blocked. Durable data stores and Cloud Run resources carry
destruction protection, so an accidental command or mistaken plan cannot erase production data or services.
To retire an environment, first export/retain BigQuery and GCS data, review ownership and the complete plan, then
remove the relevant lifecycle/deletion protections in an explicit reviewed change. Delete retained data only as a
separate deliberate action. Project deletion is an additional irreversible operation and is not part of the
normal deployment script.

For release rollback, do not tear down infrastructure. Use the previous dashboard revision and immutable pipeline
digest as documented in [OPERATIONS.md](OPERATIONS.md).

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
| `OSS_RADAR_ANTHROPIC_API_KEY` | — | configured key enables AI-written briefs; absent/disabled uses templates |
| `OSS_RADAR_WATCHLIST_LIMIT` | `0` (all) | cap packages for quick runs |

See [OPERATIONS.md](OPERATIONS.md) for deployment verification, GitHub backlog cleanup, rollback, and
incident-response commands.

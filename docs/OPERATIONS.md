# OSS Radar operations runbook

This is the handoff for running OSS Radar as a service. It separates local verification, one-time
administrative changes, routine operation, and recovery. Commands that mutate GitHub or GCP are explicitly
identified; nothing in the repository setup silently applies them.

## 1. Release preflight

Run these from the repository root before merging an operational change:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
.venv/bin/python -m pip install \
  -r pipeline/requirements.txt -r dashboard/requirements.txt -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e pipeline

make test
.venv/bin/python -m ruff check pipeline/oss_radar pipeline/tests dashboard scripts
actionlint
shellcheck scripts/*.sh
pip-audit -r pipeline/requirements.txt
pip-audit -r dashboard/requirements.txt
terraform -chdir=infra/terraform fmt -check -diff
terraform -chdir=infra/terraform init -backend=false -input=false
terraform -chdir=infra/terraform validate
```

CI repeats the dependency audits, workflow/shell lint, Terraform initialization/validation, leak-gate tests,
pipeline tests, and dashboard tests. A release is not ready because only the model tests pass; automation syntax
and the dashboard health contract are part of the release.

## 2. One-time GitHub activation

Required signed commits currently block the unsigned commits on bot-owned report branches. Keep signed commits
enabled and install the narrower pull-request-only GitHub Actions bypass:

```bash
# Read-only audit: saves a local snapshot and prints the proposed bypass-only diff.
./scripts/configure_github_rules.sh

# External GitHub write: apply only after reviewing that diff.
./scripts/configure_github_rules.sh --apply
```

Verify the invariant:

```bash
gh api repos/MiladShd/oss-radar/rulesets/17938598 \
  --jq '{
    enforcement,
    signatures: any(.rules[]; .type == "required_signatures"),
    actions_bypass: [
      .bypass_actors[]
      | select(.actor_type == "Integration" and .actor_id == 15368)
      | .bypass_mode
    ]
  }'
```

Expected: active enforcement, `signatures: true`, and exactly one `pull_request` bypass.

After the operationalization change itself is on `main`, dispatch the checked-in maintainer:

```bash
# External GitHub write: starts the workflow; the script performs its own strict allowlist checks.
gh workflow run auto-triage.yml --repo MiladShd/oss-radar --ref main
gh run list --repo MiladShd/oss-radar --workflow=auto-triage.yml --limit=3
gh run watch RUN_ID --repo MiladShd/oss-radar --exit-status
```

As observed on 2026-07-26, the cleanup target is 32 green daily-report PRs and 10 exact duplicate drift issues.
The maintainer lands the reports oldest-first, retains issue #27 as the canonical drift thread, and closes the
other nine with backlinks. It deliberately does **not** merge failed feature PRs #29 or #37. Once this release is
on `main`, close those stale proposals so a later qualifying experiment can recreate a clean branch:

```bash
# External GitHub writes.
gh pr close 29 --repo MiladShd/oss-radar --delete-branch \
  --comment "Closing the stale failed proposal; the refreshed experiment path can recreate it from current main."
gh pr close 37 --repo MiladShd/oss-radar --delete-branch \
  --comment "Closing the stale failed proposal; the refreshed experiment path can recreate it from current main."
```

The latest production run on 2026-07-26 recorded low drift (momentum PSI 0.030, risk PSI 0.037, label churn
2.7%). After deduplication, either let the next low-drift pipeline run close the canonical issue automatically or
close it with that evidence:

```bash
# Optional external GitHub write after confirming the latest run is still in the low band.
gh issue close 27 --repo MiladShd/oss-radar --reason completed \
  --comment "Latest production drift is back in the low band; closing the consolidated investigation."
```

## 3. One-time GCP/CD bootstrap

Authenticate both the CLI and Terraform provider, then deploy from a clean commit:

```bash
gcloud auth login
gcloud auth application-default login
echo "your-project-id" > .gcp_project
./scripts/deploy.sh
```

The script creates/hardens versioned remote Terraform state, treats an existing remote object as authoritative,
and backs up plus interactively migrates a validated local state only when remote state is absent. It refuses to
bootstrap over existing runtimes when no authoritative state exists. It imports or creates the Terraform-managed
immutable Artifact Registry repository, rejects delete/replace plans, provisions the runtime stack plus isolated no-role smoke
job, builds each component independently, and writes the five required repository variables. Terraform owns Secret
Manager **containers**, never versions/values; verified release/greenfield runs publish versions after validation,
while existing-stack `DISPATCH_DEPLOY=0` preserves all live versions. Template-mode reporting is the default. If
an AI-written brief is required, read the key securely as shown in
[DEPLOY.md](DEPLOY.md). By default the script reuses or dispatches `deploy.yml` for the exact remote `main`
commit, waits for that exact run to pass, performs the full post-release reconciliation, and requires zero
Terraform drift. `DISPATCH_DEPLOY=0` stops after control-plane/build bootstrap for an existing stack without
changing its runtime images, traffic, or effective secrets.

The deploy workflow stages a dashboard revision at zero production traffic, checks its SHA, system-health query,
and SPA shell through the tagged candidate URL. It then executes the pipeline digest in
`oss-radar-pipeline-smoke`: an eight-package DuckDB dry run under a dedicated service account with no project roles,
no secrets, and the optional LLM disabled. Only a passing smoke reaches the production job and dashboard traffic.
A failure or cancellation restores the captured pipeline image/SHA and runtime override, previous dashboard
revision/service SHA label, and prior candidate-tag state. A successful release removes the temporary tag.

## 4. Verify a release

```bash
project="$(cat .gcp_project)"
region="us-central1"
dashboard_url="$(
  gcloud run services describe oss-radar-dashboard \
    --project="$project" --region="$region" --format='value(status.url)'
)"

curl --fail --silent --show-error "$dashboard_url/healthz" | jq .
curl --fail --silent --show-error "$dashboard_url/api/system-health" | jq .

gcloud run jobs describe oss-radar-pipeline \
  --project="$project" --region="$region" \
  --format='yaml(metadata.labels.git_sha,spec.template.spec.template.spec.containers[0].image)'

gcloud run jobs describe oss-radar-pipeline-smoke \
  --project="$project" --region="$region" \
  --format='yaml(spec.template.spec.template.spec.serviceAccount,spec.template.spec.template.spec.containers[0].image)'
```

`/healthz` must return `status: ok` and the expected full Git SHA. `/api/system-health` must return successfully
even for a first-run warehouse. The dashboard has an explicit first-run state, parameterized package-detail
queries, a 60-second response cache, and a two-instance maximum.

Populate a new environment or smoke-test the pipeline job:

```bash
gcloud run jobs execute oss-radar-pipeline \
  --project="$project" --region="$region" --wait
```

Then confirm the latest `/api/runs` record is successful and reports the deployed SHA.

## 5. Rollback

Dashboard rollback changes traffic only; it does not rebuild an image:

```bash
gcloud run revisions list --service=oss-radar-dashboard \
  --project="$project" --region="$region"
gcloud run services update-traffic oss-radar-dashboard \
  --project="$project" --region="$region" \
  --to-revisions="PREVIOUS_REVISION=100"
```

Pipeline rollback points the job at a known immutable digest. The image already contains its build SHA:

```bash
gcloud run jobs update oss-radar-pipeline \
  --project="$project" --region="$region" \
  --image="$region-docker.pkg.dev/$project/oss-radar/pipeline@sha256:KNOWN_GOOD_DIGEST" \
  --update-labels="git_sha=KNOWN_GOOD_GIT_SHA"
```

Record why the rollback occurred and preserve the failing SHA. Do not retag `latest` and call that provenance.

## 6. Routine checks

- Daily: latest pipeline run succeeded; prediction count matches the configured watchlist count; source coverage
  and drift are plausible.
- Weekly: no growing report-PR backlog, no duplicate drift issues, deploy workflow green, CodeQL baseline clean.
- Monthly: review Artifact Registry storage. Full-SHA tags are intentionally immutable and retained so rollback
  provenance stays auditable; delete only superseded, unreferenced digests under an explicit retention decision.
- On model promotion: inspect both the promotion metric and package-disjoint gate metrics; do not infer temporal
  improvement until a production cohort's 70-day outcome is closed.
- On schema/dashboard change: run both backends' tests and the API smoke tests; parameterized query behavior and
  first-run responses are release contracts.

Changes under `infra/terraform/**` require an explicit authenticated plan/apply; a push does not auto-apply
infrastructure. Prefer `DISPATCH_DEPLOY=0 ./scripts/deploy.sh` for the protected pre-main bootstrap path on an
existing stack: it initializes the remote backend, saves plans, and refuses delete/replacement actions without
updating production runtimes. The deploy workflow owns image digests, runtime
provenance, and dashboard traffic after bootstrap, while Terraform lifecycle rules ignore those release fields and
protect Artifact Registry, both data buckets, BigQuery, and Cloud Run resources. The artifact/model and build-source
buckets both enforce public-access prevention, `force_destroy = false`, and `prevent_destroy`.

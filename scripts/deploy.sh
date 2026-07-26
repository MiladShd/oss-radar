#!/usr/bin/env bash
# OSS Radar — bootstrap protected infrastructure, build immutable images, execute the
# verified GitHub deployment workflow, then reconcile the full Terraform contract.
#
#   ./scripts/deploy.sh
#   DISPATCH_DEPLOY=0 ./scripts/deploy.sh     # CD bootstrap/build only (no production change)
#   CONFIGURE_GITHUB_VARS=0 ./scripts/deploy.sh
#
# ANTHROPIC_API_KEY is optional. Omit it for deterministic template-mode reporting.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."

for command_name in git gh gcloud jq mktemp terraform; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command not found: $command_name" >&2
    exit 1
  }
done

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/oss-radar-deploy.XXXXXX")"
cleanup() {
  rm -rf "$work_dir"
}
trap cleanup EXIT

PROJECT="${OSS_RADAR_PROJECT:-}"
if [[ -z "$PROJECT" ]]; then
  if [[ ! -f .gcp_project ]]; then
    echo "Set OSS_RADAR_PROJECT or create .gcp_project first." >&2
    exit 1
  fi
  PROJECT="$(<.gcp_project)"
fi

REGION="${REGION:-us-central1}"
GITHUB_REPO="${OSS_RADAR_GITHUB_REPO:-MiladShd/oss-radar}"
GITHUB_REPOSITORY_ID="${OSS_RADAR_GITHUB_REPOSITORY_ID:-1274922810}"
GITHUB_REPOSITORY_OWNER_ID="${OSS_RADAR_GITHUB_REPOSITORY_OWNER_ID:-14307102}"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/oss-radar"
BUILD_SOURCE_BUCKET="${PROJECT}-oss-radar-build-source"
STATE_BUCKET="${PROJECT}-oss-radar-tfstate"
STATE_PREFIX="oss-radar/prod"
REMOTE_STATE_URI="gs://${STATE_BUCKET}/${STATE_PREFIX}/default.tfstate"
LOCAL_STATE="infra/terraform/terraform.tfstate"
STATE_BACKUP_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/oss-radar/terraform"
BUILD_SERVICE_ACCOUNT="projects/${PROJECT}/serviceAccounts/oss-radar-builder@${PROJECT}.iam.gserviceaccount.com"
GH_SECRET="oss-radar-github-token"
ANT_SECRET="oss-radar-anthropic-key"
HEAD_SHA="$(git rev-parse HEAD)"
GIT_SHA_OVERRIDE="${GIT_SHA:-}"
DISPATCH_DEPLOY="${DISPATCH_DEPLOY:-1}"

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "Refusing to build or provision from a dirty worktree." >&2
  echo "Commit the exact release bytes first; dirty production releases are unsupported." >&2
  exit 1
fi
if [[ -n "$GIT_SHA_OVERRIDE" && "$GIT_SHA_OVERRIDE" != "$HEAD_SHA" ]]; then
  echo "GIT_SHA must match the clean checked-out HEAD." >&2
  exit 1
fi
GIT_SHA="$HEAD_SHA"
TAG="$GIT_SHA"

if [[ ! "$GIT_SHA" =~ ^[a-f0-9]{40}$ ]]; then
  echo "Expected a full lowercase 40-character Git commit SHA." >&2
  exit 1
fi
if [[ "$DISPATCH_DEPLOY" != "0" && "$DISPATCH_DEPLOY" != "1" ]]; then
  echo "DISPATCH_DEPLOY must be 0 or 1." >&2
  exit 1
fi

echo "==> Project: $PROJECT  Region: $REGION  Commit: $GIT_SHA"

echo "==> GitHub credential and release preflight"
GITHUB_OWNER="${GITHUB_REPO%%/*}"
GH_LOGIN="$(gh api user --jq .login)"
GH_PERMISSION="$(gh repo view "$GITHUB_REPO" --json viewerPermission --jq .viewerPermission)"
GH_TOKEN_VALUE="$(gh auth token)"
if [[ "${GH_LOGIN,,}" != "${GITHUB_OWNER,,}" ]]; then
  echo "Authenticated GitHub user ${GH_LOGIN} does not match repository owner ${GITHUB_OWNER}." >&2
  exit 1
fi
if [[ "$GH_PERMISSION" != "ADMIN" || -z "$GH_TOKEN_VALUE" ]]; then
  echo "The authenticated GitHub credential must have ADMIN access to ${GITHUB_REPO}." >&2
  exit 1
fi
if [[ "$DISPATCH_DEPLOY" == "1" ]]; then
  REMOTE_MAIN_SHA="$(gh api "repos/${GITHUB_REPO}/commits/main" --jq .sha)"
  if [[ "$REMOTE_MAIN_SHA" != "$HEAD_SHA" ]]; then
    echo "Verified deployment can only be dispatched for the exact remote main commit." >&2
    echo "Local HEAD: $HEAD_SHA  Remote main: $REMOTE_MAIN_SHA" >&2
    echo "Use DISPATCH_DEPLOY=0 for an infrastructure-only bootstrap." >&2
    exit 1
  fi
fi

echo "==> Google Cloud credential preflight"
if ! gcloud auth application-default print-access-token >/dev/null 2>&1; then
  echo "Terraform requires Application Default Credentials for its GCS backend and provider." >&2
  echo "Run: gcloud auth application-default login" >&2
  exit 1
fi

echo "==> Required Google Cloud APIs"
gcloud services enable \
  artifactregistry.googleapis.com \
  bigquery.googleapis.com \
  cloudbuild.googleapis.com \
  cloudresourcemanager.googleapis.com \
  cloudscheduler.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  sts.googleapis.com \
  storage.googleapis.com \
  --project="$PROJECT"

echo "==> Versioned, access-restricted Terraform state bucket"
if ! gcloud storage buckets describe "gs://${STATE_BUCKET}" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${STATE_BUCKET}" \
    --project="$PROJECT" \
    --location="$REGION" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi
gcloud storage buckets update "gs://${STATE_BUCKET}" \
  --project="$PROJECT" \
  --uniform-bucket-level-access \
  --public-access-prevention \
  --versioning >/dev/null

echo "==> Secret containers"
ensure_secret() {
  gcloud secrets describe "$1" --project="$PROJECT" >/dev/null 2>&1 \
    || gcloud secrets create "$1" --replication-policy=automatic --project="$PROJECT"
}
ensure_secret "$GH_SECRET"
ensure_secret "$ANT_SECRET"

TF_VARS=(
  "-var=project=${PROJECT}"
  "-var=region=${REGION}"
  "-var=pipeline_image=${REPO}/pipeline:${TAG}"
  "-var=dashboard_image=${REPO}/dashboard:${TAG}"
  "-var=git_sha=${GIT_SHA}"
  "-var=github_repo=${GITHUB_REPO}"
  "-var=github_repository_id=${GITHUB_REPOSITORY_ID}"
  "-var=github_repository_owner_id=${GITHUB_REPOSITORY_OWNER_ID}"
)

validate_state_file() {
  local state_path="$1"
  local require_resources="${2:-0}"
  local foreign_projects
  jq -e '
    .version == 4
    and (.lineage | type == "string")
    and (.serial | type == "number")
    and (.resources | type == "array")
  ' "$state_path" >/dev/null || {
    echo "Invalid Terraform state structure: $state_path" >&2
    exit 1
  }
  if [[ "$require_resources" == "1" ]] \
    && ! jq -e '(.resources | length) > 0' "$state_path" >/dev/null; then
    echo "Refusing to migrate an empty local Terraform state: $state_path" >&2
    exit 1
  fi
  foreign_projects="$(
    jq -r --arg project "$PROJECT" '
      [.resources[]?.instances[]?.attributes.project? // empty]
      | unique
      | map(select(. != $project))
      | .[]?
    ' "$state_path"
  )"
  if [[ -n "$foreign_projects" ]]; then
    echo "Terraform state contains resources from a different project:" >&2
    echo "$foreign_projects" >&2
    exit 1
  fi
}

backup_local_state() {
  [[ -s "$LOCAL_STATE" ]] || return 0
  mkdir -p "$STATE_BACKUP_DIR"
  local stamp backup_path
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_path="${STATE_BACKUP_DIR}/terraform-${stamp}-$$.tfstate"
  cp "$LOCAL_STATE" "$backup_path"
  if [[ -s "${LOCAL_STATE}.backup" ]]; then
    cp "${LOCAL_STATE}.backup" "${backup_path}.previous"
  fi
  echo "    Preserved local state backup: ${backup_path}"
}

echo "==> Select authoritative Terraform state"
remote_state_exists=false
if gcloud storage objects describe "$REMOTE_STATE_URI" \
  --project="$PROJECT" >/dev/null 2>&1; then
  remote_state_exists=true
fi

if [[ "$remote_state_exists" == "true" ]]; then
  # A stale ignored local state must never overwrite an already-authoritative remote state.
  backup_local_state
  terraform -chdir=infra/terraform init -input=false -reconfigure \
    -backend-config="bucket=${STATE_BUCKET}"
  terraform -chdir=infra/terraform state pull >"$work_dir/remote-state.json"
  validate_state_file "$work_dir/remote-state.json"
  echo "    Using existing versioned GCS state."
elif [[ -s "$LOCAL_STATE" ]] && jq -e '(.resources | length) > 0' "$LOCAL_STATE" >/dev/null; then
  validate_state_file "$LOCAL_STATE" 1
  local_lineage="$(jq -r .lineage "$LOCAL_STATE")"
  local_resource_count="$(jq -r '.resources | length' "$LOCAL_STATE")"
  backup_local_state
  if [[ ! -t 0 ]]; then
    echo "First-time state migration requires an interactive terminal confirmation." >&2
    echo "Re-run this command in a terminal; the reviewed local state will be copied to GCS." >&2
    exit 1
  fi
  echo "    One-time migration: ${local_resource_count} resources, lineage ${local_lineage}."
  terraform -chdir=infra/terraform init -input=true -migrate-state \
    -backend-config="bucket=${STATE_BUCKET}"
  gcloud storage objects describe "$REMOTE_STATE_URI" \
    --project="$PROJECT" >/dev/null
  terraform -chdir=infra/terraform state pull >"$work_dir/migrated-state.json"
  validate_state_file "$work_dir/migrated-state.json" 1
  [[ "$(jq -r .lineage "$work_dir/migrated-state.json")" == "$local_lineage" ]] \
    || {
      echo "Migrated state lineage does not match the reviewed local state." >&2
      exit 1
    }
  [[ "$(jq -r '.resources | length' "$work_dir/migrated-state.json")" == "$local_resource_count" ]] \
    || {
      echo "Migrated state resource count does not match the reviewed local state." >&2
      exit 1
    }
  echo "    Migration verified in versioned GCS state."
else
  # Empty state is safe only for a genuinely new stack. Existing production resources require
  # explicit recovery/import, not an optimistic create that could collide or assume ownership.
  if gcloud run jobs describe oss-radar-pipeline \
    --project="$PROJECT" --region="$REGION" >/dev/null 2>&1 \
    || gcloud run services describe oss-radar-dashboard \
      --project="$PROJECT" --region="$REGION" >/dev/null 2>&1; then
    echo "No authoritative Terraform state exists, but OSS Radar production resources do." >&2
    echo "Recover or import the state explicitly before deployment." >&2
    exit 1
  fi
  backup_local_state
  terraform -chdir=infra/terraform init -input=false -reconfigure \
    -backend-config="bucket=${STATE_BUCKET}"
  echo "    Initialized a new empty GCS state for a greenfield stack."
fi

# Import pre-existing secret containers without ever importing or exposing secret versions.
for secret_spec in \
  "google_secret_manager_secret.github:${GH_SECRET}" \
  "google_secret_manager_secret.anthropic:${ANT_SECRET}"; do
  secret_resource="${secret_spec%%:*}"
  secret_name="${secret_spec#*:}"
  if ! terraform -chdir=infra/terraform state show "$secret_resource" >/dev/null 2>&1; then
    terraform -chdir=infra/terraform import -input=false "${TF_VARS[@]}" \
      "$secret_resource" "projects/${PROJECT}/secrets/${secret_name}"
  fi
done

# Import the pre-operationalization repository once; new projects let Terraform create it.
if gcloud artifacts repositories describe oss-radar \
  --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
  && ! terraform -chdir=infra/terraform state show \
    google_artifact_registry_repository.images >/dev/null 2>&1; then
  terraform -chdir=infra/terraform import -input=false "${TF_VARS[@]}" \
    google_artifact_registry_repository.images \
    "projects/${PROJECT}/locations/${REGION}/repositories/oss-radar"
fi

pipeline_in_state=false
dashboard_in_state=false
terraform -chdir=infra/terraform state show \
  google_cloud_run_v2_job.pipeline >/dev/null 2>&1 && pipeline_in_state=true
terraform -chdir=infra/terraform state show \
  google_cloud_run_v2_service.dashboard >/dev/null 2>&1 && dashboard_in_state=true
if [[ "$pipeline_in_state" == "true" && "$dashboard_in_state" == "true" ]]; then
  stack_mode="existing"
elif [[ "$pipeline_in_state" == "false" && "$dashboard_in_state" == "false" ]]; then
  stack_mode="greenfield"
else
  echo "Terraform state contains only one production runtime; refusing a partial-stack deploy." >&2
  exit 1
fi
echo "    Stack mode: ${stack_mode}"

assert_plan_has_no_delete() {
  local plan_path="$1"
  if ! terraform -chdir=infra/terraform show -json "$plan_path" \
    | jq -e '
        [.resource_changes[]?
          | select(any(.change.actions[]?; . == "delete"))]
        | length == 0
      ' >/dev/null; then
    echo "Refusing a Terraform plan containing delete/replace actions: $plan_path" >&2
    echo "Review and perform destructive changes through a separate, explicit process." >&2
    exit 1
  fi
}

plan_and_apply() {
  local plan_path="$1"
  shift
  terraform -chdir=infra/terraform plan -input=false -out="$plan_path" "${TF_VARS[@]}" "$@"
  assert_plan_has_no_delete "$plan_path"
  terraform -chdir=infra/terraform apply -input=false -auto-approve "$plan_path"
}

echo "==> Immutable repository and dedicated Cloud Build bootstrap"
bootstrap_plan="${work_dir}/bootstrap.tfplan"
plan_and_apply "$bootstrap_plan" \
  -target=google_artifact_registry_repository.images \
  -target=google_service_account.builder \
  -target=google_storage_bucket.build_source \
  -target=google_storage_bucket_iam_member.builder_source_reader \
  -target=google_artifact_registry_repository_iam_member.builder_artifact_writer \
  -target=google_project_iam_member.builder_log_writer

echo "==> Build or reuse each immutable image independently"
build_component() {
  local component="$1"
  local image="${REPO}/${component}:${TAG}"
  local build_log="${work_dir}/cloud-build-${component}.log"
  local attempt
  local delay
  if gcloud artifacts docker images describe "$image" --project="$PROJECT" >/dev/null 2>&1; then
    echo "    Reusing ${component}:${TAG}."
    return
  fi
  for attempt in 1 2 3 4 5; do
    : >"$build_log"
    if gcloud builds submit \
      --project="$PROJECT" \
      --config=infra/cloudbuild.yaml \
      --service-account="$BUILD_SERVICE_ACCOUNT" \
      --gcs-source-staging-dir="gs://${BUILD_SOURCE_BUCKET}/source" \
      --substitutions="_REPO=${REPO},_TAG=${TAG},_COMPONENT=${component}" \
      . 2>&1 | tee "$build_log"; then
      return
    fi
    if [[ "$attempt" == "5" ]] \
      || ! grep -Eqi \
        'PERMISSION_DENIED|permission denied|does not have permission|iam\.serviceAccounts\.actAs' \
        "$build_log"; then
      echo "Cloud Build failed for ${component}; refusing a non-IAM retry." >&2
      return 1
    fi
    delay=$((attempt * 10))
    echo "    Builder IAM may still be propagating; retrying ${component} in ${delay}s." >&2
    sleep "$delay"
  done
}
build_component pipeline
build_component dashboard

if [[ "$DISPATCH_DEPLOY" == "1" || "$stack_mode" == "greenfield" ]]; then
  echo "==> Publish validated secret versions"
  printf '%s' "$GH_TOKEN_VALUE" \
    | gcloud secrets versions add "$GH_SECRET" --data-file=- --project="$PROJECT"
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    printf '%s' "$ANTHROPIC_API_KEY" \
      | gcloud secrets versions add "$ANT_SECRET" --data-file=- --project="$PROJECT"
  elif [[ -z "$(
    gcloud secrets versions list "$ANT_SECRET" --project="$PROJECT" \
      --filter='state=ENABLED' --limit=1 --format='value(name)'
  )" ]]; then
    printf '%s' "DISABLED" \
      | gcloud secrets versions add "$ANT_SECRET" --data-file=- --project="$PROJECT"
  else
    echo "    Preserving the existing Anthropic secret version (no key supplied)."
  fi
else
  echo "==> Preserve production secret versions during control-plane-only bootstrap"
fi

if [[ "$stack_mode" == "existing" ]]; then
  echo "==> Deployment control-plane bootstrap (production runtimes untouched)"
  control_plan="${work_dir}/control-plane.tfplan"
  plan_and_apply "$control_plan" \
    -target=google_service_account.deployer \
    -target=google_service_account.smoke \
    -target=google_project_iam_member.deployer_project_roles \
    -target=google_artifact_registry_repository_iam_member.deployer_reader \
    -target=google_storage_bucket_iam_member.deployer_build_source_roles \
    -target=google_service_account_iam_member.deployer_use_pipeline_identity \
    -target=google_service_account_iam_member.deployer_use_dashboard_identity \
    -target=google_service_account_iam_member.deployer_use_cloud_build_identity \
    -target=google_service_account_iam_member.deployer_use_smoke_identity \
    -target=google_iam_workload_identity_pool.github \
    -target=google_iam_workload_identity_pool_provider.github \
    -target=google_service_account_iam_member.github_impersonates_deployer \
    -target=google_cloud_run_v2_job.pipeline_smoke
else
  # A greenfield workflow cannot capture a previous release until Terraform creates both
  # production runtimes. Images already contain this exact SHA, so the first full apply is safe.
  echo "==> Greenfield protected infrastructure plan"
  greenfield_plan="${work_dir}/greenfield.tfplan"
  plan_and_apply "$greenfield_plan"
fi

if [[ "${CONFIGURE_GITHUB_VARS:-1}" == "1" ]]; then
  echo "==> GitHub Actions repository variables"
  gh variable set GCP_PROJECT_ID --repo "$GITHUB_REPO" --body "$PROJECT"
  gh variable set GCP_REGION --repo "$GITHUB_REPO" --body "$REGION"
  gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER --repo "$GITHUB_REPO" \
    --body "$(terraform -chdir=infra/terraform output -raw github_workload_identity_provider)"
  gh variable set GCP_DEPLOY_SERVICE_ACCOUNT --repo "$GITHUB_REPO" \
    --body "$(terraform -chdir=infra/terraform output -raw github_deploy_service_account)"
  gh variable set GCP_BUILD_SERVICE_ACCOUNT --repo "$GITHUB_REPO" \
    --body "$(terraform -chdir=infra/terraform output -raw cloud_build_service_account)"
fi

latest_deploy_run() {
  local event_filter="${1:-}"
  local args=(
    run list
    --repo "$GITHUB_REPO"
    --workflow deploy.yml
    --branch main
    --commit "$GIT_SHA"
    --limit 20
    --json "databaseId,status,conclusion,event,headSha,createdAt,url"
  )
  if [[ -n "$event_filter" ]]; then
    args+=(--event "$event_filter")
  fi
  gh "${args[@]}" | jq -c 'sort_by(.createdAt) | reverse | .[0] // {}'
}

if [[ "$DISPATCH_DEPLOY" == "1" ]]; then
  echo "==> Execute and verify exact-SHA digest/canary/rollback deployment"
  run_json="$(latest_deploy_run)"
  run_id="$(jq -r '.databaseId // empty' <<<"$run_json")"
  run_status="$(jq -r '.status // empty' <<<"$run_json")"
  run_conclusion="$(jq -r '.conclusion // empty' <<<"$run_json")"

  # A main push can race this manual bootstrap by a few seconds. Give that run a chance to
  # appear before creating a redundant workflow_dispatch execution.
  if [[ -z "$run_id" ]]; then
    for _attempt in 1 2 3; do
      sleep 2
      run_json="$(latest_deploy_run)"
      run_id="$(jq -r '.databaseId // empty' <<<"$run_json")"
      [[ -n "$run_id" ]] && break
    done
    run_status="$(jq -r '.status // empty' <<<"$run_json")"
    run_conclusion="$(jq -r '.conclusion // empty' <<<"$run_json")"
  fi

  if [[ -n "$run_id" && "$run_status" == "completed" && "$run_conclusion" == "success" ]]; then
    echo "    Reusing successful deployment run ${run_id} for ${GIT_SHA}."
  elif [[ -n "$run_id" && "$run_status" != "completed" ]]; then
    echo "    Waiting for existing deployment run ${run_id}."
    gh run watch "$run_id" --repo "$GITHUB_REPO" --exit-status
  else
    previous_run_id="$run_id"
    gh workflow run deploy.yml --repo "$GITHUB_REPO" --ref main
    run_id=""
    for _attempt in {1..30}; do
      run_json="$(latest_deploy_run workflow_dispatch)"
      candidate_id="$(jq -r '.databaseId // empty' <<<"$run_json")"
      if [[ -n "$candidate_id" && "$candidate_id" != "$previous_run_id" ]]; then
        run_id="$candidate_id"
        break
      fi
      sleep 2
    done
    if [[ -z "$run_id" ]]; then
      echo "Could not identify the dispatched deployment run for ${GIT_SHA}." >&2
      exit 1
    fi
    echo "    Waiting for dispatched deployment run ${run_id}."
    gh run watch "$run_id" --repo "$GITHUB_REPO" --exit-status
  fi

  verified_run="$(
    gh run view "$run_id" --repo "$GITHUB_REPO" \
      --json headSha,conclusion,status,workflowName,url
  )"
  jq -e --arg sha "$GIT_SHA" '
    .headSha == $sha
    and .workflowName == "Deploy"
    and .status == "completed"
    and .conclusion == "success"
  ' <<<"$verified_run" >/dev/null || {
    echo "Deployment run did not verify the requested commit:" >&2
    jq . <<<"$verified_run" >&2
    exit 1
  }
  echo "    Verified deployment: $(jq -r .url <<<"$verified_run")"

  echo "==> Post-deployment full Terraform reconciliation"
  full_plan="${work_dir}/full-reconcile.tfplan"
  plan_and_apply "$full_plan"

  echo "==> Confirm zero Terraform drift"
  final_plan="${work_dir}/final-check.tfplan"
  if terraform -chdir=infra/terraform plan -input=false -detailed-exitcode \
    -out="$final_plan" "${TF_VARS[@]}"; then
    echo "    Terraform reports zero drift."
  else
    final_status=$?
    if [[ "$final_status" == "2" ]]; then
      echo "Terraform still reports changes after reconciliation." >&2
    fi
    exit "$final_status"
  fi
else
  echo "==> CD bootstrap complete; production deployment and full reconciliation skipped"
fi

echo ""
echo "Dashboard URL:"
terraform -chdir=infra/terraform output -raw dashboard_url
echo ""

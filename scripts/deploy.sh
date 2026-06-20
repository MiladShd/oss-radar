#!/usr/bin/env bash
# OSS Radar — build images, manage secrets, and provision the GCP stack with Terraform.
#
#   ./scripts/deploy.sh                 # template-mode agents (no Anthropic key)
#   ANTHROPIC_API_KEY=sk-ant-... ./scripts/deploy.sh   # Claude-powered agents
#
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${OSS_RADAR_PROJECT:-$(cat .gcp_project)}"
REGION="${REGION:-us-central1}"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/oss-radar"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"
GH_SECRET="oss-radar-github-token"
ANT_SECRET="oss-radar-anthropic-key"

echo "==> Project: $PROJECT  Region: $REGION  Tag: $TAG"

echo "==> Artifact Registry repo"
gcloud artifacts repositories describe oss-radar --location="$REGION" --project="$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create oss-radar --repository-format=docker --location="$REGION" \
       --description="OSS Radar images" --project="$PROJECT"

echo "==> Secrets"
ensure_secret() {
  gcloud secrets describe "$1" --project="$PROJECT" >/dev/null 2>&1 \
    || gcloud secrets create "$1" --replication-policy=automatic --project="$PROJECT"
}
ensure_secret "$GH_SECRET"
ensure_secret "$ANT_SECRET"
printf '%s' "$(gh auth token)" | gcloud secrets versions add "$GH_SECRET" --data-file=- --project="$PROJECT"
# A real key (sk-ant-...) enables Claude agents; the DISABLED sentinel keeps template mode.
printf '%s' "${ANTHROPIC_API_KEY:-DISABLED}" | gcloud secrets versions add "$ANT_SECRET" --data-file=- --project="$PROJECT"

echo "==> Build images (Cloud Build, linux/amd64)"
gcloud builds submit --project="$PROJECT" --config=infra/cloudbuild.yaml \
  --substitutions=_REPO="$REPO",_TAG="$TAG" .

echo "==> Terraform apply"
cd infra/terraform
terraform init -input=false
terraform apply -input=false -auto-approve \
  -var="project=${PROJECT}" -var="region=${REGION}" \
  -var="pipeline_image=${REPO}/pipeline:${TAG}" \
  -var="dashboard_image=${REPO}/dashboard:${TAG}"

echo ""
echo "==> Done. Dashboard URL:"
terraform output -raw dashboard_url
echo ""

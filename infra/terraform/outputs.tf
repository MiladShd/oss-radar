output "dashboard_url" {
  value       = google_cloud_run_v2_service.dashboard.uri
  description = "Public dashboard URL"
}

output "pipeline_job" {
  value = google_cloud_run_v2_job.pipeline.name
}

output "pipeline_smoke_job" {
  value       = google_cloud_run_v2_job.pipeline_smoke.name
  description = "No-role DuckDB canary executed before the production pipeline image is updated"
}

output "artifact_repository" {
  value       = google_artifact_registry_repository.images.name
  description = "Terraform-managed immutable Docker repository"
}

output "artifact_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "managed_secret_containers" {
  description = "Terraform-managed secret containers; secret values/versions are supplied outside state"
  value = {
    github    = google_secret_manager_secret.github.secret_id
    anthropic = google_secret_manager_secret.anthropic.secret_id
  }
}

output "bq_dataset" {
  value = google_bigquery_dataset.ds.dataset_id
}

output "build_source_bucket" {
  value       = google_storage_bucket.build_source.name
  description = "Dedicated staging bucket used by GitHub Actions for Cloud Build source archives"
}

output "github_workload_identity_provider" {
  value       = google_iam_workload_identity_pool_provider.github.name
  description = "Set this as the GCP_WORKLOAD_IDENTITY_PROVIDER GitHub Actions repository variable"
}

output "github_deploy_service_account" {
  value       = google_service_account.deployer.email
  description = "Set this as the GCP_DEPLOY_SERVICE_ACCOUNT GitHub Actions repository variable"
}

output "cloud_build_service_account" {
  value       = google_service_account.builder.name
  description = "Set this as the GCP_BUILD_SERVICE_ACCOUNT GitHub Actions repository variable"
}

output "github_actions_variables" {
  description = "Repository variables required by .github/workflows/deploy.yml"
  value = {
    GCP_PROJECT_ID                 = var.project
    GCP_REGION                     = var.region
    GCP_WORKLOAD_IDENTITY_PROVIDER = google_iam_workload_identity_pool_provider.github.name
    GCP_DEPLOY_SERVICE_ACCOUNT     = google_service_account.deployer.email
    GCP_BUILD_SERVICE_ACCOUNT      = google_service_account.builder.name
  }
}

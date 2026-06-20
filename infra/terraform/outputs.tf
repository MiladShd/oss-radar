output "dashboard_url" {
  value       = google_cloud_run_v2_service.dashboard.uri
  description = "Public dashboard URL"
}

output "pipeline_job" {
  value = google_cloud_run_v2_job.pipeline.name
}

output "artifact_bucket" {
  value = google_storage_bucket.artifacts.name
}

output "bq_dataset" {
  value = google_bigquery_dataset.ds.dataset_id
}

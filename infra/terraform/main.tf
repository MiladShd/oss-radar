terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
}

locals {
  labels = { app = "oss-radar", managed_by = "terraform" }
}

# --- Warehouse & artifact storage ---
resource "google_bigquery_dataset" "ds" {
  dataset_id                 = var.bq_dataset
  location                   = var.region
  description                = "OSS Radar warehouse: snapshots, features, predictions, model history, agent activity"
  labels                     = local.labels
  delete_contents_on_destroy = true
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project}-oss-radar"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
  labels                      = local.labels
}

# --- Service accounts ---
resource "google_service_account" "pipeline" {
  account_id   = "oss-radar-pipeline"
  display_name = "OSS Radar pipeline job"
}

resource "google_service_account" "dashboard" {
  account_id   = "oss-radar-dashboard"
  display_name = "OSS Radar dashboard service"
}

resource "google_service_account" "scheduler" {
  account_id   = "oss-radar-scheduler"
  display_name = "OSS Radar Cloud Scheduler invoker"
}

# --- IAM ---
resource "google_project_iam_member" "pipeline_bq_data" {
  project = var.project
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_project_iam_member" "pipeline_bq_jobs" {
  project = var.project
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_storage_bucket_iam_member" "pipeline_storage" {
  bucket = google_storage_bucket.artifacts.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_secret_manager_secret_iam_member" "pipeline_github" {
  secret_id = var.github_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_secret_manager_secret_iam_member" "pipeline_anthropic" {
  secret_id = var.anthropic_secret
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_project_iam_member" "dashboard_bq_data" {
  project = var.project
  role    = "roles/bigquery.dataViewer"
  member  = "serviceAccount:${google_service_account.dashboard.email}"
}

resource "google_project_iam_member" "dashboard_bq_jobs" {
  project = var.project
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.dashboard.email}"
}

# --- Pipeline Cloud Run Job (daily) ---
resource "google_cloud_run_v2_job" "pipeline" {
  name                = "oss-radar-pipeline"
  location            = var.region
  deletion_protection = false
  labels              = local.labels

  template {
    template {
      service_account = google_service_account.pipeline.email
      timeout         = "1800s"
      max_retries     = 1

      containers {
        image = var.pipeline_image

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        env {
          name  = "OSS_RADAR_BACKEND"
          value = "bigquery"
        }
        env {
          name  = "OSS_RADAR_ENV"
          value = "cloud"
        }
        env {
          name  = "OSS_RADAR_GCP_PROJECT"
          value = var.project
        }
        env {
          name  = "GCP_PROJECT"
          value = var.project
        }
        env {
          name  = "OSS_RADAR_REGION"
          value = var.region
        }
        env {
          name  = "OSS_RADAR_BQ_DATASET"
          value = var.bq_dataset
        }
        env {
          name  = "OSS_RADAR_GCS_BUCKET"
          value = google_storage_bucket.artifacts.name
        }
        env {
          name  = "OSS_RADAR_GITHUB_REPO"
          value = var.github_repo
        }
        env {
          name = "OSS_RADAR_GITHUB_TOKEN"
          value_source {
            secret_key_ref {
              secret  = var.github_secret
              version = "latest"
            }
          }
        }
        env {
          name = "OSS_RADAR_ANTHROPIC_API_KEY"
          value_source {
            secret_key_ref {
              secret  = var.anthropic_secret
              version = "latest"
            }
          }
        }
      }
    }
  }
}

# --- Dashboard Cloud Run Service (public, scale-to-zero) ---
resource "google_cloud_run_v2_service" "dashboard" {
  name                = "oss-radar-dashboard"
  location            = var.region
  deletion_protection = false
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = local.labels

  template {
    service_account = google_service_account.dashboard.email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.dashboard_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      env {
        name  = "OSS_RADAR_BACKEND"
        value = "bigquery"
      }
      env {
        name  = "OSS_RADAR_ENV"
        value = "cloud"
      }
      env {
        name  = "OSS_RADAR_GCP_PROJECT"
        value = var.project
      }
      env {
        name  = "GCP_PROJECT"
        value = var.project
      }
      env {
        name  = "OSS_RADAR_REGION"
        value = var.region
      }
      env {
        name  = "OSS_RADAR_BQ_DATASET"
        value = var.bq_dataset
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "dashboard_public" {
  name     = google_cloud_run_v2_service.dashboard.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Cloud Scheduler -> run the pipeline job daily ---
resource "google_cloud_run_v2_job_iam_member" "scheduler_invoke" {
  name     = google_cloud_run_v2_job.pipeline.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "daily" {
  name      = "oss-radar-daily"
  region    = var.region
  schedule  = var.schedule
  time_zone = "UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project}/locations/${var.region}/jobs/${google_cloud_run_v2_job.pipeline.name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoke]
}

# --- Cloud Scheduler -> validation backstop (cloud-side cross-check refresh) ---
# Runs the SAME pipeline job but overrides the entrypoint args to `validate --upload`, so the
# growth-model validation stats + reproducibility dumps are regenerated daily and pushed to GCS
# even if the local Mac (which holds the Wolfram Engine) is offline. The `validate` command also
# alarms (structured log warning) when the local Wolfram educational cross-check has gone stale.
resource "google_cloud_scheduler_job" "validate_daily" {
  name      = "oss-radar-validate-daily"
  region    = var.region
  schedule  = var.validate_schedule
  time_zone = "UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project}/locations/${var.region}/jobs/${google_cloud_run_v2_job.pipeline.name}:run"
    headers     = { "Content-Type" = "application/json" }
    body = base64encode(jsonencode({
      overrides = {
        containerOverrides = [{
          args = ["validate", "--upload", "--out", "/tmp/validation"]
        }]
      }
    }))
    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_invoke]
}

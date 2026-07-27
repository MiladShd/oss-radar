terraform {
  required_version = ">= 1.5"
  backend "gcs" {
    prefix = "oss-radar/prod"
  }
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

data "google_project" "current" {
  project_id = var.project
}

locals {
  labels            = { app = "oss-radar", managed_by = "terraform" }
  deployment_labels = merge(local.labels, { git_sha = var.git_sha })
}

# --- Immutable release images ---
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "oss-radar"
  description   = "Immutable OSS Radar pipeline and dashboard release images"
  format        = "DOCKER"

  docker_config {
    immutable_tags = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

# --- Warehouse & artifact storage ---
resource "google_bigquery_dataset" "ds" {
  dataset_id                 = var.bq_dataset
  location                   = var.region
  description                = "OSS Radar warehouse: snapshots, features, predictions, model history, agent activity"
  labels                     = local.labels
  delete_contents_on_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket" "artifacts" {
  name                        = "${var.project}-oss-radar"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels

  lifecycle {
    prevent_destroy = true
  }
}

# GitHub Actions uploads a source archive here before submitting an immutable
# Cloud Build. Keeping it separate from model artifacts makes the deployer's
# object permissions narrow and lets lifecycle cleanup remove old archives.
resource "google_storage_bucket" "build_source" {
  name                        = "${var.project}-oss-radar-build-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Secret containers are infrastructure; secret versions remain release-time inputs and are never
# stored in Terraform state.
resource "google_secret_manager_secret" "github" {
  secret_id = var.github_secret
  labels    = local.labels

  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret" "anthropic" {
  secret_id = var.anthropic_secret
  labels    = local.labels

  replication {
    auto {}
  }

  lifecycle {
    prevent_destroy = true
  }
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

resource "google_service_account" "deployer" {
  account_id   = "oss-radar-deployer"
  display_name = "OSS Radar GitHub Actions deployer"
  description  = "Keyless deployment identity; impersonation is restricted to this repository's main branch"
}

resource "google_service_account" "builder" {
  account_id   = "oss-radar-builder"
  display_name = "OSS Radar Cloud Build executor"
  description  = "Dedicated least-privilege identity for building OSS Radar container images"
}

resource "google_service_account" "smoke" {
  account_id   = "oss-radar-smoke"
  display_name = "OSS Radar isolated release smoke"
  description  = "No-role identity for the DuckDB release smoke job; intentionally has no project permissions"
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
  secret_id = google_secret_manager_secret.github.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.pipeline.email}"
}

resource "google_secret_manager_secret_iam_member" "pipeline_anthropic" {
  secret_id = google_secret_manager_secret.anthropic.id
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

# The deployer can submit builds and update existing Cloud Run resources. It cannot
# change IAM, secrets, datasets, schedules, or the Terraform-managed infrastructure.
resource "google_project_iam_member" "deployer_project_roles" {
  for_each = toset([
    "roles/cloudbuild.builds.editor",
    "roles/run.developer",
    "roles/serviceusage.serviceUsageConsumer",
  ])

  project = var.project
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_artifact_registry_repository_iam_member" "deployer_reader" {
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_storage_bucket_iam_member" "deployer_build_source_roles" {
  for_each = toset([
    "roles/storage.legacyBucketReader",
    "roles/storage.objectCreator",
  ])

  bucket = google_storage_bucket.build_source.name
  role   = each.value
  member = "serviceAccount:${google_service_account.deployer.email}"
}

# The dedicated Cloud Build identity reads the uploaded source archive and
# pushes images. It deliberately replaces the project's broad default builder.
resource "google_storage_bucket_iam_member" "builder_source_reader" {
  bucket = google_storage_bucket.build_source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_artifact_registry_repository_iam_member" "builder_artifact_writer" {
  project    = var.project
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_project_iam_member" "builder_log_writer" {
  project = var.project
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.builder.email}"
}

resource "google_service_account_iam_member" "deployer_use_pipeline_identity" {
  service_account_id = google_service_account.pipeline.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_use_dashboard_identity" {
  service_account_id = google_service_account.dashboard.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# Build creation alone is insufficient: the deployer must also be allowed to act as the dedicated,
# least-privilege Cloud Build execution identity.
resource "google_service_account_iam_member" "deployer_use_cloud_build_identity" {
  service_account_id = google_service_account.builder.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "deployer_use_smoke_identity" {
  service_account_id = google_service_account.smoke.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deployer.email}"
}

# GitHub Actions authenticates without a stored service-account key. Both immutable
# GitHub ids and refs/heads/main are checked before Google accepts an OIDC token.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "oss-radar-github"
  display_name              = "OSS Radar GitHub Actions"
  description               = "Keyless identity pool for OSS Radar deployments"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "oss-radar-main"
  display_name                       = "OSS Radar main branch"

  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository"          = "assertion.repository"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.ref"                 = "assertion.ref"
    "attribute.workflow_ref"        = "assertion.workflow_ref"
  }
  attribute_condition = join(" && ", [
    "assertion.repository == '${var.github_repo}'",
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository_owner_id == '${var.github_repository_owner_id}'",
    "assertion.ref == 'refs/heads/${var.github_deploy_branch}'",
    "assertion.workflow_ref == '${var.github_repo}/.github/workflows/deploy.yml@refs/heads/${var.github_deploy_branch}'",
  ])

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_impersonates_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}

# --- Isolated release smoke (no cloud data/secrets permissions) ---
resource "google_cloud_run_v2_job" "pipeline_smoke" {
  name                = "oss-radar-pipeline-smoke"
  location            = var.region
  deletion_protection = true
  labels              = local.labels

  template {
    template {
      service_account = google_service_account.smoke.email
      timeout         = "900s"
      max_retries     = 0

      containers {
        image = var.pipeline_image
        args  = ["run", "--dry-run", "--limit", "8"]

        resources {
          limits = {
            cpu    = "2"
            memory = "4Gi"
          }
        }

        env {
          name  = "OSS_RADAR_BACKEND"
          value = "duckdb"
        }
        env {
          name  = "OSS_RADAR_ENV"
          value = "smoke"
        }
        env {
          name  = "OSS_RADAR_DUCKDB_PATH"
          value = "/tmp/oss-radar-release-smoke.duckdb"
        }
        env {
          name  = "OSS_RADAR_ANTHROPIC_API_KEY"
          value = "DISABLED"
        }
      }
    }
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      template[0].template[0].containers[0].image,
    ]
  }
}

# --- Pipeline Cloud Run Job (daily) ---
resource "google_cloud_run_v2_job" "pipeline" {
  name                = "oss-radar-pipeline"
  location            = var.region
  deletion_protection = true
  labels              = local.deployment_labels

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

  # Terraform owns the service shape; the verified GitHub release workflow owns image, runtime
  # provenance, and release-time environment mutation after initial creation.
  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      labels["git_sha"],
      template[0].template[0].containers[0].image,
    ]
  }
}

# --- Dashboard Cloud Run Service (public, scale-to-zero) ---
resource "google_cloud_run_v2_service" "dashboard" {
  name                = "oss-radar-dashboard"
  location            = var.region
  deletion_protection = true
  ingress             = "INGRESS_TRAFFIC_ALL"
  labels              = local.deployment_labels

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

  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      labels["git_sha"],
      template[0].containers[0].image,
    ]
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

variable "project" {
  type        = string
  description = "GCP project id"
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "pipeline_image" {
  type        = string
  description = "Full Artifact Registry image URL for the pipeline job"
}

variable "dashboard_image" {
  type        = string
  description = "Full Artifact Registry image URL for the dashboard service"
}

variable "github_repo" {
  type    = string
  default = "MiladShd/oss-radar"
}

variable "github_secret" {
  type    = string
  default = "oss-radar-github-token"
}

variable "anthropic_secret" {
  type    = string
  default = "oss-radar-anthropic-key"
}

variable "schedule" {
  type        = string
  default     = "30 9 * * *"
  description = "Daily cron (UTC) for the pipeline job"
}

variable "validate_schedule" {
  type        = string
  default     = "30 10 * * *"
  description = "Daily cron (UTC) for the validation backstop job (runs after the pipeline)"
}

variable "bq_dataset" {
  type    = string
  default = "oss_radar"
}

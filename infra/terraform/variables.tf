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

variable "github_repository_id" {
  type        = string
  default     = "1274922810"
  description = "Immutable numeric GitHub repository id used to restrict deployment OIDC tokens"
}

variable "github_repository_owner_id" {
  type        = string
  default     = "14307102"
  description = "Immutable numeric GitHub repository-owner id used to restrict deployment OIDC tokens"
}

variable "github_deploy_branch" {
  type        = string
  default     = "main"
  description = "Only GitHub OIDC tokens for this branch may impersonate the deployment service account"
}

variable "git_sha" {
  type        = string
  default     = "unknown"
  description = "Git commit represented by the deployed pipeline and dashboard images"

  validation {
    condition     = can(regex("^[a-z0-9_-]{1,63}$", var.git_sha))
    error_message = "git_sha must be a lowercase Git SHA or a label-safe marker such as dirty-<sha>."
  }
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

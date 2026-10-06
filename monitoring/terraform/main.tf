# Cloud Run scheduled prober for the quant-swarm MCP endpoints.
#
# Deploys (user-run; leaves the machine):
#   terraform -chdir=monitoring/terraform init
#   terraform -chdir=monitoring/terraform apply
#
# Creates: a Cloud Run job running the prober every 5 minutes, a service account
# limited to the two secrets it needs, and a GCP alert on job execution failure
# (independent backstop to the Grafana dead-man rule).

terraform {
  required_providers {
    google = { source = "hashicorp/google", version = "~> 6.0" }
  }
}

variable "project_id" { type = string }
variable "region" { default = "europe-west1" }
variable "image" {
  description = "Container image that contains monitoring/prober.py and httpx (the quant-swarm release image)."
  type        = string
}
variable "hosted_url" { type = string }
variable "site_url" { type = string }
variable "loki_url" { type = string }

locals {
  service_account_id = "mcp-prober"
  job_name           = "mcp-prober"
}

resource "google_service_account" "prober" {
  account_id   = local.service_account_id
  display_name = "quant-swarm MCP endpoint prober"
}

resource "google_secret_manager_secret" "monitor_token" {
  secret_id = "mcp-monitor-token"
  replication { auto {} }
}

resource "google_secret_manager_secret" "loki_password" {
  secret_id = "grafana-loki-password"
  replication { auto {} }
}

# shared secret the prober uses to POST run summaries to the site's
# /api/status/probe ingest (7.11); the same value lives in the site's
# SWARM_MCP_INTERNAL_KEY env.
resource "google_secret_manager_secret" "internal_key" {
  secret_id = "swarm-mcp-internal-key"
  replication { auto {} }
}

data "google_iam_policy" "secret_accessor" {
  binding {
    role = "roles/secretmanager.secretAccessor"
    members = [
      "serviceAccount:${google_service_account.prober.email}",
    ]
  }
}

# least privilege: the prober reads exactly these two secrets, nothing else
resource "google_secret_manager_secret_iam_policy" "monitor_token" {
  secret_id   = google_secret_manager_secret.monitor_token.secret_id
  policy_data = data.google_iam_policy.secret_accessor.policy_data
}

resource "google_secret_manager_secret_iam_policy" "loki_password" {
  secret_id   = google_secret_manager_secret.loki_password.secret_id
  policy_data = data.google_iam_policy.secret_accessor.policy_data
}

resource "google_secret_manager_secret_iam_policy" "internal_key" {
  secret_id   = google_secret_manager_secret.internal_key.secret_id
  policy_data = data.google_iam_policy.secret_accessor.policy_data
}

resource "google_cloud_run_v2_job" "prober" {
  name     = local.job_name
  location = var.region

  template {
    task_count = 1
    timeout    = "600s"
    containers {
      image = var.image
      command = ["python"]
      args    = ["-m", "monitoring.prober", "--base-url", var.hosted_url, "--site-url", var.site_url]
      env {
        name  = "SWARM_MCP_PROBE_ENV"
        value = "production"
      }
      env {
        name  = "LOKI_URL"
        value = var.loki_url
      }
      env {
        name = "SWARM_MCP_MONITOR_TOKEN"
        value_source {
          secret_key_source {
            secret       = google_secret_manager_secret.monitor_token.secret_id
            version      = "latest"
          }
        }
      }
      env {
        name = "LOKI_PASSWORD"
        value_source {
          secret_key_source {
            secret  = google_secret_manager_secret.loki_password.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SWARM_MCP_INTERNAL_KEY"
        value_source {
          secret_key_source {
            secret  = google_secret_manager_secret.internal_key.secret_id
            version = "latest"
          }
        }
      }
    }
    max_retries = 1
  }

  depends_on = [
    google_secret_manager_secret_iam_policy.monitor_token,
    google_secret_manager_secret_iam_policy.loki_password,
    google_secret_manager_secret_iam_policy.internal_key,
  ]
}

resource "google_cloud_scheduler_job" "prober" {
  name             = "${local.job_name}-schedule"
  region           = var.region
  schedule         = "*/5 * * * *"
  attempt_deadline = "600s"
  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_id}/jobs/${local.job_name}:run"
    oauth_token {
      service_account_email = google_service_account.prober.email
    }
  }
}

# independent backstop: GCP alerts when the scheduled job itself fails
resource "google_monitoring_alert_policy" "prober_job_failed" {
  display_name = "MCP prober job failing"
  combiner     = "OR"
  conditions {
    display_name = "job execution failed"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${local.job_name}\" AND metric.type=\"run.googleapis.com/job/execution_count\" AND metric.label.result=\"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }
  documentation {
    content = "The scheduled MCP endpoint prober failed; probes never reached Grafana. Check Cloud Run logs for the mcp-prober job."
  }
}

output "scheduler_job" {
  value = google_cloud_scheduler_job.prober.name
}

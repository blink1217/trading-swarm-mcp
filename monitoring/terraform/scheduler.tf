# Weekly Saturday 06:00 London Cloud Scheduler trigger for the House League refresh.
#
# Re-evaluates the 8 canonical `house_v4` baseline variants against the live
# champion on the rolling 1010-day panel (`credits_charged = 0`, `house_seed = true`)
# so the public Shadow Tournament leaderboard stays continuously fresh without
# burning customer credits or mixing legacy `house_v3` scores.

variable "house_league_internal_key" {
  description = "Value of SWARM_MCP_INTERNAL_KEY passed as X-Swarm-Internal-Key to /internal/tournament/house-league."
  type        = string
  sensitive   = true
  default     = ""
}

resource "google_cloud_scheduler_job" "weekly_house_league" {
  count       = var.house_league_internal_key != "" ? 1 : 0
  name        = "mcp-weekly-house-league"
  description = "Weekly Saturday 06:00 Europe/London refresh of Shadow Tournament house_v4 baselines"
  project     = var.project_id
  region      = var.region
  schedule    = "0 6 * * 6"
  time_zone   = "Europe/London"

  retry_config {
    retry_count          = 2
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

  http_target {
    http_method = "POST"
    uri         = "${trimsuffix(var.site_url, "/")}/internal/tournament/house-league"
    body        = base64encode(jsonencode({ trigger = "cloud_scheduler_weekly" }))
    headers = {
      "Content-Type"         = "application/json"
      "X-Swarm-Internal-Key" = var.house_league_internal_key
    }
  }
}

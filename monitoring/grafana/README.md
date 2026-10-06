# MCP endpoint monitoring

- \prober.py\ (in the parent dir): shape-asserting probes for every MCP endpoint,
  one \mcp_probe\ Loki event per probe plus an \mcp_probe_run\ summary. Non-zero
  exit on any failure (Cloud Run job alert backstops the Grafana rules).
- \provision.py\: idempotent Grafana provisioning (email contact point,
  notification policy on service=quant-swarm-mcp, five alert rules incl. a
  15-minute dead-man switch, and the endpoint dashboard). Requires
  GRAFANA_URL, GRAFANA_SA_TOKEN, GRAFANA_OPS_EMAIL.
- \../terraform/main.tf\: Cloud Run job + Cloud Scheduler (*/5) + a
  least-privilege service account (monitor token + Loki password only) + a GCP
  alert on job execution failure.

Alert rules: endpoint-down (>=2 failures/10m, critical), dead-man (no probe run
in 15m, critical, noDataState=Alerting), latency p95>5s/30m (warning), version
drift (warning), suggestion spam >50/h (warning).

Probe credentials: a monitor token (institutional plan, \monitor: true\ set by
the admin only) - the site skips quota/credit/analytics writes for it; the
meter probe always runs in dry-run mode. Probes never POST suggestions, never
submit tournaments, never warm caches.

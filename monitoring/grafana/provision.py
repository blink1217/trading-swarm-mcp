"""Idempotent Grafana provisioning for the quant-swarm MCP monitor.

Usage (user-run; leaves the machine):
  GRAFANA_URL=https://... GRAFANA_SA_TOKEN=... GRAFANA_OPS_EMAIL=ops@... \
      python monitoring/grafana/provision.py

Upserts, in order: contact point -> notification policy -> alert rules (folder
"trading-ops") -> dashboard. Safe to re-run; every step PUTs by name/uid.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.request

HERE = pathlib.Path(__file__).parent
FOLDER_NAME = "trading-ops"
FOLDER_UID = "trading-ops-mcp"
DASHBOARD_UID = "quant-swarm-mcp-endpoints"


def api(base: str, token: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read().decode() or "{}")
        return r.status, payload


def main() -> int:
    base = os.environ.get("GRAFANA_URL", "").strip()
    token = os.environ.get("GRAFANA_SA_TOKEN", "").strip()
    email = os.environ.get("GRAFANA_OPS_EMAIL", "").strip()
    if not base or not token or not email:
        print("GRAFANA_URL, GRAFANA_SA_TOKEN and GRAFANA_OPS_EMAIL are required", file=sys.stderr)
        return 2
    spec = json.loads((HERE / "alerts.json").read_text(encoding="utf-8"))

    # 1. contact point (idempotent upsert by uid)
    contact = {
        "uid": "quant-swarm-ops-email",
        "name": spec["contactPoint"]["name"],
        "type": "email",
        "settings": {"addresses": [email]},
    }
    status, _ = api(base, token, "PUT", "/v1/provisioning/contact-points/quant-swarm-ops-email", contact)
    print(f"contact point: {status}")

    # 2. notification policy (read-modify-write: insert our route at the top)
    status, policy = api(base, token, "GET", "/v1/provisioning/policies")
    route = spec["notificationPolicy"]
    routes = [r for r in policy.get("routes", [])
              if r.get("receiver") != route["receiver"]]
    policy["routes"] = [route] + routes
    status, _ = api(base, token, "PUT", "/v1/provisioning/policies", policy)
    print(f"notification policy: {status}")

    # 3. folder + alert rules
    api(base, token, "POST", "/v1/folders",
        {"uid": FOLDER_UID, "title": FOLDER_NAME})
    for rule in spec["rules"]:
        rule["folderUID"] = FOLDER_UID
        rule["ruleGroup"] = "mcp-endpoints"
        status, _ = api(base, token, "POST", "/v1/provisioning/alert-rules", rule)
        print(f"rule '{rule['title']}': {status}")

    # 4. dashboard (status history per endpoint + latency + failures)
    dashboard = {
        "uid": DASHBOARD_UID,
        "title": "quant-swarm MCP endpoints",
        "tags": ["quant-swarm", "mcp"],
        "refresh": "1m",
        "panels": [
            {
                "type": "status-history", "title": "Probe status by endpoint",
                "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
                "targets": [{
                    "expr": "{service=\"quant-swarm-mcp\"} | json",
                    "queryType": "range", "refId": "A",
                }],
            },
            {
                "type": "timeseries", "title": "Probe latency (ms)",
                "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
                "targets": [{
                    "expr": "quantile_over_time(0.95, {service=\"quant-swarm-mcp\"} | json | unwrap latency_ms [5m]) by (endpoint)",
                    "queryType": "range", "refId": "A",
                }],
            },
            {
                "type": "logs", "title": "Probe failures",
                "gridPos": {"x": 0, "y": 8, "w": 24, "h": 10},
                "targets": [{
                    "expr": "{service=\"quant-swarm-mcp\"} | json | ok=\"false\"",
                    "queryType": "range", "refId": "A",
                }],
            },
        ],
    }
    status, _ = api(base, token, "POST", "/v1/dashboards/db",
                    {"dashboard": dashboard, "overwrite": True,
                     "folderUid": FOLDER_UID})
    print(f"dashboard: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

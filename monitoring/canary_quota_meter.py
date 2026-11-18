#!/usr/bin/env python3
"""Post-deploy hosted metering & relay canary (requires a non-monitor token).

Monitor tokens (`monitor: true`) are intentionally metering-exempt on the site,
so this canary uses `SWARM_MCP_CANARY_TOKEN` (a standard free-plan test token)
to verify:
  1. Invalid symbol `ZZ_Q` fails at the site relay (`data relay refused (HTTP 400): no valid symbols`)
     and never leaks raw provider URLs (`data.alpaca.markets`).
  2. Calling `market.regime(["QQQ"])` advances `/api/mcp/verify` `quota.used` by +1.
"""
from __future__ import annotations

import json
import os
import sys
import httpx

MCP_URL = os.environ.get("SWARM_MCP_URL", "https://mcp.1.21initiative.com/mcp/data").rstrip("/")
VERIFY_URL = os.environ.get("SWARM_MCP_VERIFY_URL", "https://1.21initiative.com/api/mcp/verify")


def _verify_used(client: httpx.Client, token: str) -> int:
    r = client.post(VERIFY_URL, json={"token": token}, headers={"X-Swarm-Surface": "hosted"})
    r.raise_for_status()
    body = r.json()
    if body.get("monitor"):
        raise RuntimeError("SWARM_MCP_CANARY_TOKEN must NOT be a monitor:true token (monitors are metering-exempt)")
    return int(body["quota"]["used"])


def _call_tool(client: httpx.Client, token: str, name: str, arguments: dict) -> dict:
    r = client.post(
        MCP_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    token = os.environ.get("SWARM_MCP_CANARY_TOKEN", "").strip()
    if not token:
        print("SKIP: set SWARM_MCP_CANARY_TOKEN to a non-monitor free-tier token", file=sys.stderr)
        return 2

    with httpx.Client(timeout=30.0) as client:
        before = _verify_used(client, token)

        bad = _call_tool(client, token, "market.regime", {"symbols": ["ZZ_Q"]})
        bad_text = json.dumps(bad)
        if "alpaca.markets" in bad_text or "finnhub.io" in bad_text:
            raise AssertionError(f"Upstream provider URL leaked in error response: {bad_text}")
        if "no valid symbols" not in bad_text:
            raise AssertionError(f"Expected site relay 400 ('no valid symbols'), got: {bad_text}")

        ok = _call_tool(client, token, "market.regime", {"symbols": ["QQQ"]})
        ok_text = json.dumps(ok)
        if "alpaca.markets" in ok_text:
            raise AssertionError(f"Upstream provider URL leaked: {ok_text}")

        after = _verify_used(client, token)
        if after < before + 1:
            raise AssertionError(f"Expected quota.used to increment ({before} -> >= {before + 1}), got {after}")

        print(f"OK: relay 400 verified, zero provider leakage, quota.used {before} -> {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

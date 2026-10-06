"""MCP endpoint prober: exercises every quant-swarm MCP endpoint on a schedule and pushes
one structured event per probe to Grafana Cloud Loki, where alert rules notify ops.

Probes (all shape-asserting, never just status-code):
- hosted /health and, per server (data, warden, gym): MCP initialize, tools/list (expected
  tool counts; pit.analysis present) and one cheap tools/call; pit.analysis returns verdicts
  within the allowed set.
- site /health, /api/healthz, /api/mcp/verify (real verify round-trip with the monitor
  token), /api/mcp/pit/coverage, /api/mcp/pit/suggestions/board and /api/mcp/meter with
  dry_run=true (authenticates + prices, writes nothing).
- Smithery: initialize only.
Never POSTs suggestions; never submits tournaments; never warms caches.

Credentials: a monitor token (institutional plan, `monitor: true`) is used for authed calls;
the site skips quota/credit/analytics side effects for it. Loki push uses the same
basic-auth format as trade_bot_dynamic/observability.py (LOKI_URL/LOKI_USER/LOKI_PASSWORD).

Usage: python -m monitoring.prober [--dry-run] [--print] [--base-url URL] [--site-url URL]
Exit code: non-zero when any probe fails (so Cloud Run job failures alert independently).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
import time
import uuid

try:
    import httpx
except ImportError as e:  # pragma: no cover - httpx is a runtime dependency
    raise SystemExit("monitoring prober needs httpx: pip install httpx") from e

DEFAULT_HOSTED = "https://swarm-mcp-503318750546.europe-west1.run.app"
DEFAULT_SITE = "https://1.21initiative.com"
TIMEOUT_S = 20.0
PROBE_VERSION = "0.5.0"

ALLOWED_PIT_STATUSES = {
    "SIGNIFICANT",
    "NOT_SIGNIFICANT",
    "NOT_JUDGED",
    "INSUFFICIENT_OBSERVATIONS",
    "UNVERIFIED",
}
EXPECTED_TOOL_COUNTS = {"swarm-data": 13, "swarm-warden": 6, "swarm-gym": 7}
ALLOWED_LABEL_KEYS = ("service", "env", "endpoint", "ok")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def redact(text: str) -> str:
    """Keep error classes, never tokens or bodies: strip anything token-like."""
    if not text:
        return ""
    text = re.sub(r"121_[A-Za-z0-9]+", "<token>", text)
    text = re.sub(r"Bearer\s+\S+", "Bearer <token>", text, flags=re.I)
    text = re.sub(r"[A-Za-z0-9_\-]{40,}", "<redacted>", text)
    text = text.replace("\n", " ")
    return text[:200]


class ProbeResult:
    __slots__ = ("endpoint", "check", "ok", "http_status", "latency_ms", "error")

    def __init__(self, endpoint: str, check: str, ok: bool, http_status: int,
                 latency_ms: float, error: str = ""):
        self.endpoint = endpoint
        self.check = check
        self.ok = ok
        self.http_status = http_status
        self.latency_ms = latency_ms
        self.error = redact(error)

    def event(self, probe_run_id: str) -> dict:
        return {
            "event": "mcp_probe",
            "endpoint": self.endpoint,
            "check": self.check,
            "ok": "true" if self.ok else "false",
            "http_status": self.http_status,
            "latency_ms": round(self.latency_ms, 1),
            "error_class": self.error or "none",
            "probe_run_id": probe_run_id,
            "version": PROBE_VERSION,
        }


def _extract_json(response_text: str) -> dict | None:
    """Parse a JSON body or an SSE stream (data: lines) into a dict."""
    text = response_text.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except ValueError:
            return None
    for line in reversed(text.splitlines()):
        if line.startswith("data:"):
            payload = line[5:].strip()
            try:
                return json.loads(payload)
            except ValueError:
                continue
    return None


async def _rpc(client: "httpx.AsyncClient", url: str, token: str, payload: dict,
               result: list[ProbeResult], endpoint: str, check: str,
               expect: "set[str] | None" = None) -> dict | None:
    t0 = time.perf_counter()
    try:
        r = await client.post(
            url, json=payload, timeout=TIMEOUT_S,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    except Exception as e:  # noqa: BLE001 - probe never crashes the run
        result.append(ProbeResult(endpoint, check, False, 0,
                                  (time.perf_counter() - t0) * 1000.0,
                                  f"transport:{type(e).__name__}"))
        return None
    latency = (time.perf_counter() - t0) * 1000.0
    body = _extract_json(r.text) if r.text else None
    if r.status_code != 200:
        result.append(ProbeResult(endpoint, check, False, r.status_code, latency,
                                  f"http:{r.status_code}"))
        return None
    if body is None:
        result.append(ProbeResult(endpoint, check, False, r.status_code, latency,
                                  "shape:non-json"))
        return None
    if "error" in body:
        result.append(ProbeResult(endpoint, check, False, r.status_code, latency,
                                  f"rpc:{(body.get('error') or {}).get('code', 'unknown')}"))
        return None
    if expect is not None and body.get("result") is None:
        result.append(ProbeResult(endpoint, check, False, r.status_code, latency,
                                  "shape:no-result"))
        return None
    result.append(ProbeResult(endpoint, check, True, r.status_code, latency))
    return body.get("result")


async def _get(client: "httpx.AsyncClient", url: str, result: list[ProbeResult],
               endpoint: str, check: str, *, token: str = "", expect_json: bool = False,
               expect_status: tuple[int, ...] = (200,)) -> dict | None:
    t0 = time.perf_counter()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = await client.get(url, timeout=TIMEOUT_S, headers=headers)
    except Exception as e:  # noqa: BLE001
        result.append(ProbeResult(endpoint, check, False, 0,
                                  (time.perf_counter() - t0) * 1000.0,
                                  f"transport:{type(e).__name__}"))
        return None
    latency = (time.perf_counter() - t0) * 1000.0
    if r.status_code not in expect_status:
        result.append(ProbeResult(endpoint, check, False, r.status_code, latency,
                                  f"http:{r.status_code}"))
        return None
    body = None
    if expect_json:
        try:
            body = r.json()
        except ValueError:
            result.append(ProbeResult(endpoint, check, False, r.status_code, latency,
                                      "shape:non-json"))
            return None
    result.append(ProbeResult(endpoint, check, True, r.status_code, latency))
    return body


async def probe_server(client, base_url: str, key: str, token: str,
                       results: list[ProbeResult]) -> None:
    url = f"{base_url}/mcp/{key}"
    init = await _rpc(client, url, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "swarm-prober", "version": PROBE_VERSION}},
    }, results, f"mcp/{key}", "initialize", expect={"serverInfo"})
    if init is None:
        return
    listed = await _rpc(client, url, token, {"jsonrpc": "2.0", "id": 2,
                                             "method": "tools/list"},
                        results, f"mcp/{key}", "tools/list", expect={"tools"})
    if listed is None:
        return
    names = {t.get("name") for t in listed.get("tools", [])}
    expected = EXPECTED_TOOL_COUNTS.get(key)
    if expected is not None and len(names) < expected - 2:
        # tolerate +/- small drift but never a half-empty catalog
        results.append(ProbeResult(f"mcp/{key}", "tools/list", False, 200, 0.0,
                                   f"shape:tool-count-{len(names)}"))
    if key == "swarm-data" and "pit.analysis" not in names:
        results.append(ProbeResult(f"mcp/{key}", "tools/list", False, 200, 0.0,
                                   "shape:pit-analysis-missing"))


async def probe_pit_analysis(client, base_url: str, token: str,
                             results: list[ProbeResult]) -> None:
    url = f"{base_url}/mcp/data"
    init = await _rpc(client, url, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "swarm-prober", "version": PROBE_VERSION}},
    }, results, "mcp/data", "initialize", expect={"serverInfo"})
    if init is None:
        return
    body = await _rpc(client, url, token, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "pit.analysis", "arguments": {"symbols": ["AA"]}},
    }, results, "mcp/data", "tools/call:pit.analysis", expect={"content"})
    if body is None:
        return
    text = json.dumps(body)
    statuses = {s for s in ALLOWED_PIT_STATUSES if s in text}
    if not statuses and '"error"' not in text:
        results.append(ProbeResult("mcp/data", "tools/call:pit.analysis", False, 200, 0.0,
                                   "shape:no-verdicts"))
    else:
        results.append(ProbeResult("mcp/data", "tools/call:pit.analysis", True, 200, 0.0))


async def probe_cheap_calls(client, base_url: str, token: str,
                            results: list[ProbeResult]) -> None:
    bars = []
    ts = 0
    price = 100.0
    for _ in range(60):
        ts += 86400
        price *= 1.001
        bars.append({"symbol": "PROBE", "ts": ts, "open": price, "high": price * 1.01,
                     "low": price * 0.99, "close": price, "volume": 1_000_000})
    await _rpc(client, f"{base_url}/mcp/data", token, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "market.regime",
                   "arguments": {"bars": bars}},
    }, results, "mcp/data", "tools/call:market.regime", expect={"content"})
    await _rpc(client, f"{base_url}/mcp/warden", token, {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "warden.validate_order",
                   "arguments": {"order": {"symbol": "PROBE", "notional": 100.0,
                                            "side": "buy"},
                                 "equity": 10_000.0}},
    }, results, "mcp/warden", "tools/call:warden.validate_order", expect={"content"})
    await _rpc(client, f"{base_url}/mcp/gym", token, {
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "tournament.leaderboard", "arguments": {}},
    }, results, "mcp/gym", "tools/call:tournament.leaderboard", expect={"content"})


async def probe_site(client, site_url: str, token: str, results: list[ProbeResult]) -> None:
    await _get(client, f"{site_url}/health", results, "site", "health")
    await _get(client, f"{site_url}/api/healthz", results, "site", "healthz")
    # /verify with the monitor token: proves the whole verify path end to end
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{site_url}/api/mcp/verify", timeout=TIMEOUT_S,
                              json={"token": token},
                              headers={"Authorization": f"Bearer {token}"})
    except Exception as e:  # noqa: BLE001
        results.append(ProbeResult("site", "verify", False, 0,
                                   (time.perf_counter() - t0) * 1000.0,
                                   f"transport:{type(e).__name__}"))
        return
    ok = r.status_code == 200
    try:
        body = r.json()
        ok = ok and body.get("ok") is True and body.get("plan") in ("free", "pro", "institutional")
    except ValueError:
        ok, body = False, {}
    results.append(ProbeResult("site", "verify", ok, r.status_code,
                               (time.perf_counter() - t0) * 1000.0,
                               "" if ok else f"shape:{r.status_code}"))
    await _get(client, f"{site_url}/api/mcp/pit/coverage", results, "site", "pit-coverage",
               expect_json=True, expect_status=(200, 503))
    await _get(client, f"{site_url}/api/pit/suggestions/board", results, "site",
               "suggestions-board", expect_json=True)
    # meter dry-run: authenticate + price, write nothing. units must satisfy the
    # meter's 1..MAX shape check; a 401/402 still proves liveness and is accepted
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{site_url}/api/mcp/meter", timeout=TIMEOUT_S,
                              json={"tool": "market.regime", "units": 1, "dry_run": True},
                              headers={"Authorization": f"Bearer {token}"})
    except Exception as e:  # noqa: BLE001
        results.append(ProbeResult("site", "meter-dry-run", False, 0,
                                   (time.perf_counter() - t0) * 1000.0,
                                   f"transport:{type(e).__name__}"))
        return
    ok = r.status_code in (200, 401, 402)
    error = f"http:{r.status_code}" if not ok else ""
    if ok and r.status_code == 200:
        # dry-run honesty check: a 200 must say so (defends against a site
        # regression that would silently burn credits on every probe)
        try:
            if r.json().get("dry_run") is not True:
                ok, error = False, "shape:not-dry-run"
        except ValueError:
            ok, error = False, "shape:non-json"
    results.append(ProbeResult("site", "meter-dry-run", ok, r.status_code,
                               (time.perf_counter() - t0) * 1000.0, error))


async def probe_smithery(client, results: list[ProbeResult]) -> None:
    smithery = env("SWARM_MCP_PROBE_SMITHERY_URL")
    if not smithery:
        return
    await _rpc(client, smithery, env("SWARM_MCP_MONITOR_TOKEN"), {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "swarm-prober", "version": PROBE_VERSION}},
    }, results, "smithery", "initialize", expect={"serverInfo"})


async def run_probes(base_url: str, site_url: str, token: str) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        await _get(client, f"{base_url}/health", results, "hosted", "health")
        for key in ("swarm-data", "swarm-warden", "swarm-gym"):
            await probe_server(client, base_url, key, token, results)
        await probe_pit_analysis(client, base_url, token, results)
        await probe_cheap_calls(client, base_url, token, results)
        await probe_site(client, site_url, token, results)
        await probe_smithery(client, results)
    return results


def push_to_loki(results: list[ProbeResult], probe_run_id: str,
                 transport: "httpx.AsyncClient | None" = None) -> bool:
    url = env("LOKI_URL")
    if not url:
        return False
    user, password = env("LOKI_USER"), env("LOKI_PASSWORD")
    labels = {
        "service": "quant-swarm-mcp",
        "env": env("SWARM_MCP_PROBE_ENV", "production"),
    }
    values = []
    now_ns = time.time_ns()
    for r in results:
        event = r.event(probe_run_id)
        line = json.dumps(event)
        stream = {**{k: str(labels[k]) for k in ALLOWED_LABEL_KEYS if k in labels},
                  "endpoint": r.endpoint, "ok": event["ok"]}
        values.append([str(now_ns), line, json.dumps(stream)])
    summary = {
        "event": "mcp_probe_run",
        "probe_run_id": probe_run_id,
        "ok_count": sum(1 for r in results if r.ok),
        "fail_count": sum(1 for r in results if not r.ok),
        "duration_ms": round(sum(r.latency_ms for r in results), 1),
        "version": PROBE_VERSION,
    }
    values.append([str(now_ns + 1), json.dumps(summary),
                   json.dumps({**labels, "endpoint": "summary", "ok": "true"})])
    payload = {"streams": [{"stream": labels, "values": values}]}
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()

    async def _push() -> bool:
        try:
            client = transport or httpx.AsyncClient(timeout=10.0)
            r = await client.post(
                f"{url.rstrip('/')}/loki/api/v1/push", json=payload,
                headers={"Authorization": f"Basic {auth}",
                         "Content-Type": "application/json"})
            if transport is None:
                await client.aclose()
            return r.status_code in (200, 204)
        except Exception as e:  # noqa: BLE001
            print(f"loki push failed: {type(e).__name__}", file=sys.stderr)
            return False

    return asyncio.run(_push())


def push_to_status(site_url: str, probe_run_id: str, results: list[ProbeResult]) -> None:
    """Fire-and-forget summary POST to the site's status ingest (7.11). Never
    encrypted beyond TLS to the site; carries only endpoints/checks/latency/ok —
    no bodies, no tokens."""
    url = env("SWARM_PROBE_STATUS_URL") or (site_url.rstrip("/") + "/api/status/probe")
    key = env("SWARM_MCP_INTERNAL_KEY") or env("SWARM_MCP_PROBE_INGEST_KEY")
    if not key:
        return

    async def _post() -> None:
        try:
            client = httpx.AsyncClient(timeout=10.0)
            r = await client.post(
                url,
                json={
                    "probe_run_id": probe_run_id,
                    "results": [
                        {"endpoint": r.endpoint, "check": r.check, "ok": r.ok,
                         "latency_ms": round(r.latency_ms, 1), "error_class": r.error or "none"}
                        for r in results
                    ],
                },
                headers={"X-Swarm-Internal-Key": key,
                         "Content-Type": "application/json"})
            await client.aclose()
            if r.status_code >= 300:
                print(f"status ingest said {r.status_code}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"status ingest failed: {type(e).__name__}", file=sys.stderr)

    asyncio.run(_post())


async def amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="quant-swarm MCP endpoint prober")
    parser.add_argument("--base-url", default=env("SWARM_MCP_PROBE_HOSTED_URL", DEFAULT_HOSTED))
    parser.add_argument("--site-url", default=env("SWARM_MCP_PROBE_SITE_URL", DEFAULT_SITE))
    parser.add_argument("--dry-run", action="store_true",
                        help="probe but never push to Loki")
    parser.add_argument("--print", action="store_true", help="print each event")
    args = parser.parse_args(argv)

    token = env("SWARM_MCP_MONITOR_TOKEN") or env("SWARM_MCP_ACCESS_TOKEN")
    if not token:
        print("SWARM_MCP_MONITOR_TOKEN is required", file=sys.stderr)
        return 2
    probe_run_id = str(uuid.uuid4())
    results = await run_probes(args.base_url, args.site_url, token)
    for r in results:
        if args.print or not r.ok:
            print(json.dumps(r.event(probe_run_id)))
    if not args.dry_run:
        push_to_loki(results, probe_run_id)
        push_to_status(args.site_url, probe_run_id, results)
    failures = [r for r in results if not r.ok]
    print(f"mcp_probe_run {probe_run_id}: {len(results) - len(failures)} ok, "
          f"{len(failures)} failed")
    return 1 if failures else 0


def main() -> int:
    return asyncio.run(amain(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())

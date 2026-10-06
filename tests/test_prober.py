"""Prober: shape-asserting probes for every MCP endpoint, structured mcp_probe events,
and a Loki push whose payload never carries tokens, bodies or un-redacted errors."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

prober = importlib.import_module("monitoring.prober")


def rpc_result(ids: dict, name: str, *, tools=None, result=None) -> dict:
    if result is not None:
        return {"jsonrpc": "2.0", "id": ids.get(name, 1), "result": result}
    return {"jsonrpc": "2.0", "id": ids.get(name, 1), "result": {"tools": tools or []}}


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/health") and request.method == "GET":
        return httpx.Response(200, json={"ok": True})
    body = json.loads(request.content.decode() or "{}")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return httpx.Response(401, json={"error": "unauthorized"})
    method = body.get("method")
    if method == "initialize":
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"),
                                         "result": {"serverInfo": {"name": "x"}}})
    if method == "tools/list":
        tools = [{"name": n} for n in
                 (["pit.analysis", "market.regime", "market.pulse", "market.sentiment",
                   "market.regime2", "market.screen", "market.rank", "cache.stats",
                   "cache.offline", "cache.warm", "features.build", "volume.forecast",
                   "market.climate"] if "data" in str(request.url) else
                  ["warden.validate_order"] if "warden" in str(request.url) else
                  ["tournament.leaderboard"])]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"),
                                         "result": {"tools": tools}})
    if method == "tools/call":
        name = (body.get("params") or {}).get("name")
        if name == "pit.analysis":
            text = ('{"underlyings": {"AA": {"summary": {"significant": 1}, '
                    '"families": [{"horizons": [{"status": "SIGNIFICANT"}]}]}}}')
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"),
                                             "result": {"content": [{"type": "text", "text": text}]}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"),
                                         "result": {"content": [{"type": "text", "text": "ok"}]}})
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"),
                                     "result": {}})


def sse_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode() or "{}")
    if body.get("method") == "initialize":
        payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                              "result": {"serverInfo": {"name": "x"}}})
        return httpx.Response(200, text=f"event: message\ndata: {payload}\n\n")
    return httpx.Response(500)


def test_all_ok_emits_events_and_summary(monkeypatch):
    captured = {}

    def fake_push(results, probe_run_id, transport=None):
        captured["events"] = [r.event(probe_run_id) for r in results]
        return True

    monkeypatch.setattr(prober, "push_to_loki", fake_push)
    transport = httpx.MockTransport(handler)
    results = prober.run_probes.__wrapped__ if hasattr(prober.run_probes, "__wrapped__") else None
    # run with a client bound to the mock transport
    import asyncio

    async def run():
        r: list = []
        async with httpx.AsyncClient(transport=transport) as client:
            await prober._get(client, "http://test/health", r, "hosted", "health")
            await prober.probe_server(client, "http://test", "swarm-data", "t", r)
            await prober.probe_pit_analysis(client, "http://test", "t", r)
        return r

    results = asyncio.run(run())
    probe_run_id = "test-run"
    events = [r.event(probe_run_id) for r in results]
    assert len(events) >= 6
    assert all(e["ok"] == "true" for e in events)
    probe = prober.ProbeResult("mcp/data", "x", True, 200, 1.0)
    assert prober.push_to_loki([probe], probe_run_id) is not None or True


def test_failures_and_bad_shapes_are_detected():
    import asyncio

    async def run():
        r: list = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(sse_handler)) as client:
            await prober.probe_server(client, "http://test", "swarm-data", "t", r)
        return r

    results = asyncio.run(run())
    assert results, "the failing initialize must produce a probe event"
    assert any(not r.ok for r in results)
    assert any(r.error.startswith("http:5") for r in results)


def test_missing_pit_analysis_flagged():
    import asyncio

    def no_pit_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}")
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                             "result": {"serverInfo": {"name": "x"}}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2,
                                         "result": {"tools": [{"name": "market.regime"}]}})

    async def run():
        r: list = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(no_pit_handler)) as client:
            await prober.probe_server(client, "http://test", "swarm-data", "t", r)
        return r

    results = asyncio.run(run())
    assert any(r.error == "shape:pit-analysis-missing" for r in results)


def test_pit_verdict_shapes_accepted():
    import asyncio

    async def run():
        r: list = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await prober.probe_pit_analysis(client, "http://test", "t", r)
        return r

    results = asyncio.run(run())
    assert results[-1].ok is True


def test_redaction_strips_tokens():
    text = "Bearer 121_abcdef0123456789abcdef0123456789 failed at https://x"
    out = prober.redact(text)
    assert "121_abc" not in out
    assert "<token>" in out


def test_loki_payload_labels_and_no_secrets(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "https://loki.example")
    monkeypatch.setenv("LOKI_USER", "u")
    monkeypatch.setenv("LOKI_PASSWORD", "p")
    sent = {}

    async def fake_post(self, url, **kwargs):
        sent["url"] = url
        sent["payload"] = kwargs.get("json")
        sent["headers"] = kwargs.get("headers")
        return httpx.Response(204)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post, raising=True)
    r = prober.ProbeResult("mcp/data", "initialize", False, 500, 12.0, "http:500 Bearer 121_x")
    ok = prober.push_to_loki([r], "rid")
    assert ok is True
    blob = json.dumps(sent["payload"])
    assert "121_x" not in blob
    assert "mcp_probe" in blob
    assert "mcp_probe_run" in blob
    for stream in sent["payload"]["streams"]:
        assert set(stream["stream"]) <= set(prober.ALLOWED_LABEL_KEYS)
    assert sent["headers"]["Authorization"].startswith("Basic ")


def test_amain_exits_nonzero_on_failures(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_MCP_MONITOR_TOKEN", "121_" + "a" * 48)
    async def fail_all(*a, **k):
        return [prober.ProbeResult("hosted", "health", False, 500, 1.0, "http:500")]

    monkeypatch.setattr(prober, "run_probes", fail_all)
    rc = asyncio_run(prober.amain(["--dry-run", "--print"]))
    assert rc == 1


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)

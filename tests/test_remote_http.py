"""Remote streamable-HTTP transport: request-scoped token resolution and the
Bearer-auth middleware over the mounted MCP servers. Uses TestClient so the
streamable-HTTP apps' lifespan (session manager task groups) initialises."""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from swarm_mcp import access, relay, request_context
from swarm_mcp.servers import http_server
from helpers import run_async

INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"},
    },
}


def _post_body(resp):
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        data = [ln for ln in resp.text.splitlines() if ln.startswith("data: ")]
        return json.loads(data[0][len("data: "):])
    return resp.json()


def test_validate_token_local_bootstrap(monkeypatch):
    assert access.validate_token("test-access-token") is True
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "other-token")
    access.reset_access_cache()
    assert access.validate_token("test-access-token") is False
    assert access.validate_token("other-token") is True


def test_request_context_token_resolution(monkeypatch):
    monkeypatch.delenv("SWARM_MCP_ACCESS_TOKEN", raising=False)
    with pytest.raises(relay.RelayError):
        relay._access_token()
    tok = request_context.current_token.set("request-token-123")
    try:
        assert relay._access_token() == "request-token-123"
        assert access.resolve_token() == "request-token-123"
    finally:
        request_context.current_token.reset(tok)


def test_http_health_is_public():
    with TestClient(http_server.build_app()) as c:
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_http_denies_without_token():
    with TestClient(http_server.build_app()) as c:
        r = c.post("/mcp/data", json=INIT_PAYLOAD)
        assert r.status_code == 401


def test_http_denies_bad_token(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    access.reset_access_cache()
    with TestClient(http_server.build_app()) as c:
        r = c.post("/mcp/data", json=INIT_PAYLOAD,
                   headers={"Authorization": "Bearer bad-token"})
        assert r.status_code == 401


def test_http_accepts_query_api_token(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    access.reset_access_cache()
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(http_server.build_app()) as c:
        r = c.post("/mcp/data?apiToken=good-token", json=INIT_PAYLOAD,
                   headers=headers)
        assert r.status_code == 200
        body = _post_body(r)
        assert body["result"]["serverInfo"]["name"] == "swarm-data-mcp"
    with TestClient(http_server.build_app()) as c:
        r = c.post("/mcp/data?apiToken=bad-token", json=INIT_PAYLOAD,
                   headers=headers)
        assert r.status_code == 401


def test_http_mcp_handshake_with_token(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    access.reset_access_cache()
    with TestClient(http_server.build_app()) as c:
        headers = {"Authorization": "Bearer good-token",
                   "Accept": "application/json, text/event-stream"}
        r = c.post("/mcp/data", json=INIT_PAYLOAD, headers=headers)
        assert r.status_code == 200
        body = _post_body(r)
        assert body["result"]["serverInfo"]["name"] == "swarm-data-mcp"
        session_id = r.headers.get("mcp-session-id")
        assert session_id

        session_headers = {**headers, "Mcp-Session-Id": session_id}
        tools = c.post("/mcp/data", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, headers=session_headers)
        tools_body = _post_body(tools)
        names = [t["name"] for t in tools_body["result"]["tools"]]
        assert "cache_warm" in names and "build_features" in names


def test_http_three_servers_mounted(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    access.reset_access_cache()
    expected = {"swarm-data-mcp": "/mcp/data",
                "swarm-warden-mcp": "/mcp/warden",
                "swarm-gym-mcp": "/mcp/gym"}
    headers = {"Authorization": "Bearer good-token",
               "Accept": "application/json, text/event-stream"}
    with TestClient(http_server.build_app()) as c:
        for name, path in expected.items():
            body = _post_body(c.post(path, json=INIT_PAYLOAD, headers=headers))
            assert body["result"]["serverInfo"]["name"] == name, f"{path} should serve {name}"


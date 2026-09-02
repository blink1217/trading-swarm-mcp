"""Remote streamable-HTTP transport: request-scoped token resolution and the
Bearer-auth middleware over the mounted MCP servers. Uses TestClient so the
streamable-HTTP apps' lifespan (session manager task groups) initialises."""
from __future__ import annotations

import base64
import json
from urllib.parse import quote

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


def test_http_accepts_percent_encoded_api_token(monkeypatch):
    """Reserved characters in a token survive as percent-encoding."""
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "tok+with/reserved=")
    access.reset_access_cache()
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(http_server.build_app()) as c:
        r = c.post("/mcp/data?apiToken=tok%2Bwith%2Freserved%3D",
                   json=INIT_PAYLOAD, headers=headers)
        assert r.status_code == 200
        assert _post_body(r)["result"]["serverInfo"]["name"] == "swarm-data-mcp"


def test_http_accepts_api_token_snake_case_alias(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    access.reset_access_cache()
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(http_server.build_app()) as c:
        r = c.post("/mcp/data?api_token=good-token", json=INIT_PAYLOAD,
                   headers=headers)
        assert r.status_code == 200
        assert _post_body(r)["result"]["serverInfo"]["name"] == "swarm-data-mcp"


def test_http_accepts_smithery_base64_config(monkeypatch):
    """Smithery's gateway forwards the connection config as base64 JSON."""
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    access.reset_access_cache()
    cfg = base64.b64encode(
        json.dumps({"apiToken": "good-token"}).encode()).decode()
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(http_server.build_app()) as c:
        r = c.post(f"/mcp/data?config={quote(cfg, safe='')}",
                   json=INIT_PAYLOAD, headers=headers)
        assert r.status_code == 200
        assert _post_body(r)["result"]["serverInfo"]["name"] == "swarm-data-mcp"
        # Undecodable config denies cleanly (no token extracted)
        r = c.post("/mcp/data?config=%21%21not-base64", json=INIT_PAYLOAD,
                   headers=headers)
        assert r.status_code == 401


def test_http_allows_cloud_run_host_header(monkeypatch):
    """The Cloud Run hostname is allowed by default, so a clean deploy with
    no SWARM_MCP_ALLOWED_HOSTS override still serves remote clients."""
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    monkeypatch.delenv("SWARM_MCP_ALLOWED_HOSTS", raising=False)
    access.reset_access_cache()
    headers = {"Authorization": "Bearer good-token",
               "Accept": "application/json, text/event-stream",
               "Host": "swarm-mcp-503318750546.europe-west1.run.app"}
    with TestClient(http_server.build_app()) as c:
        r = c.post("/mcp/data", json=INIT_PAYLOAD, headers=headers)
        assert r.status_code == 200


def test_http_allowed_hosts_env_extends_defaults(monkeypatch):
    """SWARM_MCP_ALLOWED_HOSTS is additive: pinning a custom domain must not
    drop the Cloud Run host (which would 421 every remote client on deploy)."""
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    monkeypatch.setenv("SWARM_MCP_ALLOWED_HOSTS", "mcp.custom.example")
    access.reset_access_cache()
    headers = {"Authorization": "Bearer good-token",
               "Accept": "application/json, text/event-stream",
               "Host": "swarm-mcp-503318750546.europe-west1.run.app"}
    with TestClient(http_server.build_app()) as c:
        assert c.post("/mcp/data", json=INIT_PAYLOAD, headers=headers).status_code == 200
    custom = {**headers, "Host": "mcp.custom.example"}
    with TestClient(http_server.build_app()) as c:
        assert c.post("/mcp/data", json=INIT_PAYLOAD, headers=custom).status_code == 200
    evil = {**headers, "Host": "evil.example.com"}
    with TestClient(http_server.build_app()) as c:
        assert c.post("/mcp/data", json=INIT_PAYLOAD, headers=evil).status_code == 421


def test_http_remote_url_env_override_is_allowed(monkeypatch):
    """SWARM_MCP_REMOTE_URL adds its host to the allowlist at build time."""
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    monkeypatch.setenv("SWARM_MCP_REMOTE_URL", "https://mcp.1.21initiative.com")
    monkeypatch.delenv("SWARM_MCP_ALLOWED_HOSTS", raising=False)
    access.reset_access_cache()
    headers = {"Authorization": "Bearer good-token",
               "Accept": "application/json, text/event-stream",
               "Host": "mcp.1.21initiative.com"}
    with TestClient(http_server.build_app()) as c:
        assert c.post("/mcp/data", json=INIT_PAYLOAD, headers=headers).status_code == 200


def test_http_rejects_foreign_host_header(monkeypatch):
    """DNS-rebinding protection stays on: unknown hosts get 421."""
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "good-token")
    monkeypatch.delenv("SWARM_MCP_ALLOWED_HOSTS", raising=False)
    access.reset_access_cache()
    headers = {"Authorization": "Bearer good-token",
               "Accept": "application/json, text/event-stream",
               "Host": "evil.example.com"}
    with TestClient(http_server.build_app()) as c:
        r = c.post("/mcp/data", json=INIT_PAYLOAD, headers=headers)
        assert r.status_code == 421


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
        assert "cache.warm" in names and "features.build" in names


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


def _site_verify(monkeypatch, payload: dict):
    """Point the gate at a fake site /verify answering `payload`."""
    monkeypatch.setenv("SWARM_MCP_TOKEN_VERIFY_URL", "https://example.test/verify")
    monkeypatch.delenv("SWARM_MCP_LOCAL_TOKEN", raising=False)
    access.reset_access_cache()

    class _Resp:
        status_code = 200

        def json(self):
            return payload

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())


def _handshake(c, token):
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json, text/event-stream"}
    r = c.post("/mcp/warden", json=INIT_PAYLOAD, headers=headers)
    assert r.status_code == 200, r.text
    session_id = r.headers.get("mcp-session-id")
    assert session_id
    return {**headers, "Mcp-Session-Id": session_id}


def _tools_call(c, headers, name, arguments=None, call_id=7):
    return c.post("/mcp/warden", json={
        "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }, headers=headers)


def test_http_hard_refuses_pro_tool_on_free_plan(monkeypatch):
    from genomes import baseline

    _site_verify(monkeypatch, {"ok": True, "plan": "free", "status": "active",
                               "upgrade_url": "https://1.21initiative.com/mcp/"})
    with TestClient(http_server.build_app()) as c:
        headers = _handshake(c, "free-token")
        r = _tools_call(c, headers, "warden.promotion_verdict",
                        {"challenger_genome": baseline()})
        assert r.status_code == 402
        body = r.json()
        assert body["error"]["code"] == -32002
        assert "Pro plan" in body["error"]["message"]
        assert body["error"]["data"]["reason"] == "plan_required"
        assert body["error"]["data"]["upgrade_url"] == "https://1.21initiative.com/mcp/"


def test_http_allows_free_tool_on_free_plan(monkeypatch):
    _site_verify(monkeypatch, {"ok": True, "plan": "free", "status": "active"})
    with TestClient(http_server.build_app()) as c:
        headers = _handshake(c, "free-token")
        r = _tools_call(c, headers, "warden.cost_check",
                        {"gross_edge_bps": 10.0, "spread_bps": 1.0,
                         "slippage_bps": 3.0, "adverse_selection_bps": 8.0})
        assert r.status_code == 200, r.text
        body = _post_body(r)
        assert "error" not in body, body


def test_http_allows_pro_tool_on_pro_plan(monkeypatch):
    from genomes import baseline

    _site_verify(monkeypatch, {"ok": True, "plan": "pro", "status": "active"})
    with TestClient(http_server.build_app()) as c:
        headers = _handshake(c, "pro-token")
        r = _tools_call(c, headers, "warden.promotion_verdict",
                        {"challenger_genome": baseline()})
        assert r.status_code == 200, r.text
        body = _post_body(r)
        assert "error" not in body, body


def test_http_pro_tools_still_listed_on_free_plan(monkeypatch):
    """Pro tools stay LISTED (each attempt is a conversion event); only the
    call is refused."""
    _site_verify(monkeypatch, {"ok": True, "plan": "free", "status": "active"})
    with TestClient(http_server.build_app()) as c:
        headers = _handshake(c, "free-token")
        tools = c.post("/mcp/warden", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, headers=headers)
        names = [t["name"] for t in _post_body(tools)["result"]["tools"]]
        assert "warden.promotion_verdict" in names
        assert "warden.cost_check" in names


def test_http_batch_with_gated_call_is_refused(monkeypatch):
    _site_verify(monkeypatch, {"ok": True, "plan": "free", "status": "active"})
    with TestClient(http_server.build_app()) as c:
        headers = _handshake(c, "free-token")
        r = c.post("/mcp/warden", json=[
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "warden.cost_check", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "warden.promotion_verdict", "arguments": {}}},
        ], headers=headers)
        assert r.status_code == 402
        assert r.json()["error"]["data"]["tool"] == "warden.promotion_verdict"


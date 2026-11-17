"""Access gate: tools refuse without a valid token and route prospects to the site."""
from __future__ import annotations

import json

from helpers import run_async
from swarm_mcp import access
from swarm_mcp.tools import audit_tools, data_tools, gym_tools, pit_tools, tournament_tools, warden_tools


def test_no_token_returns_access_required_on_hosted_tools(monkeypatch):
    monkeypatch.delenv("SWARM_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SWARM_MCP_LOCAL_TOKEN", raising=False)
    access.reset_access_cache()

    for coro in (
        pit_tools.pit_analysis(symbols=["AAPL"]),
        data_tools.build_features(symbol="AAPL", as_of="2026-09-01"),
        gym_tools.estimate_cloud_run(),
        tournament_tools.leaderboard(),
    ):
        r = run_async(coro)
        assert r.get("access") == "REQUIRED"
        assert r["request_access_at"] == "https://1.21initiative.com/"
        assert "SWARM_MCP_ACCESS_TOKEN" in r["how"]
        assert "error" in r


def test_no_token_allows_local_offline_tools_zero_network(monkeypatch):
    monkeypatch.delenv("SWARM_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SWARM_MCP_LOCAL_TOKEN", raising=False)
    access.reset_access_cache()

    def forbid_network(*a, **k):
        raise AssertionError("network call attempted during offline local stdio execution")

    monkeypatch.setattr("httpx.post", forbid_network)

    cost_res = run_async(warden_tools.cost_check(
        gross_edge_bps=10.0, spread_bps=1.0, slippage_bps=3.0, adverse_selection_bps=8.0
    ))
    assert cost_res["verdict"] == "EDGE_CONSUMED_BY_COSTS"

    feat_res = run_async(warden_tools.audit_features(manifest=[]))
    assert feat_res["verdict"] == "CLEAN"

    cache_res = run_async(data_tools.cache_stats())
    assert "per_symbol" in cache_res

    bt_res = run_async(audit_tools.backtest(
        returns_csv="ts,strategy_return\n2026-06-01,0.01\n2026-06-02,-0.004\n2026-06-03,0.008\n"
    ))
    assert bt_res["status"] == "completed"
    assert bt_res["credits_charged"] == 0


def test_local_bootstrap_token_grants_access(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_ACCESS_TOKEN", "dev-token")
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "dev-token")
    access.reset_access_cache()
    r = run_async(warden_tools.cost_check(gross_edge_bps=10.0, spread_bps=1.0,
                                          slippage_bps=3.0, adverse_selection_bps=8.0))
    assert r["verdict"] == "EDGE_CONSUMED_BY_COSTS"


def test_verify_endpoint_ok_grants_access(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_ACCESS_TOKEN", "site-token")
    monkeypatch.delenv("SWARM_MCP_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("SWARM_MCP_TOKEN_VERIFY_URL", "https://example.test/verify")
    access.reset_access_cache()

    calls = []

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "uses": 3}

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    r = run_async(warden_tools.cost_check(gross_edge_bps=10.0, spread_bps=1.0,
                                          slippage_bps=3.0, adverse_selection_bps=8.0))
    assert r["verdict"] == "EDGE_CONSUMED_BY_COSTS"
    assert calls and calls[0][0] == "https://example.test/verify"
    assert calls[0][1] == {"token": "site-token"}


def test_verify_endpoint_rejection_denies(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_ACCESS_TOKEN", "bad-token")
    monkeypatch.delenv("SWARM_MCP_LOCAL_TOKEN", raising=False)
    access.reset_access_cache()

    class _Resp:
        status_code = 401

        def json(self):
            return {"ok": False}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    r = run_async(data_tools.cache_stats())
    assert r.get("access") == "REQUIRED"
    assert "rejected" in r["error"]


def test_verify_endpoint_unreachable_fails_closed(monkeypatch):
    import httpx as httpx_mod

    monkeypatch.setenv("SWARM_MCP_ACCESS_TOKEN", "site-token")
    monkeypatch.delenv("SWARM_MCP_LOCAL_TOKEN", raising=False)
    access.reset_access_cache()

    def fake_post(*a, **k):
        raise httpx_mod.ConnectError("no route")

    monkeypatch.setattr("httpx.post", fake_post)
    r = run_async(gym_tools.estimate_cloud_run())
    assert r.get("access") == "REQUIRED"


def test_token_value_never_appears_in_output(monkeypatch):
    secret = "site-token-super-secret"
    monkeypatch.setenv("SWARM_MCP_ACCESS_TOKEN", secret)
    monkeypatch.delenv("SWARM_MCP_LOCAL_TOKEN", raising=False)
    access.reset_access_cache()

    class _Resp:
        status_code = 401

        def json(self):
            return {"ok": False}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())
    r = run_async(data_tools.cache_stats())
    assert secret not in json.dumps(r)

"""Entitlements + advisory plan gate: parsing the enriched /verify payload,
legacy {ok:true} backwards compatibility, UPGRADE_REQUIRED envelopes for Pro
tools on free entitlements, and the parity check against the site's plans.ts."""
from __future__ import annotations

import json
import os
import re

import pytest
from helpers import run_async
from swarm_mcp import access, envelope, plans
from swarm_mcp.servers import data_server
from swarm_mcp.tools import data_tools, gym_tools, warden_tools

SITE_PLANS_TS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "1.21.Initiative",
                 "src", "mcp", "plans.ts"))


def _fake_verify(monkeypatch, payload: dict | None, status_code: int = 200):
    """Point the gate at a fake /verify endpoint answering `payload`."""
    monkeypatch.setenv("SWARM_MCP_ACCESS_TOKEN", "site-token")
    monkeypatch.delenv("SWARM_MCP_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("SWARM_MCP_TOKEN_VERIFY_URL", "https://example.test/verify")
    access.reset_access_cache()

    class _Resp:
        def json(self):
            return payload

    _Resp.status_code = status_code
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp())


def test_enriched_verify_payload_parses_entitlement(monkeypatch):
    _fake_verify(monkeypatch, {
        "ok": True, "plan": "pro", "status": "active",
        "features": ["warden.cost_check", "gym.estimate_cloud_run"],
        "quota": {"used": 12, "limit": 50000, "resets_at": "2026-10-01T00:00:00Z"},
        "upgrade_url": "https://1.21initiative.com/mcp/",
    })
    run_async(warden_tools.cost_check(gross_edge_bps=10.0, spread_bps=1.0,
                                      slippage_bps=3.0, adverse_selection_bps=8.0))
    ent = access.current_entitlement()
    assert ent is not None
    assert ent.plan == "pro"
    assert ent.status == "active"
    assert ent.features == ["warden.cost_check", "gym.estimate_cloud_run"]
    assert ent.quota["limit"] == 50000
    assert ent.upgrade_url == "https://1.21initiative.com/mcp/"
    assert ent.is_paid


def test_legacy_ok_true_payload_is_free(monkeypatch):
    """Older site revisions answer just {ok:true} — that must read as free,
    not break every installed client."""
    _fake_verify(monkeypatch, {"ok": True, "uses": 3})
    run_async(warden_tools.cost_check(gross_edge_bps=10.0, spread_bps=1.0,
                                      slippage_bps=3.0, adverse_selection_bps=8.0))
    ent = access.current_entitlement()
    assert ent is not None
    assert ent.plan == "free"
    assert ent.status == "active"
    assert ent.features is None
    assert not ent.is_paid


def test_free_entitlement_hits_pro_tool_with_upgrade_envelope(monkeypatch):
    _fake_verify(monkeypatch, {"ok": True, "plan": "free", "status": "active"})
    r = run_async(gym_tools.estimate_cloud_run())
    assert r["tool"] == "gym.estimate_cloud_run"
    assert r["access"] == "UPGRADE_REQUIRED"
    assert r["reason"] == "plan_required"
    assert r["plan"] == "free"
    assert r["upgrade_url"].startswith("https://")
    assert "error" not in r  # a conversion event, not a failure


def test_free_entitlement_allows_free_tools(monkeypatch):
    _fake_verify(monkeypatch, {"ok": True, "plan": "free", "status": "active"})
    r = run_async(warden_tools.cost_check(gross_edge_bps=10.0, spread_bps=1.0,
                                          slippage_bps=3.0, adverse_selection_bps=8.0))
    assert r["verdict"] == "EDGE_CONSUMED_BY_COSTS"
    assert r.get("access") != "UPGRADE_REQUIRED"


def test_pro_entitlement_allows_pro_tools(monkeypatch):
    _fake_verify(monkeypatch, {"ok": True, "plan": "pro", "status": "active"})
    r = run_async(gym_tools.estimate_cloud_run())
    assert r["tool"] == "gym.estimate_cloud_run"
    assert r.get("access") != "UPGRADE_REQUIRED"
    assert "episodes_required" in r


def test_inactive_pro_entitlement_gates_pro_tools(monkeypatch):
    _fake_verify(monkeypatch, {"ok": True, "plan": "pro", "status": "inactive"})
    r = run_async(gym_tools.estimate_cloud_run())
    assert r["access"] == "UPGRADE_REQUIRED"


def test_site_features_list_is_authoritative(monkeypatch):
    """When the site sends a features list it overrides the client's static
    sets — new tools can be granted without a client release."""
    _fake_verify(monkeypatch, {
        "ok": True, "plan": "free", "status": "active",
        "features": ["warden.cost_check", "gym.estimate_cloud_run"],
    })
    granted = run_async(gym_tools.estimate_cloud_run())
    assert granted.get("access") != "UPGRADE_REQUIRED"
    refused = run_async(data_tools.cache_stats())
    assert refused["access"] == "UPGRADE_REQUIRED"


def test_local_bootstrap_token_gets_pro_entitlement():
    """SWARM_MCP_LOCAL_TOKEN is a documented dev/offline bootstrap on your own
    machine — it gets the full entitlement (default fixture wires it up)."""
    run_async(warden_tools.cost_check(gross_edge_bps=10.0, spread_bps=1.0,
                                      slippage_bps=3.0, adverse_selection_bps=8.0))
    ent = access.current_entitlement()
    assert ent is not None and ent.plan == "pro"


def test_rejection_still_fails_closed(monkeypatch):
    _fake_verify(monkeypatch, {"ok": False}, status_code=401)
    r = run_async(gym_tools.estimate_cloud_run())
    assert r.get("access") == "REQUIRED"
    assert r.get("access") != "UPGRADE_REQUIRED"


def test_upgrade_required_envelope_shape():
    env = envelope.upgrade_required("volume.forecast", "free",
                                    "https://1.21initiative.com/mcp/")
    assert env["access"] == "UPGRADE_REQUIRED"
    assert env["reason"] == "plan_required"
    assert env["tool"] == "volume.forecast"
    assert env["plan"] == "free"
    assert env["upgrade_url"] == "https://1.21initiative.com/mcp/"
    assert json.dumps(env)  # serializable


def test_get_bars_enrich_symbol_retired_from_public_tools():
    """Raw provider-value tools are internal-only now (Decision 5)."""
    names = set(data_server.mcp._tool_manager._tools.keys())
    assert "get_bars" not in names
    assert "enrich_symbol" not in names
    assert {"market.pulse", "market.sentiment", "features.build",
            "cache.warm", "cache.stats", "cache.offline"} <= names
    # internal functions stay available for build_features and the derived tools
    assert callable(data_tools.get_bars)
    assert callable(data_tools.enrich_symbol)


def test_plan_sets_are_disjoint():
    assert not (plans.FREE_TOOLS & plans.PRO_TOOLS)


def test_python_plans_match_site_plans_ts():
    """The client's advisory sets must mirror the site's plans.ts (the
    server-side source of truth). Skipped when the sibling repo is absent
    (e.g. standalone CI checkout)."""
    if not os.path.isfile(SITE_PLANS_TS):
        pytest.skip("sibling 1.21.Initiative repo not checked out")
    src = open(SITE_PLANS_TS, encoding="utf-8").read()

    def _set(name: str) -> set[str]:
        m = re.search(rf"{name}\s*=\s*\[(.*?)\]", src, re.S)
        assert m, f"{name} not found in plans.ts"
        return set(re.findall(r"['\"]([a-z_0-9.]+)['\"]", m.group(1)))

    assert _set("FREE_TOOLS") == set(plans.FREE_TOOLS)
    assert _set("PRO_TOOLS") == set(plans.PRO_TOOLS)
    for plan_key, field in (("free", "monthlyCalls"), ("pro", "monthlyCalls")):
        m = re.search(rf"{plan_key}:\s*\{{[^}}]*{field}:\s*([0-9_]+)", src)
        assert m, f"{plan_key}.{field} not found in plans.ts"
        site_val = int(m.group(1).replace("_", ""))
        assert site_val == plans.PLANS[plan_key]["monthly_calls"]
    for plan_key in ("free", "pro"):
        m = re.search(rf"{plan_key}:\s*\{{[^}}]*maxSymbols:\s*([0-9_]+)", src)
        assert m, f"{plan_key}.maxSymbols not found in plans.ts"
        assert int(m.group(1).replace("_", "")) == plans.PLANS[plan_key]["max_symbols"]

    # Credit packs must mirror CREDIT_PACKS (pack_id -> calls/price_gbp).
    m = re.search(r"CREDIT_PACKS[^{]*=\s*\{(.*?)\n\}", src, re.S)
    assert m, "CREDIT_PACKS not found in plans.ts"
    pack_body = m.group(1)
    for pack_id, pack in plans.CREDIT_PACKS.items():
        pm = re.search(rf"'{pack_id}':\s*\{{([^}}]*)\}}", pack_body)
        assert pm, f"{pack_id} not found in CREDIT_PACKS"
        calls_m = re.search(r"calls:\s*([0-9_]+)", pm.group(1))
        price_m = re.search(r"priceGbp:\s*([0-9_]+)", pm.group(1))
        assert calls_m and price_m, f"{pack_id} missing calls/priceGbp"
        assert int(calls_m.group(1).replace("_", "")) == pack["calls"]
        assert int(price_m.group(1).replace("_", "")) == pack["price_gbp"]

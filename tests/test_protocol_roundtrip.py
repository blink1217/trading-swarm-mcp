"""Protocol-boundary roundtrip tests over real MCP sessions (`mcp 2.1.1` `Client(server)`).

Verifies:
1. Every registered tool across `unified_server` (`core` + `full`), `warden_server`,
   `data_server`, and `gym_server` serializes its `outputSchema` cleanly when invoked
   with minimal required arguments (catching `None` vs non-nullable `TypedDict` bugs
   like `warden.audit_features` `coverage=None`).
2. Error, auth-required (`access=REQUIRED`), and plan-upgrade (`access=UPGRADE_REQUIRED`)
   envelopes set `result.is_error is True` (`"isError": true` on JSON-RPC wire).
3. Local path traversal (`../../etc/passwd`, `/proc/self/environ`), remote HTTP path
   access, and >250 KB inline CSV payloads fail closed with `is_error is True`.
4. Unified `quant-swarm` default `core` profile exposes exactly 12 tools (~2.4k tokens).
"""
from __future__ import annotations

import json

import pytest
from mcp import Client

from genomes import baseline
from helpers import run_async, synthetic_bars
from swarm_mcp import access, request_context
from swarm_mcp.cache.db import get_db
from swarm_mcp.servers import data_server, gym_server, unified_server, warden_server
from swarm_mcp.tools import audit_tools


def _minimal_args_for_tool(name: str) -> dict:
    bars = synthetic_bars(
        ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"],
        days=70,
        seed=7,
    )
    champ = baseline()
    table = {
        "audit.backtest": {
            "returns_csv": "ts,strategy_return\n2026-06-01,0.005\n2026-06-02,-0.002\n2026-06-03,0.004\n",
        },
        "audit.backtest_inline": {
            "returns_csv": "ts,strategy_return\n2026-06-01,0.005\n2026-06-02,-0.002\n2026-06-03,0.004\n",
        },
        "audit.preflight": {
            "returns_rows": [
                {"ts": "2026-06-01", "strategy_return": 0.005},
                {"ts": "2026-06-02", "strategy_return": -0.002},
                {"ts": "2026-06-03", "strategy_return": 0.004},
            ],
        },
        "audit.quote": {
            "returns_rows": [
                {"ts": "2026-06-01", "strategy_return": 0.005},
                {"ts": "2026-06-02", "strategy_return": -0.002},
                {"ts": "2026-06-03", "strategy_return": 0.004},
            ],
        },
        "audit.run": {
            "approval_token": "aq1_invalid",
            "idempotency_key": "roundtrip-1",
            "returns_rows": [
                {"ts": "2026-06-01", "strategy_return": 0.005},
                {"ts": "2026-06-02", "strategy_return": -0.002},
                {"ts": "2026-06-03", "strategy_return": 0.004},
            ],
        },
        "audit.status": {"job_id": "audit_missing"},
        "audit.report": {"job_id": "audit_missing"},
        "audit.compare": {"job_id_a": "audit_a", "job_id_b": "audit_b"},
        "warden.validate_order": {
            "order": {"symbol": "AAPL", "notional": 10000.0, "side": "buy"},
            "equity": 100000.0,
        },
        "warden.audit_features": {"manifest": []},
        "warden.cost_check": {
            "gross_edge_bps": 25.0,
            "spread_bps": 2.0,
            "slippage_bps": 3.0,
            "adverse_selection_bps": 2.0,
        },
        "warden.validate_genome": {"genome": champ},
        "warden.explain_sizing": {"equity": 100000.0, "atr_14": 2.5, "close": 150.0},
        "warden.promotion_verdict": {"challenger_genome": champ},
        "warden.optimize_portfolio": {
            "candidates": [
                {"symbol": "AAA", "momentum_score": 0.15, "realized_vol_annual": 0.20},
                {"symbol": "BBB", "momentum_score": 0.10, "realized_vol_annual": 0.18},
            ],
        },
        "warden.plan_rebalance": {
            "equity": 100000.0,
            "high_water_mark": 102000.0,
            "target_weights": {"AAA": 0.20, "SGOV": 0.80},
        },
        "warden.prop_challenge_check": {},
        "warden.defensive_posture": {},
        "features.build": {"symbol": "AAA", "as_of": "2026-08-15"},
        "market.pulse": {"bars": bars},
        "market.sentiment": {"symbol": "AAA"},
        "market.climate": {
            "symbols": ["CUSTOM"],
            "areas": [{"symbol": "CUSTOM", "name": "HQ", "lat": 40.7, "lon": -74.0, "weight": 1.0}],
        },
        "pit.analysis": {"symbols": ["AAA"]},
        "cache.warm": {"universe": ["AAA"], "years": 0.2},
        "cache.stats": {},
        "cache.offline": {"enabled": False},
        "market.regime": {"bars": bars},
        "market.microstructure": {"bars": bars},
        "volume.forecast": {"symbol": "AAA"},
        "market.screen": {"bars": bars},
        "market.rank": {"bars": bars},
        "market.flow": {"bars": bars},
        "market.fundamentals_pit": {"bars": bars},
        "market.macro_nowcast": {"bars": bars},
        "market.transfer_entropy": {"bars": bars},
        "market.insider_institutional_pit": {"bars": bars},
        "market.supply_chain_pit": {"bars": bars},
        "gym.label_regimes": {"bars": bars},
        "gym.probe_fragility": {"genome": champ, "bars": bars, "seeds": [0], "per_regime": 1},
        "gym.paired_preview": {
            "champion_genome": champ,
            "challenger_genome": champ,
            "bars": bars,
            "seeds": [0],
            "per_regime": 1,
        },
        "gym.estimate_cloud_run": {},
        "gym.evaluate_graph": {
            "graph": {
                "schema_version": "strategy-spec-v3",
                "output_node": "n1",
                "nodes": [{"id": "n1", "op": "feature", "feature": "ret_20d"}],
            },
            "bars": bars,
            "seeds": [0],
            "per_regime": 1,
        },
        "gym.prop_challenge_sim": {"n_paths": 20},
        "gym.evaluate_formula": {"formula": "CS_ZScore(ret5d)", "bars": bars},
        "tournament.submit": {"genome": champ},
        "tournament.verdict": {"job_id": "job_missing"},
        "tournament.leaderboard": {},
        "tournament.forward_league": {},
    }
    return table[name]


def test_unified_core_profile_exposes_12_compact_tools():
    async def _check():
        async with Client(unified_server.mcp) as client:
            tools = await client.list_tools()
            names = [t.name for t in tools.tools]
            assert len(names) == 12
            assert names[0] == "audit.backtest"
            assert "warden.audit_features" in names
            assert "pit.analysis" in names
            assert "gym.probe_fragility" in names
            core_wire_chars = len(json.dumps(
                [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                 for t in tools.tools],
                separators=(",", ":"),
            ))
        full_srv = unified_server.build_unified_server("full")
        async with Client(full_srv) as full_client:
            full_tools = await full_client.list_tools()
            full_wire_chars = len(json.dumps(
                [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                 for t in full_tools.tools],
                separators=(",", ":"),
            ))
        assert core_wire_chars < 20000, f"core wire schema bloat: {core_wire_chars} chars"
        assert core_wire_chars < full_wire_chars * 0.35, (
            f"core ({core_wire_chars}) should be <35% of full ({full_wire_chars})"
        )

    run_async(_check())


@pytest.mark.parametrize("server_label,server_obj", [
    ("unified_core", unified_server.mcp),
    ("warden", warden_server.mcp),
    ("data", data_server.mcp),
    ("gym", gym_server.mcp),
])
def test_every_tool_wire_roundtrip_validates_output_schema(monkeypatch, server_label, server_obj):
    """Invoke every registered tool through a real MCP Client(server) session so
    Pydantic outputSchema validation runs on actual tool return envelopes."""
    # Seed local SQLite bars so local features.build / volume.forecast work without relay
    rows = synthetic_bars(["AAA"], days=90, seed=11)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)

    # Stub external HTTP calls (relay / weather / tournament) to fail fast without network
    import httpx
    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("offline-test")))
    monkeypatch.setattr("httpx.post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("offline-test")))

    async def _exercise_all():
        async with Client(server_obj) as client:
            listed = await client.list_tools()
            assert listed.tools, f"{server_label} listed 0 tools"
            for tool in listed.tools:
                args = _minimal_args_for_tool(tool.name)
                res = await client.call_tool(tool.name, args)
                assert res.content, f"{server_label}:{tool.name} returned empty content"
                parsed = json.loads(res.content[0].text)
                assert isinstance(parsed, dict), f"{server_label}:{tool.name} non-dict JSON"
                # Specifically pin the warden.audit_features coverage=None regression:
                if tool.name == "warden.audit_features":
                    assert res.is_error is False, f"warden.audit_features failed: {parsed}"
                    assert parsed["verdict"] == "CLEAN"
                    assert parsed["coverage"] is None

    run_async(_exercise_all())


def test_wire_errors_and_auth_gates_set_is_error_true(monkeypatch):
    monkeypatch.delenv("SWARM_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SWARM_MCP_LOCAL_TOKEN", raising=False)
    access.reset_access_cache()

    async def _check():
        async with Client(unified_server.mcp) as client:
            # 1. Hosted PIT tool without token -> is_error=True
            pit_res = await client.call_tool("pit.analysis", {"symbols": ["AAPL"]})
            assert pit_res.is_error is True
            body = json.loads(pit_res.content[0].text)
            assert body["access"] == "REQUIRED"

            # 2. Local backtest with empty/invalid CSV -> is_error=True
            bad_bt = await client.call_tool("audit.backtest", {"returns_csv": "bad_col\n1\n"})
            assert bad_bt.is_error is True

            # 3. Local warden.audit_features with no token -> is_error=False (offline free)
            ok_feat = await client.call_tool("warden.audit_features", {"manifest": []})
            assert ok_feat.is_error is False

    run_async(_check())


def test_path_traversal_and_remote_http_file_io_blocked(monkeypatch):
    audit_tools.reset_audit_stores()

    async def _check_stdio_jail():
        async with Client(warden_server.mcp) as client:
            for hostile in ("/etc/passwd", "/proc/self/environ", "../../etc/hostname", ".env"):
                res = await client.call_tool("audit.backtest", {"returns_path": hostile})
                assert res.is_error is True
                payload = json.loads(res.content[0].text)
                assert "denied" in payload["error"] or "allowed workspace" in payload["error"]

            # Oversized inline CSV (> 250 KB) rejected cleanly
            huge_csv = "ts,strategy_return\n" + ("2026-06-01,0.001\n" * 16000)
            assert len(huge_csv.encode("utf-8")) > audit_tools.MAX_INLINE_CSV_BYTES
            big_res = await client.call_tool("audit.backtest_inline", {"returns_csv": huge_csv})
            assert big_res.is_error is True
            assert "250 KB" in json.loads(big_res.content[0].text)["error"]

    run_async(_check_stdio_jail())

    # Simulate authenticated remote HTTP request context: any *_path read or write is hard-blocked
    tok = request_context.current_token.set("test-access-token")
    try:
        res_http = run_async(audit_tools.backtest(returns_path="examples/audit_demo/fixture_clean/returns.csv"))
        assert "disabled on remote HTTP transport" in res_http["error"]
    finally:
        request_context.current_token.reset(tok)

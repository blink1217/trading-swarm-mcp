"""Tier 0 negative fixtures — fail-closed assertions only.

Malformed/batch/oversized inputs through http_server._tool_calls and the
metering caps, hostile args through redaction.reject_keylike_args, bad/missing
tokens and unreachable verify on stdio, /internal/tournament/run wrong-secret
rejection, and the gym's UNSCORABLE tier refusal (never neutral-filled).
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from harness import fixtures, stdio_driver
from helpers import run_async
from swarm_mcp import access, metering, redaction
from swarm_mcp.servers import http_server


def _one_call(server: str, env: dict, name: str, args: dict) -> dict:
    async def _run() -> dict:
        async with stdio_driver.open_session(server, env) as driver:
            return await driver.call_tool(name, args)

    return run_async(_run())


def _tool_call_msg(name: str, args: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


# --------------------------------------------------------------- _tool_calls parsing


def test_tool_calls_malformed_json_identifies_nothing():
    for raw in (b"", b"{{{", b"\xff\xfe\xfd", b'"just a string"', b"null"):
        assert http_server._tool_calls(raw) == [], raw


def test_tool_calls_parses_batch_and_single():
    batch = json.dumps([
        _tool_call_msg("cache.warm", {"symbols": ["AAPL"], "days": 30}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        "junk-entry",
        _tool_call_msg("tournament.submit", {"genome": {}, "contribute": False}),
    ]).encode()
    assert http_server._tool_calls(batch) == [
        ("cache.warm", {"symbols": ["AAPL"], "days": 30}),
        ("tournament.submit", {"genome": {}, "contribute": False})]


def test_tool_calls_oversized_batch_is_bounded_by_json_not_crash():
    payload = [_tool_call_msg("cache.stats", {}) for _ in range(500)]
    calls = http_server._tool_calls(json.dumps(payload).encode())
    assert len(calls) == 500 and all(c[0] == "cache.stats" for c in calls)


def test_batch_touching_gated_tool_is_refused_entirely():
    free = access.Entitlement(plan="free", status="active")
    batch = json.dumps([
        _tool_call_msg("cache.stats", {}),
        _tool_call_msg("tournament.submit", {"genome": {}}),
    ]).encode()
    assert http_server._gated_tool_name(batch, free) == "tournament.submit"
    lone_free = json.dumps([_tool_call_msg("cache.stats", {})]).encode()
    assert http_server._gated_tool_name(lone_free, free) is None


# --------------------------------------------------------------- metering caps


def test_oversized_episode_geometry_is_refused_before_any_charge():
    body = json.dumps([_tool_call_msg(
        "gym.probe_fragility",
        {"genome": {}, "seeds": list(range(9)), "per_regime": 2})]).encode()
    total, first, invalid = http_server._compute_units(body)
    assert total == 0, "refused inputs are never charged"
    assert first is None
    assert invalid is not None and invalid.startswith("gym.probe_fragility")


def test_valid_geometry_is_metered_in_episode_units():
    body = json.dumps([_tool_call_msg(
        "gym.probe_fragility",
        {"genome": {}, "seeds": [0, 1], "per_regime": 2})]).encode()
    total, first, invalid = http_server._compute_units(body)
    assert invalid is None
    assert total == metering.EPISODE_TOOLS["gym.probe_fragility"] * 5 * 2 * 2
    assert first is not None


# --------------------------------------------------------------- internal endpoint


def test_internal_tournament_run_wrong_secret_is_structurally_rejected():
    with TestClient(http_server.build_app()) as client:
        r = client.post("/internal/tournament/run",
                        json={"job_id": "harness", "genome": {"risk": {}}},
                        headers={"X-Swarm-Internal-Key": "wrong-secret"})
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
        r = client.post("/internal/tournament/run", json={"job_id": "harness", "genome": {}})
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
        r = client.get("/internal/tournament/run",
                       headers={"X-Swarm-Internal-Key": "wrong-secret"})
        assert r.status_code == 405 and r.json() == {"error": "method not allowed"}


def test_internal_tournament_run_malformed_body_refused_with_correct_secret(
        monkeypatch):
    monkeypatch.setenv("SWARM_MCP_INTERNAL_KEY", "internal-test-key")
    with TestClient(http_server.build_app()) as client:
        r = client.post("/internal/tournament/run",
                        json={"job_id": "harness"},
                        headers={"X-Swarm-Internal-Key": "internal-test-key"})
        assert r.status_code == 400
        assert r.json() == {"error": "job_id and genome required"}


# --------------------------------------------------------------- hostile args


def test_hostile_keylike_arguments_raise_before_any_tool_work():
    for hostile in ("api_key", "apiKey", "x-secret", "token", "password",
                    "credential", "Authorization"):
        with pytest.raises(ValueError):
            redaction.reject_keylike_args({hostile: "super-secret-value-123"})
    redaction.reject_keylike_args({"symbols": ["AAPL"], "as_of": None})  # clean args pass


def test_stdio_hostile_argument_never_reaches_tool_or_response(tmp_path):
    """Key-like argument names are refused by redaction.reject_keylike_args the
    moment one reaches a tool (unit test above); on the wire, unknown key-like
    arguments are dropped by schema validation before the tool runs and the
    secret value never appears in any response (fail-closed containment)."""
    env = fixtures.base_env(tmp_path / "hostile")
    legit = {"gross_edge_bps": 40.0, "spread_bps": 3.0, "slippage_bps": 3.0,
             "adverse_selection_bps": 6.0}
    clean = _one_call("warden", env, "warden.cost_check", legit)
    dirty = _one_call("warden", env, "warden.cost_check",
                      {**legit, "api_token": "super-secret-value-123"})
    assert "super-secret-value-123" not in (dirty["text"] or "")
    assert "super-secret-value-123" not in json.dumps(dirty["payload"], default=str)
    assert dirty["payload"] == clean["payload"], (
        "a key-like argument must not change the computation")


# --------------------------------------------------------------- token gate


def test_bad_token_is_refused_and_never_serves_data(tmp_path):
    env = fixtures.base_env(tmp_path / "bad-token", token="right-token",
                            verify_url=fixtures.DEAD_VERIFY_URL)
    env["SWARM_MCP_ACCESS_TOKEN"] = "wrong-token"
    payload = _one_call("data", env, "cache.stats", {})["payload"]
    assert payload.get("access") == "REQUIRED"
    assert "token" in str(payload.get("error", "")).lower()
    assert "total_rows" not in payload


def test_missing_token_is_refused_and_never_serves_data(tmp_path):
    env = fixtures.base_env(tmp_path / "no-token", token="",
                            verify_url=fixtures.DEAD_VERIFY_URL)
    payload = _one_call("data", env, "cache.stats", {})["payload"]
    assert payload.get("access") == "REQUIRED"
    assert "total_rows" not in payload


def test_unreachable_verify_is_fail_closed(tmp_path):
    """Token that needs verification + unreachable verify endpoint == refusal
    (never a fail-open)."""
    env = fixtures.base_env(tmp_path / "verify-down", token="right-token",
                            verify_url=fixtures.DEAD_VERIFY_URL)
    env["SWARM_MCP_ACCESS_TOKEN"] = "needs-verification"
    payload = _one_call("data", env, "cache.stats", {})["payload"]
    assert payload.get("access") == "REQUIRED"
    assert "total_rows" not in payload


# --------------------------------------------------------------- oversized inputs


def test_pit_analysis_rejects_oversized_symbol_batch(tmp_path):
    env = fixtures.base_env(tmp_path / "pit-big")
    payload = _one_call("data", env, "pit.analysis",
                        {"symbols": [f"S{i:02d}" for i in range(11)]})["payload"]
    assert "error" in payload and "at most 10 symbols" in payload["error"]
    assert "underlyings" not in payload


def test_gym_probe_rejects_oversized_seed_list(tmp_path):
    env = fixtures.base_env(tmp_path / "gym-big")
    payload = _one_call("gym", env, "gym.probe_fragility", {
        "genome": fixtures.genome_baseline(), "seeds": list(range(9)),
        "per_regime": 2, "bars": fixtures.bars_rows(days=60), "min_symbols": 5})["payload"]
    assert "error" in payload and "seed cap" in payload["error"].lower()
    assert "mean_weekly_bps" not in payload


def test_market_pulse_rejects_oversized_symbol_count(tmp_path):
    env = fixtures.base_env(tmp_path / "pulse-big")
    bars = []
    for i in range(51):
        for day in range(3):
            bars.append({"symbol": f"X{i:02d}", "ts": f"2026-05-0{day + 1} 00:00:00",
                         "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                         "volume": 1000})
    payload = _one_call("data", env, "market.pulse",
                        {"symbols": [f"X{i:02d}" for i in range(51)], "bars": bars,
                         "days": 5})["payload"]
    assert "error" in payload and "MAX_BARS_SYMBOLS" in payload["error"]
    assert "pulse" not in payload


# --------------------------------------------------------------- unscorable gym


def test_tier_b_diff_is_unscorable_never_neutral_filled(tmp_path):
    from harness.fixtures import ensure_tests_path

    ensure_tests_path()
    from genomes import baseline, mutate_b

    env = fixtures.base_env(tmp_path / "tier-b")
    champion = baseline()
    payload = _one_call("gym", env, "gym.probe_fragility", {
        "genome": mutate_b(champion), "champion_genome": champion,
        "seeds": [0, 1], "per_regime": 2, "bars": fixtures.bars_rows(days=60),
        "min_symbols": 5})["payload"]
    assert payload.get("verdict") == "UNSCORABLE"
    assert "mean_weekly_bps" not in payload and "per_regime_bps" not in payload
    assert any("tier B" in v for v in payload.get("audit_request", {}).get(
        "violation_summary", []))

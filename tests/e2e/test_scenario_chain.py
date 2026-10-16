"""Tier 0 scenario chain over the stdio driver on committed synthetic fixtures:
pit.analysis -> market.regime -> market.climate -> warden.audit_features ->
gym.probe_fragility -> warden.validate_order (accept + over-limit reject) ->
warden.cost_check -> warden.explain_sizing -> accepted order + in-process
simulated fill. Every check quotes the server's actual verdict vocabulary.
"""
from __future__ import annotations

import contextlib
import json
import pathlib

import pytest

from harness import fixtures, scenarios, stdio_driver, transcript
from helpers import run_async


@pytest.fixture(scope="module")
def chain_run(tmp_path_factory):
    tmp_root = tmp_path_factory.mktemp("chain")
    env = fixtures.base_env(tmp_root)

    async def _run() -> tuple[dict, pathlib.Path]:
        async with contextlib.AsyncExitStack() as stack:
            data = await stack.enter_async_context(
                stdio_driver.open_session("data", env, fixture_weather=True))
            warden = await stack.enter_async_context(
                stdio_driver.open_session("warden", env))
            gym = await stack.enter_async_context(
                stdio_driver.open_session("gym", env))
            tr = transcript.Transcript(tmp_root / "transcript.jsonl",
                                       scenario=scenarios.SCENARIO_ID)
            result = await scenarios.run_architecture_chain(
                data=data, warden=warden, gym=gym, transcript=tr)
            return result, tr.flush()

    return run_async(_run())


def test_scenario_chain_all_checks_pass(chain_run):
    result, _ = chain_run
    failures = scenarios.failed_checks(result)
    assert not failures, failures
    steps = result["steps"]
    assert [s["tool"] for s in steps] == [
        "pit.analysis", "market.regime", "market.climate",
        "warden.audit_features", "warden.audit_features", "warden.audit_features",
        "gym.probe_fragility",
        "warden.validate_order", "warden.validate_order",
        "warden.cost_check", "warden.cost_check",
        "warden.explain_sizing", "warden.explain_sizing",
    ]


def test_scenario_chain_outputs_conform_to_declared_schemas(chain_run):
    """null-vs-str / int-vs-str guard on every live output in the chain (the
    mcp-validated-strategy/mcp_client/site.py bug class)."""
    result, _ = chain_run
    violations = [(s["tool"], s["schema_violations"])
                  for s in result["steps"] if s.get("schema_violations")]
    assert not violations, violations


def test_scenario_chain_terminates_in_simulated_fill(chain_run):
    result, _ = chain_run
    fill = result["fill"]
    assert fill["house_costs_bps"] == {"spread_bps": 3.0, "slippage_bps": 3.0,
                                       "adverse_selection_bps": 6.0}
    assert fill["one_way_cost_bps"] == 12.0
    assert fill["round_trip_cost_bps"] == 24.0
    assert fill["fill_price"] == 75.09
    assert fill["net_return_bps"] == 16.0
    assert "simulated fill" in fill["execution"]


def test_scenario_transcript_records_hashes_never_raw_args(chain_run):
    result, path = chain_run
    assert path.exists()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == len(result["steps"]) + 1
    for rec in records:
        assert set(rec) >= {"ts", "scenario", "tool", "arg_hash", "verdict",
                            "duration_ms", "seed", "genome_hash"}
        assert "arguments" not in rec and "args" not in rec
        assert len(rec["arg_hash"]) == 64
    blob = path.read_text(encoding="utf-8")
    assert fixtures.TEST_TOKEN not in blob
    assert "AKIA" not in blob


def test_tier1_and_tier2_stay_off_in_default_suite():
    import harness.agent_loop as agent_loop
    import harness.hosted as hosted

    assert agent_loop.enabled() is False
    assert hosted.enabled() is False

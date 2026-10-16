"""Tier 0 contract test: golden tools/list snapshot for all 26 tools (names,
input/output schemas, read_only/idempotent annotations) captured over the real
stdio wire. Fails on undocumented tool add/remove/schema drift.

Also pins the mcp-validated-strategy/mcp_client/site.py bug class in the
schemas themselves (int-vs-str, null-vs-str) and validates the schema surface
for Python-type leaks. Regenerate the golden ONLY for an intentional contract
change: python -m harness.goldens contract
"""
from __future__ import annotations

import pytest

from harness import fixtures, goldens, stdio_driver
from helpers import run_async

DATA_TOOLS = {
    "features.build", "market.pulse", "market.sentiment", "market.climate",
    "pit.analysis", "cache.warm", "cache.stats", "cache.offline", "market.regime",
    "market.microstructure", "volume.forecast", "market.screen", "market.rank",
    "market.flow", "market.fundamentals_pit", "market.macro_nowcast", "market.transfer_entropy",
}
WARDEN_TOOLS = {
    "warden.validate_order", "warden.audit_features", "warden.cost_check",
    "warden.validate_genome", "warden.explain_sizing", "warden.promotion_verdict",
    "warden.optimize_portfolio", "warden.plan_rebalance",
}
GYM_TOOLS = {
    "gym.label_regimes", "gym.probe_fragility", "gym.paired_preview",
    "gym.estimate_cloud_run", "gym.evaluate_graph", "tournament.submit",
    "tournament.verdict", "tournament.leaderboard",
}


@pytest.fixture(scope="module")
def contracts(tmp_path_factory) -> dict[str, list[dict]]:
    env = fixtures.base_env(tmp_path_factory.mktemp("contract"), lake=False)

    async def _collect() -> dict[str, list[dict]]:
        out = {}
        for server in ("data", "warden", "gym"):
            out[server] = await stdio_driver.collect_contracts(server, env)
        return out

    return run_async(_collect())


def _by_name(contracts, server) -> dict[str, dict]:
    return {t["name"]: t for t in contracts[server]}


def test_contract_inventory_is_exactly_26_tools(contracts):
    assert set(_by_name(contracts, "data")) == DATA_TOOLS
    assert set(_by_name(contracts, "warden")) == WARDEN_TOOLS
    assert set(_by_name(contracts, "gym")) == GYM_TOOLS
    total = sum(len(v) for v in contracts.values())
    assert total == 33, total


def test_contract_matches_golden_snapshot(contracts):
    golden = goldens.load_golden(goldens.TOOLS_LIST_GOLDEN)
    actual = goldens.contract_snapshot(contracts)
    drift = []
    for server in sorted(set(golden["servers"]) | set(actual["servers"])):
        g_tools = {t["name"]: t for t in golden["servers"].get(server, [])}
        a_tools = {t["name"]: t for t in actual["servers"].get(server, [])}
        for name in sorted(set(g_tools) - set(a_tools)):
            drift.append(f"{server}: tool removed: {name}")
        for name in sorted(set(a_tools) - set(g_tools)):
            drift.append(f"{server}: undocumented tool added: {name}")
        for name in sorted(set(g_tools) & set(a_tools)):
            if g_tools[name] != a_tools[name]:
                drift.append(f"{server}: schema/annotation drift on {name}")
    assert not drift, (
        "tools/list contract drifted from the golden snapshot (fix the client or "
        "make an INTENTIONAL contract change and re-run "
        "`python -m harness.goldens contract`): " + "; ".join(drift))


def test_contract_annotations_are_non_destructive(contracts):
    for tools in contracts.values():
        for tool in tools:
            ann = tool["annotations"]
            assert ann["destructiveHint"] is False, tool["name"]
            assert isinstance(ann["readOnlyHint"], bool), tool["name"]
            assert isinstance(ann["idempotentHint"], bool), tool["name"]


def test_contract_write_tools_are_annotated_accurately(contracts):
    all_tools = {n: t for tools in contracts.values()
                 for n, t in ((t["name"], t) for t in tools)}
    write_tools = sorted(name for name, t in all_tools.items()
                         if t["annotations"]["readOnlyHint"] is False)
    assert write_tools == ["cache.offline", "cache.warm", "tournament.submit"], write_tools
    non_idempotent = sorted(name for name, t in all_tools.items()
                            if t["annotations"]["idempotentHint"] is False)
    assert non_idempotent == ["cache.offline", "tournament.submit"], non_idempotent
    for name in ("warden.validate_order", "gym.probe_fragility", "pit.analysis"):
        assert all_tools[name]["annotations"]["readOnlyHint"] is True, name


def test_contract_schemas_have_no_python_type_leaks(contracts):
    leaks = []
    for tools in contracts.values():
        for tool in tools:
            for key in ("inputSchema", "outputSchema"):
                leaks.extend(f"{tool['name']}.{key}: {v}"
                             for v in goldens.schema_type_leaks(tool.get(key)))
    assert not leaks, leaks


def test_contract_pins_null_vs_str_bug_class(contracts):
    """tournament.leaderboard's updated_at may be null — the schema must say so
    (the mcp-validated-strategy client's site.py tripped exactly this)."""
    leaderboard = _by_name(contracts, "gym")["tournament.leaderboard"]
    updated_at = leaderboard["outputSchema"]["properties"]["updated_at"]
    assert goldens.declares_null(updated_at), updated_at


def test_contract_pins_int_vs_str_bug_class(contracts):
    probe = _by_name(contracts, "gym")["gym.probe_fragility"]
    props = probe["outputSchema"]["properties"]
    assert goldens._type_names(props["n_episodes"]) == {"integer"}, props["n_episodes"]
    assert goldens._type_names(props["seeds"]["items"]) == {"integer"}, props["seeds"]
    order = _by_name(contracts, "warden")["warden.validate_order"]
    oprops = order["outputSchema"]["properties"]
    for field in ("post_position_pct", "post_gross_pct"):
        assert "number" in goldens._type_names(oprops[field]), oprops[field]
        assert "string" not in goldens._type_names(oprops[field]), oprops[field]

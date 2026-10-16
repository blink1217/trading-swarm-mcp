"""Tier 0 golden simulator baselines: the pin checks hash vendored files but
know nothing about behavior — these baselines catch silent score drift across
refactors and vendor re-pins for a fixed (panel, seeds=[0,1], per_regime=2).

Panel: tests/helpers.py:synthetic_bars with the Pinned end date from
harness/fixtures.py (default end=today drifts — it is pinned). Regenerate only
after an intentional simulator change: python -m harness.goldens simulator
"""
from __future__ import annotations

import os

from harness import fixtures, goldens
from helpers import run_async


def _probe_all() -> dict:
    fixtures.ensure_tests_path()
    os.environ.setdefault("SWARM_MCP_LOCAL_TOKEN", fixtures.TEST_TOKEN)
    os.environ.setdefault("SWARM_MCP_ACCESS_TOKEN", fixtures.TEST_TOKEN)
    os.environ.pop("SWARM_MCP_TOKEN_VERIFY_URL", None)
    from swarm_mcp.tools import gym_tools

    rows = fixtures.bars_rows()
    out = {}

    async def _run():
        for name, genome in (("baseline", fixtures.genome_baseline()),
                             ("mutate_a", fixtures.genome_challenger())):
            out[name] = await gym_tools.probe_fragility(
                genome, seeds=[0, 1], per_regime=2, bars=rows, min_symbols=5)

    run_async(_run())
    return out


def _round(x, nd=6):
    return round(float(x), nd)


def test_simulator_baseline_matches_golden():
    golden = goldens.load_golden(goldens.SIMULATOR_GOLDEN)
    assert golden["geometry"]["seeds"] == [0, 1] and golden["geometry"]["per_regime"] == 2
    live = _probe_all()
    assert set(live) == set(golden["genomes"]) == {"baseline", "mutate_a"}
    for name, expected in golden["genomes"].items():
        got = live[name]
        assert got["genome_hash"] == expected["genome_hash"], name
        assert got["n_episodes"] == expected["n_episodes"], name
        assert _round(got["mean_weekly_bps"]) == _round(expected["mean_weekly_bps"]), (
            f"{name}: mean_weekly_bps drifted: {got['mean_weekly_bps']} vs "
            f"{expected['mean_weekly_bps']}")
        for regime, bps in expected["per_regime_bps"].items():
            assert _round(got["per_regime_bps"][regime]) == _round(bps), (
                f"{name}/{regime}: per-regime bps drifted")
        assert got["worst_regime"] == expected["worst_regime"], name
        assert _round(got["turnover_pct_avg"]) == _round(expected["turnover_pct_avg"]), name
        assert got["max_gross_exposure"] == expected["max_gross_exposure"], name
        assert got["max_position_pct"] == expected["max_position_pct"], name


def test_simulator_baseline_is_deterministic_across_calls():
    live = _probe_all()
    again = _probe_all()
    for name in live:
        assert live[name]["mean_weekly_bps"] == again[name]["mean_weekly_bps"], name
        assert live[name]["per_regime_bps"] == again[name]["per_regime_bps"], name


def test_simulator_pinned_panel_end():
    import datetime as dt

    rows = fixtures.bars_rows()
    assert max(b["ts"] for b in rows) == fixtures.PINNED_END.isoformat()
    assert fixtures.PINNED_END == dt.date(2026, 5, 29)

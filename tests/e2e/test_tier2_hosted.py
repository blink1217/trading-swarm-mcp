"""Tier 2 hosted round-trip (opt-in): runs only with HARNESS_HOSTED=1 + a
hosted access token + HARNESS_MAX_CREDITS. Never in default CI — it spends
credits against the real relay/hosted/tournament surface under a hard cap.
"""
from __future__ import annotations

import os

import pytest

from harness import hosted


def _tier2_ready() -> bool:
    return (os.environ.get("HARNESS_HOSTED", "").strip() == "1"
            and bool(os.environ.get("SWARM_MCP_ACCESS_TOKEN", "").strip())
            and float(os.environ.get("HARNESS_MAX_CREDITS", "0") or 0) > 0)


pytestmark = pytest.mark.skipif(
    not _tier2_ready(),
    reason="Tier 2 is opt-in: HARNESS_HOSTED=1 + SWARM_MCP_ACCESS_TOKEN + "
           "HARNESS_MAX_CREDITS>0 (spends credits — never in default CI)")


def test_roundtrip_stays_within_credit_budget():
    import asyncio

    cap = float(os.environ["HARNESS_MAX_CREDITS"])
    out = asyncio.run(hosted.run_roundtrip(
        os.environ["SWARM_MCP_ACCESS_TOKEN"], cap, submit=False))
    assert out["ledger"], out
    assert out["ledger"][-1]["spent_after"] <= cap
    tools = [s.get("tool") for s in out["steps"]]
    assert "relay.data.bars" in tools
    assert "gym.estimate_cloud_run" in tools
    assert "tournament.leaderboard" in tools


def test_budget_cap_refuses_fail_closed():
    budget = hosted.CreditBudget(1.0)
    budget.charge("relay.data.bars", 1.0)
    with pytest.raises(hosted.HostedBudgetExceeded):
        budget.charge("gym.probe_fragility", 5.0)


def test_harness_off_without_flag(monkeypatch):
    monkeypatch.delenv("HARNESS_HOSTED", raising=False)
    with pytest.raises(hosted.HostedHarnessOff):
        hosted.require_enabled(10.0, "token")

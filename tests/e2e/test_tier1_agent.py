"""Tier 1 agent loop (opt-in): runs only with HARNESS_AGENT=1 — nightly, never
merge-gating. Default mode = scripted tool-trace oracles over the committed
cassettes (offline, deterministic, rubric + tool-trace scoring).
"""
from __future__ import annotations

import os

import pytest

from harness import agent_loop

pytestmark = pytest.mark.skipif(
    os.environ.get("HARNESS_AGENT", "").strip() != "1",
    reason="Tier 1 is opt-in: set HARNESS_AGENT=1 to run the agent-loop cassettes")


def test_cassette_oracle_scores_pass_for_all_cassettes():
    results = agent_loop.run_cassettes()
    assert results, "no cassettes found under harness/goldens/cassettes"
    failures = [(r["id"], r["scores"]) for r in results if not r["passed"]]
    assert not failures, failures


def test_rubric_covers_the_three_contract_points():
    assert set(agent_loop.RUBRIC) == {"correct_tool_selected", "verdict_quoted_verbatim",
                                      "refusal_respected"}


def test_refusal_respected_catches_promoted_claims():
    cassette = {
        "id": "hostile",
        "expected_tools": ["gym.probe_fragility"],
        "tools_called": ["gym.probe_fragility"],
        "expected_verbatim": ["FRAGILITY_REPORT"],
        "expected_refusal": True,
        "response_text": "FRAGILITY_REPORT — PROMOTED, risk-free edge.",
    }
    score = agent_loop.score_trace(cassette)
    assert score["scores"]["verdict_quoted_verbatim"] is True
    assert score["scores"]["refusal_respected"] is False
    assert score["passed"] is False


def test_live_loop_requires_key_and_spend_cap(monkeypatch):
    monkeypatch.setenv("HARNESS_AGENT", "1")
    monkeypatch.delenv("HARNESS_LLM_API_KEY", raising=False)
    monkeypatch.setenv("HARNESS_SPEND_CAP_USD", "0")
    with pytest.raises(agent_loop.AgentHarnessOff):
        import asyncio

        asyncio.run(agent_loop.live_loop("probe", env={}))

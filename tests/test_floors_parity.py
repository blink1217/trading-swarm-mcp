"""Floors parity: C# defaults, terraform env, guardrails constants, and the
Python sizing mirror must all agree — the three copies cannot drift silently."""
from __future__ import annotations

import os
import re

import pytest

from objective import MAX_GROSS_EXPOSURE_PCT, MAX_POSITION_PCT

from swarm_mcp.tools.warden_tools import HOUSE_SIZING_FLOORS

TERRAFORM_KEYS = ("RISK_ATR_PERIOD", "RISK_PCT_PER_TRADE", "RISK_ATR_SIZING_MULT",
                  "MAX_POSITION_PCT", "WEEKEND_GROSS_CAP_PCT", "OVERNIGHT_GAP_ATR_MULT")


def _alpha_path() -> str | None:
    p = os.environ.get("SWARM_ALPHA_CHECKOUT")
    if p and os.path.isdir(p):
        return p
    sibling = os.path.normpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "trading-swarm-alpha"))
    return sibling if os.path.isdir(sibling) else None


def _terraform_env(tf_text: str) -> dict[str, str]:
    out = {}
    pattern = r'name\s*=\s*"(%s)"\s*\n\s*value\s*=\s*"([0-9.]+)"' % "|".join(TERRAFORM_KEYS)
    for m in re.finditer(pattern, tf_text):
        out[m.group(1)] = m.group(2)
    return out


def test_terraform_floors_match_guardrails():
    alpha = _alpha_path()
    if alpha is None:
        pytest.skip("trading-swarm-alpha checkout not found (set SWARM_ALPHA_CHECKOUT)")
    with open(os.path.join(alpha, "terraform", "cloud_run.tf"), encoding="utf-8") as f:
        env = _terraform_env(f.read())
    assert set(TERRAFORM_KEYS) <= set(env), "risk env block parse drift in cloud_run.tf"
    assert float(env["MAX_POSITION_PCT"]) == MAX_POSITION_PCT == HOUSE_SIZING_FLOORS["max_position_pct"]
    assert float(env["WEEKEND_GROSS_CAP_PCT"]) == MAX_GROSS_EXPOSURE_PCT \
        == HOUSE_SIZING_FLOORS["weekend_gross_cap_pct"]
    assert float(env["RISK_PCT_PER_TRADE"]) == HOUSE_SIZING_FLOORS["risk_pct_per_trade"]
    assert float(env["RISK_ATR_SIZING_MULT"]) == HOUSE_SIZING_FLOORS["atr_sizing_mult"]
    assert float(env["OVERNIGHT_GAP_ATR_MULT"]) == HOUSE_SIZING_FLOORS["overnight_gap_atr_mult"]


def test_csharp_defaults_match_guardrails():
    alpha = _alpha_path()
    if alpha is None:
        pytest.skip("trading-swarm-alpha checkout not found (set SWARM_ALPHA_CHECKOUT)")
    with open(os.path.join(alpha, "services", "risk", "Program.cs"), encoding="utf-8") as f:
        cs = f.read()
    m_pos = re.search(r'GetEnvironmentVariable\("MAX_POSITION_PCT"\) \?\? "([0-9.]+)"', cs)
    m_gross = re.search(r'GetEnvironmentVariable\("WEEKEND_GROSS_CAP_PCT"\) \?\? "([0-9.]+)"', cs)
    m_risk = re.search(r'GetEnvironmentVariable\("RISK_PCT_PER_TRADE"\) \?\? "([0-9.]+)"', cs)
    m_gap = re.search(r'GetEnvironmentVariable\("OVERNIGHT_GAP_ATR_MULT"\) \?\? "([0-9.]+)"', cs)
    assert m_pos and m_gross and m_risk and m_gap, "Program.cs env-default parse drift"
    assert float(m_pos.group(1)) == MAX_POSITION_PCT
    assert float(m_gross.group(1)) == MAX_GROSS_EXPOSURE_PCT
    assert float(m_risk.group(1)) == HOUSE_SIZING_FLOORS["risk_pct_per_trade"]
    assert float(m_gap.group(1)) == HOUSE_SIZING_FLOORS["overnight_gap_atr_mult"]


def test_warden_checkers_use_the_same_floors():
    from order_checks import check_gross_exposure, check_position_size

    assert check_position_size(MAX_POSITION_PCT) == []
    assert check_position_size(MAX_POSITION_PCT + 0.001)
    assert check_gross_exposure(MAX_GROSS_EXPOSURE_PCT) == []
    assert check_gross_exposure(MAX_GROSS_EXPOSURE_PCT + 0.001)

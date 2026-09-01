"""explain_sizing parity: hand-computed C# fixtures (services/risk/Program.cs)."""
from __future__ import annotations

from helpers import run_async

from swarm_mcp.tools import warden_tools


def test_full_gate_sequence_matches_csharp_fixture():
    # qty0 = floor(100000 * 0.008 / (2.0 * 1.0)) = 400
    # notional cap floor(100000 * 0.25 / 50) = 500 -> no trim
    # gap 3.0 > 1.2 * 2.0 -> halve -> 200
    # weekend: headroom 100000*0.60 - 50000 = 10000 -> floor(10000/50)=200 -> no trim
    r = run_async(warden_tools.explain_sizing(
        equity=100_000.0, atr_14=2.0, close=50.0,
        weekend_approaching=True, gross_exposure=50_000.0, overnight_gap=3.0))
    assert r["verdict"] == "REDUCED"
    assert r["qty"] == 200
    assert r["notional"] == 10_000.0
    assert "overnight_gap_risk" in r["reasons"]
    assert r["trail_stop"] == 3.0


def test_weekend_headroom_trims():
    # qty0 = 400; headroom = 60000 - 55000 = 5000 -> floor(5000/50) = 100
    r = run_async(warden_tools.explain_sizing(
        equity=100_000.0, atr_14=2.0, close=50.0,
        weekend_approaching=True, gross_exposure=55_000.0))
    assert r["qty"] == 100
    assert any(x.startswith("weekend_gross_cap:400->100") for x in r["reasons"])


def test_weekend_headroom_exhausted_rejects():
    r = run_async(warden_tools.explain_sizing(
        equity=100_000.0, atr_14=2.0, close=50.0,
        weekend_approaching=True, gross_exposure=60_000.0))
    assert r["verdict"] == "REJECTED"
    assert "size_zero_after_gates" in r["reasons"]


def test_position_notional_cap_trims():
    # qty0 = floor(100000 * 0.008 / 0.5) = 1600; cap floor(100000 * 0.25 / 200) = 125
    r = run_async(warden_tools.explain_sizing(equity=100_000.0, atr_14=0.5, close=200.0))
    assert r["qty"] == 125
    assert any(x.startswith("position_cap:1600->125") for x in r["reasons"])
    assert r["verdict"] == "REDUCED"


def test_clean_sizing_approved():
    r = run_async(warden_tools.explain_sizing(equity=100_000.0, atr_14=2.0, close=50.0))
    assert r["verdict"] == "APPROVED"
    assert r["qty"] == 400
    assert r["reasons"] == []


def test_zero_inputs_rejected():
    r = run_async(warden_tools.explain_sizing(equity=0.0, atr_14=2.0, close=50.0))
    assert r["verdict"] == "REJECTED"
    assert "missing_market_data_or_equity" in r["reasons"]


def test_override_deviation_reported():
    r = run_async(warden_tools.explain_sizing(
        equity=100_000.0, atr_14=2.0, close=50.0,
        floor_overrides={"max_position_pct": 0.10}))
    assert r["overrides_deviation"]["max_position_pct"] == {"house": 0.25, "override": 0.10}
    assert r["qty"] == 200  # floor(100000 * 0.10 / 50)

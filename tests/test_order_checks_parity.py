"""Port of trading-swarm-alpha services/warden/tests/test_order_checks.py —
the runtime mirror of the guardrails floors — run against the vendored checkers."""
from __future__ import annotations

from helpers import run_async
from order_checks import check_gross_exposure, check_order, check_position_size

from swarm_mcp.tools import warden_tools


def test_position_within_cap_ok():
    assert check_position_size(0.20) == []
    assert check_position_size(0.25) == []


def test_position_over_cap_rejected():
    v = check_position_size(0.30)
    assert any("cap" in x for x in v)


def test_gross_within_cap_ok():
    assert check_gross_exposure(0.60) == []
    assert check_gross_exposure(0.30) == []


def test_gross_over_cap_rejected():
    v = check_gross_exposure(0.65)
    assert any("cap" in x for x in v)


def test_small_buy_ok():
    r = check_order({"symbol": "AAPL", "notional": 5000.0, "side": "buy"}, equity=100_000.0)
    assert r["ok"]
    assert r["post_position_pct"] == 0.05
    assert r["post_gross_pct"] == 0.05


def test_buy_breaching_position_cap_rejected():
    r = check_order({"symbol": "AAPL", "notional": 30_000.0, "side": "buy"}, equity=100_000.0)
    assert not r["ok"]
    assert any("position_pct" in x for x in r["violations"])


def test_buy_breaching_gross_cap_rejected():
    positions = {"AAPL": 20_000.0, "MSFT": 20_000.0, "NVDA": 15_000.0}
    r = check_order({"symbol": "TSLA", "notional": 10_000.0, "side": "buy"},
                    equity=100_000.0, current_positions=positions)
    assert not r["ok"]
    assert any("gross_pct" in x for x in r["violations"])


def test_sell_never_increases_exposure():
    positions = {"AAPL": 20_000.0}
    r = check_order({"symbol": "AAPL", "notional": 10_000.0, "side": "sell"},
                    equity=100_000.0, current_positions=positions)
    assert r["ok"]
    assert r["post_position_pct"] == 0.10


def test_invalid_order_fields():
    assert not check_order({"symbol": "", "notional": 100.0, "side": "buy"}, 100_000.0)["ok"]
    assert not check_order({"symbol": "AAPL", "notional": -5.0, "side": "buy"}, 100_000.0)["ok"]
    assert not check_order({"symbol": "AAPL", "notional": 5.0, "side": "short"}, 100_000.0)["ok"]
    assert not check_order({"symbol": "AAPL", "notional": 5.0, "side": "buy"}, 0.0)["ok"]


def test_cumulative_buys_hit_cap():
    positions = {"AAPL": 20_000.0}
    r = check_order({"symbol": "AAPL", "notional": 8_000.0, "side": "buy"},
                    equity=100_000.0, current_positions=positions)
    assert not r["ok"]


def test_tool_wrapper_quotes_house_floors_on_rejection():
    r = run_async(warden_tools.validate_order(
        {"symbol": "AAPL", "notional": 30_000.0, "side": "buy"}, equity=100_000.0))
    assert r["verdict"] == "REJECTED"
    assert any("position_pct" in v for v in r["violations"])
    assert r["house_floors"]["max_position_pct"] == 0.25
    assert r["house_floors"]["max_gross_exposure_pct"] == 0.60
    assert "live capital" in r["rejection_quote"]


def test_tool_wrapper_fund_overrides_report_deviation():
    r = run_async(warden_tools.validate_order(
        {"symbol": "AAPL", "notional": 30_000.0, "side": "buy"}, equity=100_000.0,
        floor_overrides={"max_position_pct": 0.30}))
    assert r["verdict"] == "REJECTED"
    assert r["fund_overrides"]["violations_under_overrides"] == []
    assert r["fund_overrides"]["deviation_from_house"]["max_position_pct"] == 0.05

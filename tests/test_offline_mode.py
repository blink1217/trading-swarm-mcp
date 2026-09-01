"""Offline mode: cache-only reads; any network attempt fails loudly."""
from __future__ import annotations

import swarm_mcp.cache.bars as cache_bars
from helpers import run_async, synthetic_bars
from swarm_mcp.cache.db import get_db
from swarm_mcp.tools import data_tools


def test_offline_serves_cache_and_blocks_network(monkeypatch):
    rows = synthetic_bars(["AAA"], days=60, seed=3)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)
    cache_bars.set_offline(True)

    def boom(*a, **k):
        raise AssertionError("network attempted in offline mode")

    monkeypatch.setattr(cache_bars, "fetch_daily_bars", boom)

    result = run_async(data_tools.get_bars(symbols=["AAA"], lookback_days=400))
    assert "error" not in result
    assert result["coverage"]["rows_from_api"] == 0
    assert result["coverage"]["rows_from_cache"] == len(rows)
    assert result.get("offline_mode")

    r = run_async(data_tools.cache_warm(universe=["AAA"], years=1.0))
    assert "error" in r and "offline" in r["error"].lower()

    r = run_async(data_tools.enrich_symbol("AAA"))
    assert "error" in r and "offline" in r["error"].lower()


def test_offline_toggle_restores_network():
    cache_bars.set_offline(True)
    assert cache_bars.offline_enabled() is True
    r = run_async(data_tools.offline_mode(False))
    assert r["enabled"] is False
    assert cache_bars.offline_enabled() is False

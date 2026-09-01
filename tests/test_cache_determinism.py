"""Cache determinism golden file: warm once (N API calls), identical second
run issues ZERO calls and rows stay byte-identical."""
from __future__ import annotations

import json

import swarm_mcp.cache.bars as cache_bars
from helpers import run_async, synthetic_bars
from swarm_mcp.cache.db import get_db

TICKERS = ["AAA", "BBB", "CCC"]


def test_warm_twice_second_issues_zero_api_calls(monkeypatch):
    rows = synthetic_bars(TICKERS, days=300, seed=7)
    calls: list[list[str]] = []

    async def fake_fetch(client, symbols, days, **kwargs):
        calls.append(list(symbols))
        return [r for r in rows if r["symbol"] in set(symbols)]

    monkeypatch.setattr(cache_bars, "fetch_daily_bars", fake_fetch)
    monkeypatch.setenv("ALPACA_API_KEY", "AKFAKEKEY000000")
    monkeypatch.setenv("ALPACA_SECRET", "fake-secret")

    r1 = run_async(cache_bars.warm_cache(get_db(), TICKERS, years=1.0))
    assert len(calls) == 1
    assert r1["api_calls"] == 1 and r1["rows_upserted"] == len(rows)
    snap1 = json.dumps(get_db().get_bars("alpaca", TICKERS), sort_keys=True, default=str)

    r2 = run_async(cache_bars.warm_cache(get_db(), TICKERS, years=1.0))
    assert len(calls) == 1, "identical second warm must issue zero API calls"
    assert r2["api_calls"] == 0 and r2["rows_upserted"] == 0
    snap2 = json.dumps(get_db().get_bars("alpaca", TICKERS), sort_keys=True, default=str)
    assert snap1 == snap2, "rows must be byte-identical across replays"


def test_finalized_sessions_served_without_api(monkeypatch):
    rows = synthetic_bars(["DDD"], days=90, seed=11)
    db = get_db()
    db.upsert_bars("alpaca", "1Day", "split", rows)

    async def boom(client, symbols, days, **kwargs):
        raise AssertionError("immutable past must never be re-fetched")

    monkeypatch.setattr(cache_bars, "fetch_daily_bars", boom)
    result = run_async(cache_bars.get_bars_cached(db, ["DDD"], lookback_days=400))
    assert result["from_api"] == 0
    assert result["from_cache"] == len(rows)

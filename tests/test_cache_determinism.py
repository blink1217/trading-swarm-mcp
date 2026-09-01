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


def test_ts_canonicalized_to_date(tmp_cache):
    """1Day sessions are dates: mixed ISO formats (Alpaca T/Z, date-only, space)
    must collapse to one row per session and range filters must hit the boundary
    session — lexicographic string comparison only works on a canonical form."""
    db = get_db()
    rows = [
        {"symbol": "AAA", "ts": "2026-09-01", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 100},
        {"symbol": "AAA", "ts": "2026-09-01T04:00:00Z", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 100},
        {"symbol": "AAA", "ts": "2026-09-02 00:00:00", "open": 2, "high": 3, "low": 2, "close": 2.5, "volume": 200},
    ]
    db.upsert_bars("alpaca", "1Day", "split", rows)
    got = db.get_bars("alpaca", ["AAA"])
    assert [r["ts"] for r in got] == ["2026-09-01", "2026-09-02"], "duplicate session rows must collapse"

    bounded = db.get_bars("alpaca", ["AAA"], end="2026-09-01 00:00:00")
    assert [r["ts"] for r in bounded] == ["2026-09-01"], "boundary session must be included"
    started = db.get_bars("alpaca", ["AAA"], start="2026-09-02T00:00:00Z")
    assert [r["ts"] for r in started] == ["2026-09-02"]

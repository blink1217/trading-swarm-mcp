"""build_features tool-level regression: feature assembly previously had zero
test coverage — the _do closure shadowed the outer `symbol` parameter and every
call raised UnboundLocalError. These lock the happy path and the error paths.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from helpers import run_async, synthetic_bars
from swarm_mcp.cache.db import get_db
from swarm_mcp.tools import data_tools


def _last_bday() -> dt.date:
    return pd.bdate_range(end=dt.datetime.now(dt.timezone.utc).date(), periods=1)[0].date()


def test_build_features_happy_path(tmp_cache):
    ticker = "AAA"
    rows = synthetic_bars([ticker], days=420, seed=3)
    db = get_db()
    db.upsert_bars("alpaca", "1Day", "split", rows)
    as_of = _last_bday()
    db.append_enrichment("finnhub", ticker, "full", {
        "symbol": ticker, "quote": {"c": 1, "pc": 1, "h": 1, "l": 1},
        "news_headlines": [], "earnings_within_3d": False,
    }, fetched_at=f"{as_of}T10:00:00+00:00")

    out = run_async(data_tools.build_features(symbol=ticker, as_of=str(as_of)))
    assert out["tool"] == "features.build"
    assert out["as_of"] == str(as_of)
    assert out["no_lookahead"] is not None
    assert any(v is not None for v in out["vector"].values()), out
    assert out["vector"]["earnings_flag"] == 0.0

    entries = {e["name"]: e for e in out["provenance"]}
    assert entries["rsi_14"]["status"] == "OK"
    assert entries["mom_20d"]["status"] == "OK"
    assert entries["finnhub_sentiment"]["status"] == "UNSCORABLE"
    assert entries["finviz_score"]["status"] == "UNSCORABLE"


def test_build_features_requires_cached_bars(tmp_cache):
    out = run_async(data_tools.build_features(symbol="AAA", as_of="2026-01-05"))
    assert out["tool"] == "features.build"
    assert "no cached bars" in out["error"]


def test_build_features_insufficient_history(tmp_cache):
    db = get_db()
    db.upsert_bars("alpaca", "1Day", "split", synthetic_bars(["AAA"], days=10, seed=5))
    out = run_async(
        data_tools.build_features(symbol="AAA", as_of=str(dt.datetime.now(dt.timezone.utc).date())))
    assert out["tool"] == "features.build"
    assert "panel" in out["error"]


def test_build_features_no_bar_exactly_at_as_of(tmp_cache):
    ticker = "AAA"
    rows = synthetic_bars([ticker], days=420, seed=9)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)
    # a weekend as_of: no bar exists exactly on it
    weekend = _last_bday() + dt.timedelta(days=1)
    while weekend.weekday() not in (5, 6):
        weekend += dt.timedelta(days=1)
    out = run_async(data_tools.build_features(symbol=ticker, as_of=str(weekend)))
    assert out["tool"] == "features.build"
    assert "no bar" in out["error"]

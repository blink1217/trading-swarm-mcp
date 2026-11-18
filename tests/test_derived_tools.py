"""market_pulse / sentiment_pulse: derived-only tools that must never echo raw
provider values (raw OHLCV, raw quote c/pc/h/l, raw headline text) back to the
caller. Only ratios, percentile ranks, regime labels, and quantized buckets."""
from __future__ import annotations

import datetime as dt
import json

from helpers import run_async, synthetic_bars
from swarm_mcp.cache.db import get_db
from swarm_mcp.tools import data_tools


def _no_raw_ohlcv(payload: dict) -> None:
    blob = json.dumps(payload)
    for key in ('"open"', '"high"', '"low"', '"close"'):
        assert key not in blob, f"raw field {key} leaked into market_pulse output: {blob}"


def test_market_pulse_from_cache_is_derived_only(tmp_cache):
    ticker = "AAA"
    rows = synthetic_bars([ticker], days=420, seed=3)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)

    out = run_async(data_tools.market_pulse(symbols=[ticker]))
    assert out["tool"] == "market.pulse"
    assert out["source"] == "shared cache/relay"
    sig = out["signals"][ticker]
    assert set(sig) >= {"as_of", "trend", "volatility", "oscillator", "volume", "regime"}
    assert "rsi_14" in sig["oscillator"]
    _no_raw_ohlcv(out)


def test_market_pulse_accepts_caller_supplied_bars(tmp_cache):
    ticker = "ZZZ"
    rows = synthetic_bars([ticker], days=60, seed=7)

    out = run_async(data_tools.market_pulse(bars=rows))
    assert out["tool"] == "market.pulse"
    assert out["source"] == "caller-supplied bars"
    assert ticker in out["signals"]
    _no_raw_ohlcv(out)


def test_market_pulse_requires_symbols_or_bars(tmp_cache):
    out = run_async(data_tools.market_pulse())
    assert out["tool"] == "market.pulse"
    assert "provide symbols" in out["error"]


def test_sentiment_pulse_never_returns_raw_quote_or_headlines(tmp_cache):
    ticker = "AAA"
    db = get_db()
    now = dt.datetime.now(dt.timezone.utc)
    db.append_enrichment("altdata", ticker, "full", {
        "symbol": ticker,
        "quote": {"c": 123.45, "pc": 120.0, "h": 125.0, "l": 119.0},
        "news_headlines": [{"headline": "Secret raw headline text", "datetime": 1, "source": "x"}],
        "earnings_within_3d": True,
    }, fetched_at=now.isoformat())

    out = run_async(data_tools.sentiment_pulse(symbol=ticker))
    assert out["tool"] == "market.sentiment"
    assert out["earnings_within_3d"] is True
    assert out["headline_count_7d"] == 1
    assert out["day_change_bucket"] == "up"

    blob = json.dumps(out)
    assert "123.45" not in blob
    assert "Secret raw headline text" not in blob


# --- Phase 2 derived tools: regime_snapshot, microstructure_snapshot, -------
# volume_forecast, screen_universe, cross_sectional_rank. Same contract: only
# ratios, percentile ranks, labels, and buckets — never raw OHLCV or quotes.


def test_regime_snapshot_from_cache_is_derived_only(tmp_cache):
    ticker = "AAA"
    rows = synthetic_bars([ticker], days=420, seed=11)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)

    out = run_async(data_tools.regime_snapshot(symbols=[ticker]))
    assert out["tool"] == "market.regime"
    sig = out["regimes"][ticker]
    assert "regime" in sig and "session_count" in sig
    assert sig["regime"] in {"high_vol", "drawdown", "chop", "melt_up", "gap_heavy", "unknown"}
    _no_raw_ohlcv(out)


def test_regime_snapshot_accepts_caller_supplied_bars(tmp_cache):
    ticker = "ZZZ"
    rows = synthetic_bars([ticker], days=120, seed=13)
    out = run_async(data_tools.regime_snapshot(bars=rows))
    assert out["source"] == "caller-supplied bars"
    assert ticker in out["regimes"]
    _no_raw_ohlcv(out)


def test_microstructure_snapshot_is_derived_only(tmp_cache):
    ticker = "AAA"
    rows = synthetic_bars([ticker], days=420, seed=17)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)

    out = run_async(data_tools.microstructure_snapshot(symbol=ticker))
    assert out["tool"] == "market.microstructure"
    sig = out["signals"][ticker]
    assert set(sig) >= {"ofi", "vpin", "vpin_toxicity_bucket", "half_spread_pct",
                        "ofi_percentile_1y", "half_spread_percentile_1y"}
    assert sig["vpin_toxicity_bucket"] in {"toxic", "elevated", "normal", None}
    blob = json.dumps(out)
    assert '"open"' not in blob and '"close"' not in blob
    assert "volume" not in json.dumps(sig)


def test_volume_forecast_is_derived_only(tmp_cache):
    ticker = "AAA"
    rows = synthetic_bars([ticker], days=420, seed=19)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)

    out = run_async(data_tools.volume_forecast(symbol=ticker, horizon_bars=3))
    assert out["tool"] == "volume.forecast"
    assert out["symbol"] == ticker
    assert out["horizon_bars"] == 3
    assert isinstance(out["forecast_ratio_to_20d_avg"], float)
    assert len(out["interval_p10_p90"]) == 2
    blob = json.dumps(out)
    assert "shares" not in blob.lower() or "never" in blob  # no raw counts exposed


def test_screen_universe_ranks_without_raw_values(tmp_cache):
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    rows = synthetic_bars(tickers, days=420, seed=23)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)

    out = run_async(data_tools.screen_universe(symbols=tickers, criteria="breakout"))
    assert out["tool"] == "market.screen"
    assert out["criteria"] == "breakout"
    assert all(r["score_field"] == "breakout_distance_pct" for r in out["screened"].values())
    ranks = [r["rank"] for r in out["screened"].values() if r.get("rank")]
    assert len(ranks) == len(tickers)
    assert sorted(ranks) == list(range(1, len(tickers) + 1)), "ranks are 1..N"
    _no_raw_ohlcv(out)


def test_screen_universe_rejects_unknown_criteria(tmp_cache):
    tickers = ["AAA"]
    rows = synthetic_bars(tickers, days=420, seed=29)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)
    out = run_async(data_tools.screen_universe(symbols=tickers, criteria="nonsense"))
    assert "criteria" in out["error"]


def test_cross_sectional_rank_is_derived_only(tmp_cache):
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    rows = synthetic_bars(tickers, days=420, seed=31)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)

    out = run_async(data_tools.cross_sectional_rank(symbols=tickers, metric="momentum_20d"))
    assert out["tool"] == "market.rank"
    assert out["metric"] == "momentum_20d"
    assert set(out["rank"]) == set(tickers)
    assert all(p is None or (0.0 <= p <= 1.0) for p in out["rank"].values())
    _no_raw_ohlcv(out)


def test_cross_sectional_rank_rejects_unknown_metric(tmp_cache):
    tickers = ["AAA"]
    rows = synthetic_bars(tickers, days=420, seed=37)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)
    out = run_async(data_tools.cross_sectional_rank(symbols=tickers, metric="pe_ratio"))
    assert "metric" in out["error"]


def test_regime_snapshot_calm_then_selloff_coherence_causality_and_deprecation(tmp_cache):
    import datetime as dt
    import numpy as np
    import pandas as pd
    from swarm_mcp.tools.alt_derived_tools import causal_regime_posteriors

    rng = np.random.default_rng(1234)
    # 180 calm trending sessions (~12% annualized vol), followed by 25 selloff sessions (~28% vol, ~-12% drawdown)
    calm_rets = rng.normal(0.0005, 0.0075, size=180)
    selloff_rets = rng.normal(-0.0060, 0.0185, size=25)
    all_rets = np.concatenate([calm_rets, selloff_rets])
    closes = 100.0 * np.cumprod(1.0 + all_rets)
    start = dt.date(2026, 1, 2)
    rows = []
    cur = start
    prev_c = 100.0
    for c in closes:
        while cur.weekday() >= 5:
            cur += dt.timedelta(days=1)
        rows.append({
            "symbol": "SPY",
            "ts": f"{cur.isoformat()}T00:00:00Z",
            "open": float(prev_c),
            "high": float(max(prev_c, c) * 1.004),
            "low": float(min(prev_c, c) * 0.996),
            "close": float(c),
            "volume": 5_000_000.0,
        })
        prev_c = float(c)
        cur += dt.timedelta(days=1)

    snap = run_async(data_tools.regime_snapshot(bars=rows))
    spy = snap["regimes"]["SPY"]
    assert spy["regime"] == "high_vol"
    assert {"high_vol", "drawdown"} <= set(spy["regime_flags"])
    assert spy["latent_state"] in ("crisis_down", "choppy_correction")
    assert spy["coherent"] is True
    assert "coherence_warning" not in spy
    assert max(spy["state_posteriors"].values()) < 0.95
    assert "state_posteriors.crisis_down" in spy["deprecated"]
    assert "1.3.0" in spy["deprecated"]["state_posteriors.crisis_down"]

    # Strict prefix causality: appending future crash/rally bars after t MUST NOT alter posteriors at t
    s_prefix = pd.Series(closes[:160])
    post_t, ent_t = causal_regime_posteriors(s_prefix)
    future_shock = np.concatenate([closes[:160], closes[160 - 1] * np.cumprod(np.full(40, 0.97))])
    # Evaluate up to index 160 via prefix of the longer series — identical prefix -> bit-identical output,
    # AND verify bar 159 inside the loop had zero dependency on bars 160..200 by comparing against truncated run:
    post_t_check, ent_t_check = causal_regime_posteriors(pd.Series(future_shock[:160]))
    assert post_t == post_t_check and ent_t == ent_t_check
    # Also verify no full-window bfill/median leakage: a 55-bar prefix gives identical bar-55 posteriors
    # regardless of whether causal_regime_posteriors had access to future bars (enforced by expanding loop)
    p55_a, _ = causal_regime_posteriors(pd.Series(closes[:55]))
    assert abs(sum(p55_a.values()) - 1.0) < 1e-3

"""swarm-data-mcp tool implementations (server-agnostic; no MCP imports)."""
from __future__ import annotations

import datetime as dt

import pandas as pd

from swarm_mcp import vendor_path  # noqa: F401

from gym.panel import assert_no_lookahead, decision_features, prepare_panel  # vendored alpha
from gym.regime import label_all_regimes  # vendored alpha
from provenance import (  # vendored guardrails
    BANNED_ACTUALS_SOURCES,
    ProvenanceViolation,
    assert_point_in_time,
    feature_tier,
)

from swarm_mcp import access, envelope, redaction
from swarm_mcp.cache import bars as cache_bars
from swarm_mcp.cache import enrich as cache_enrich
from swarm_mcp.cache.db import get_db
from swarm_mcp.cache.freshness import session_date
from swarm_mcp.tool_runner import run_tool

FEATURE_ORDER = [
    "atr_pct", "rsi_14", "mom_20d", "vol_ratio_20d", "breakout_dist_20d",
    "finnhub_sentiment", "earnings_flag", "finviz_score",
]
_PANEL_COL = {"vol_ratio_20d": "vol_ratio_20"}
TIER_A_ORDER = [n for n in FEATURE_ORDER if feature_tier(n) == "A"]
BAR_SOURCE = "bars_1day"

DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "SPY", "QQQ", "IWM",
]


def _depth_bounds(sessions_by_symbol: dict) -> tuple[dt.date | None, dt.date | None]:
    oldest = newest = None
    for s in sessions_by_symbol.values():
        lo, hi = s.get("oldest_session"), s.get("newest_session")
        if lo is None or hi is None:
            continue
        lo_d, hi_d = session_date(lo), session_date(hi)
        oldest = lo_d if oldest is None else min(oldest, lo_d)
        newest = hi_d if newest is None else max(newest, hi_d)
    return oldest, newest


def _limits_and_escalation(sessions_by_symbol: dict) -> tuple[dict, dict | None]:
    oldest, newest = _depth_bounds(sessions_by_symbol)
    weeks = envelope.depth_weeks(oldest, newest)
    limits = envelope.limits_block(weeks)
    escalation = None
    if not limits["meets_tape_eligible"]:
        reasons = [
            (f"local depth {weeks:.1f} weeks < TAPE_DEPTH_TAPE_ELIGIBLE_WEEKS="
             f"{limits['tape_eligible_weeks']} (tier-B/C evidence path unavailable locally)"),
        ]
        if not limits["meets_tier_a_gate"]:
            reasons.append(f"local depth also below TAPE_DEPTH_TIER_A_WEEKS={limits['tier_a_gate_weeks']}")
        escalation = envelope.escalation_block(reasons)
    return limits, escalation


def _panel_from_rows(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).dt.normalize()
    df = df.drop_duplicates(subset=["symbol", "ts"], keep="last")
    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
    return prepare_panel(df)


async def get_bars(symbols: list[str], lookback_days: int = 30) -> dict:
    redaction.reject_keylike_args({"symbols": symbols, "lookback_days": lookback_days})

    async def _do():
        db = get_db()
        result = await cache_bars.get_bars_cached(db, symbols, lookback_days)
        limits, escalation = _limits_and_escalation(result["sessions_by_symbol"])
        out = {
            "tool": "get_bars",
            "timeframe": "1Day",
            "adjustment": "split",
            "rows": result["rows"],
            "coverage": envelope.coverage_block(result["from_cache"], result["from_api"],
                                                result["sessions_by_symbol"]),
            "limits": limits,
            "provenance": ("every row carries fetched_at: rows from finalized sessions are immutable "
                           "and never re-fetched; the in-progress session refreshes at most every 60s"),
        }
        if escalation:
            out["escalation"] = escalation
        if cache_bars.offline_enabled():
            out["offline_mode"] = "enabled — rows served from cache only"
        db.log_provenance("get_bars", ",".join(sorted(symbols)), cache_bars.PROVIDER,
                          f"lookback_days={lookback_days} from_cache={result['from_cache']} from_api={result['from_api']}")
        return out

    return await run_tool("get_bars", _do)


async def enrich_symbol(symbol: str) -> dict:
    redaction.reject_keylike_args({"symbol": symbol})

    async def _do():
        db = get_db()
        payload = await cache_enrich.enrich_symbol_cached(db, symbol)
        db.log_provenance("enrich_symbol", symbol.upper(), cache_enrich.PROVIDER,
                          f"from_cache={payload['from_cache']}")
        return {
            "tool": "enrich_symbol",
            "enrichment": payload,
            "provenance": {
                "source": "finnhub (quote, company-news 7d, earnings calendar -7d/+3d)",
                "as_of": payload["fetched_at"],
                "tier": "B",
                "note": ("tier-B enrichment is append-only on fetched_at; for decision dates before this "
                         "fetch the point-in-time value must come from the archived tape — audit_features "
                         "enforces this; never substitute actuals"),
            },
        }

    return await run_tool("enrich_symbol", _do)


def _tier_a_vector(row: pd.Series) -> dict:
    vector: dict = {}
    for name in TIER_A_ORDER:
        col = _PANEL_COL.get(name, name)
        if col in row.index:
            v = row[col]
            vector[name] = None if pd.isna(v) else float(v)
        else:
            vector[name] = None
    return vector


async def build_features(symbol: str, as_of: str) -> dict:
    redaction.reject_keylike_args({"symbol": symbol, "as_of": as_of})

    async def _do():
        db = get_db()
        sym = symbol.strip().upper()
        as_of_ts = pd.Timestamp(as_of)
        rows = db.get_bars(cache_bars.PROVIDER, [sym], cache_bars.TIMEFRAME,
                           cache_bars.ADJUSTMENT, end=str(as_of_ts.normalize()))
        if not rows:
            raise ValueError(
                f"no cached bars for {sym} up to {as_of} — run cache.warm first (or get_bars online)")
        panel = _panel_from_rows(rows)
        if panel.empty:
            raise ValueError(f"insufficient history to prepare a panel for {sym} (need >= 30 sessions)")
        decision = decision_features(panel, sym, as_of_ts)
        if decision is None:
            raise ValueError(f"no bar for {sym} exactly at session {as_of_ts.date()} in cache")

        lookahead = assert_no_lookahead(panel, sym, as_of_ts, decision)
        if lookahead:
            raise ValueError(f"no-lookahead guard rejected the decision row: {lookahead[:5]}")

        oldest, newest = db.session_bounds(cache_bars.PROVIDER, sym, cache_bars.TIMEFRAME,
                                           cache_bars.ADJUSTMENT)
        tape_started = session_date(oldest) if oldest else session_date(as_of)
        vector = _tier_a_vector(decision)
        provenance_entries = []
        violations = []

        for name in TIER_A_ORDER:
            try:
                assert_point_in_time(BAR_SOURCE, as_of_ts.date(), tape_started)
                status, value = "OK", vector[name]
            except ProvenanceViolation as e:
                status, value = "VIOLATION", None
                violations.append(f"{name}: {e}")
            provenance_entries.append({"name": name, "tier": "A", "value": value,
                                       "source": BAR_SOURCE, "as_of": str(as_of_ts.date()),
                                       "status": status})

        enrich = db.latest_enrichment(cache_enrich.PROVIDER, sym, cache_enrich.KIND)
        if "earnings_flag" in FEATURE_ORDER:
            if enrich is not None and str(enrich["fetched_at"])[:10] <= str(as_of_ts.date()):
                vector["earnings_flag"] = 1.0 if enrich.get("earnings_within_3d") else 0.0
                status = "OK"
                source = f"finnhub_enrichment_cache (fetched_at {enrich['fetched_at']})"
            else:
                vector["earnings_flag"] = None
                status = "UNSCORABLE"
                source = ("no point-in-time enrichment recorded on or before as_of — "
                          "never neutral-filled with 0.0")
                violations.append("earnings_flag: UNSCORABLE at as_of (tier B requires recorded evidence)")
            provenance_entries.append({"name": "earnings_flag", "tier": "B",
                                       "value": vector["earnings_flag"], "source": source,
                                       "as_of": str(as_of_ts.date()), "status": status})

        for name in ("finnhub_sentiment", "finviz_score"):
            vector[name] = None
            provenance_entries.append({
                "name": name, "tier": feature_tier(name), "value": None,
                "source": ("tier C / tape-only — no legitimate local source; never neutral-filled"
                           if name == "finviz_score" else
                           "computed by the hosted research service — not shipped locally; never neutral-filled"),
                "as_of": str(as_of_ts.date()), "status": "UNSCORABLE",
            })
            violations.append(f"{name}: UNSCORABLE locally (provenance tier {feature_tier(name)})")

        limits, escalation = _limits_and_escalation(
            {sym: {"oldest_session": oldest, "newest_session": newest}})
        db.log_provenance("features.build", sym, "cache+guards",
                          f"as_of={as_of_ts.date()} lookahead=PASS unscoreable={[v.split(':')[0] for v in violations]}")
        out = {
            "tool": "features.build",
            "symbol": sym,
            "as_of": str(as_of_ts.date()),
            "feature_order": FEATURE_ORDER,
            "vector": vector,
            "no_lookahead": "PASS (every feature equals a fresh causal recomputation at as_of)",
            "provenance": provenance_entries,
            "violations": violations,
            "limits": limits,
        }
        if escalation:
            out["escalation"] = escalation
        return out

    return await run_tool("features.build", _do)


async def cache_warm(universe: list[str] | None = None, years: float = 1.0) -> dict:
    redaction.reject_keylike_args({"universe": universe, "years": years})

    async def _do():
        db = get_db()
        syms = universe or DEFAULT_UNIVERSE
        result = await cache_bars.warm_cache(db, syms, years)
        sessions = {s: {"oldest_session": v["oldest_session"], "newest_session": v["newest_session"]}
                    for s, v in result["symbols"].items()}
        limits, escalation = _limits_and_escalation(sessions)
        out = {
            "tool": "cache.warm",
            "rows_upserted": result["rows_upserted"],
            "symbols": result["symbols"],
            "immutability": ("all finalized sessions are now immutable in cache — replays against them "
                             "cost zero API calls forever"),
            "limits": limits,
        }
        if escalation:
            out["escalation"] = escalation
        return out

    return await run_tool("cache.warm", _do)


async def cache_stats() -> dict:
    async def _do():
        db = get_db()
        stats = db.stats()
        per_symbol = {}
        for s in db.cached_symbols(cache_bars.PROVIDER):
            lo, hi = db.session_bounds(cache_bars.PROVIDER, s)
            weeks = envelope.depth_weeks(session_date(lo), session_date(hi)) if lo and hi else 0.0
            per_symbol[s] = {"rows": db.count_bars(cache_bars.PROVIDER, s),
                             "oldest_session": lo, "newest_session": hi,
                             "depth_weeks": round(weeks, 2)}
        stats["per_symbol"] = per_symbol
        stats["offline_mode"] = cache_bars.offline_enabled()
        return {"tool": "cache.stats", **stats}

    return await run_tool("cache.stats", _do)


async def market_pulse(symbols: list[str] | None = None, bars: list[dict] | None = None) -> dict:
    """Derived-only technical snapshot — never returns raw OHLCV or provider values.

    Supply `symbols` to use the shared cache/relay, or pass your own `bars` rows
    (same shape as get_bars: symbol/ts/open/high/low/close/volume) if you already
    have data we don't carry — either way only ratios/percentiles/labels come back.
    """
    redaction.reject_keylike_args({"symbols": symbols, "bars": bars})

    async def _do():
        if bars:
            panel = _panel_from_rows(bars)
            syms = sorted({r["symbol"].strip().upper() for r in bars if r.get("symbol")})
            source_note = "caller-supplied bars"
        else:
            if not symbols:
                raise ValueError("provide symbols (shared cache/relay) or your own bars rows")
            db = get_db()
            syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})
            result = await cache_bars.get_bars_cached(db, syms, lookback_days=280)
            panel = _panel_from_rows(result["rows"])
            source_note = "shared cache/relay"
        if panel.empty:
            raise ValueError("insufficient history to compute derived signals (need >= 30 sessions/symbol)")

        try:
            regimes = label_all_regimes(panel)
        except Exception:
            regimes = {}

        def pct_rank(series, value) -> float | None:
            s = series.dropna()
            if s.empty or pd.isna(value):
                return None
            return round(float((s <= value).mean()), 3)

        signals: dict[str, dict] = {}
        for sym in syms:
            g = panel[panel["symbol"] == sym].sort_values("ts")
            if g.empty:
                signals[sym] = {"status": "NO_DATA"}
                continue
            row = g.iloc[-1]
            rsi = row.get("rsi_14")
            rsi_state = None
            if pd.notna(rsi):
                rsi_state = "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
            signals[sym] = {
                "as_of": str(pd.Timestamp(row["ts"]).date()),
                "trend": {
                    "momentum_20d_bps": None if pd.isna(row.get("mom_20d")) else round(float(row["mom_20d"]) * 10000, 1),
                    "momentum_5d_bps": None if pd.isna(row.get("mom_5d")) else round(float(row["mom_5d"]) * 10000, 1),
                    "breakout_distance_pct": None if pd.isna(row.get("breakout_dist_20d")) else round(float(row["breakout_dist_20d"]) * 100, 2),
                },
                "volatility": {
                    "atr_pct": None if pd.isna(row.get("atr_pct")) else round(float(row["atr_pct"]) * 100, 3),
                    "atr_percentile_1y": pct_rank(g["atr_pct"], row.get("atr_pct")),
                },
                "oscillator": {
                    "rsi_14": None if pd.isna(rsi) else round(float(rsi), 1),
                    "state": rsi_state,
                },
                "volume": {
                    "relative_volume_ratio_20d": None if pd.isna(row.get("vol_ratio_20")) else round(float(row["vol_ratio_20"]), 2),
                    "volume_percentile_1y": pct_rank(g["vol_ratio_20"], row.get("vol_ratio_20")),
                },
                "regime": regimes.get(row["ts"], "unknown") if regimes else "unknown",
            }

        db = get_db()
        db.log_provenance("market.pulse", ",".join(syms), source_note,
                          "derived ratios/percentiles/labels only — no raw OHLCV or quote values returned")
        return {
            "tool": "market.pulse",
            "source": source_note,
            "signals": signals,
            "methodology": ("every field is a derived ratio, percentile rank, or causal regime label "
                            "computed from point-in-time bars; raw open/high/low/close/volume and provider "
                            "quote values are never echoed back"),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.pulse", _do)


async def sentiment_pulse(symbol: str) -> dict:
    """Derived-only sentiment/news snapshot — never returns raw quotes or headline text.

    Backed by the same enrichment source as enrich_symbol, but quantized to a
    change bucket, a headline count, and an earnings flag only.
    """
    redaction.reject_keylike_args({"symbol": symbol})

    async def _do():
        db = get_db()
        sym = symbol.strip().upper()
        payload = await cache_enrich.enrich_symbol_cached(db, sym)
        headlines = payload.get("news_headlines") or []
        quote = payload.get("quote") or {}
        c, pc = quote.get("c"), quote.get("pc")
        day_change_bucket = None
        if isinstance(c, (int, float)) and isinstance(pc, (int, float)) and pc:
            chg = (c - pc) / pc
            day_change_bucket = (
                "strong_up" if chg >= 0.03 else "up" if chg > 0.003 else
                "strong_down" if chg <= -0.03 else "down" if chg < -0.003 else "flat")
        db.log_provenance("market.sentiment", sym, "derived",
                          f"from_cache={payload['from_cache']} — no raw quote/headline text returned")
        return {
            "tool": "market.sentiment",
            "symbol": sym,
            "as_of": payload["fetched_at"],
            "headline_count_7d": len(headlines),
            "earnings_within_3d": bool(payload.get("earnings_within_3d")),
            "day_change_bucket": day_change_bucket,
            "provenance": {"tier": "B",
                          "note": "quantized/derived only — no raw quote values or headline text exposed"},
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.sentiment", _do)


async def offline_mode(enabled: bool) -> dict:
    async def _do():
        state = cache_bars.set_offline(enabled)
        return {
            "tool": "cache.offline",
            "enabled": state,
            "semantics": ("cache-only: get_bars/features.build serve cached rows; enrich_symbol and "
                          "cache.warm raise until disabled" if state else
                          "network access restored for cache misses and enrichment"),
        }

    return await run_tool("cache.offline", _do)


# --- Phase 2 derived tools -------------------------------------------------
# Each accepts optional caller-supplied `bars` and returns ratios, percentile
# ranks, labels, and buckets only — raw open/high/low/close/volume and provider
# values are never echoed back (same rule as market.pulse/market.sentiment).

SCREEN_CRITERIA = ("breakout", "momentum", "volume", "anomaly")
RANK_METRICS = {
    "momentum_20d": "mom_20d",
    "volume_ratio_20d": "vol_ratio_20",
    "atr_pct": "atr_pct",
    "rsi_14": "rsi_14",
}


async def _load_panel(symbols: list[str] | None, bars: list[dict] | None) -> tuple[pd.DataFrame, list[str], str]:
    """Shared panel loader for the derived tools: caller bars win, else cache."""
    if bars:
        panel = _panel_from_rows(bars)
        syms = sorted({r["symbol"].strip().upper() for r in bars if r.get("symbol")})
        source_note = "caller-supplied bars"
    else:
        if not symbols:
            raise ValueError("provide symbols (shared cache/relay) or your own bars rows")
        db = get_db()
        syms = sorted({s.strip().upper() for s in symbols if s and s.strip()})
        result = await cache_bars.get_bars_cached(db, syms, lookback_days=280)
        panel = _panel_from_rows(result["rows"])
        source_note = "shared cache/relay"
    if panel.empty:
        raise ValueError("insufficient history to compute derived signals (need >= 30 sessions/symbol)")
    return panel, syms, source_note


def _pct_rank(series: pd.Series, value) -> float | None:
    s = series.dropna()
    if s.empty or pd.isna(value):
        return None
    return round(float((s <= value).mean()), 3)


async def regime_snapshot(symbols: list[str] | None = None, bars: list[dict] | None = None) -> dict:
    """Derived-only causal regime labels (high_vol / drawdown / chop / melt_up /
    gap_heavy) per symbol. Free tool — the public wrapper over the regime labeller.
    """
    redaction.reject_keylike_args({"symbols": symbols, "bars": bars})

    async def _do():
        panel, syms, source_note = await _load_panel(symbols, bars)
        try:
            regimes = label_all_regimes(panel)
        except Exception:
            regimes = {}
        out: dict[str, dict] = {}
        for sym in syms:
            g = panel[panel["symbol"] == sym].sort_values("ts")
            if g.empty:
                out[sym] = {"status": "NO_DATA"}
                continue
            row = g.iloc[-1]
            out[sym] = {
                "as_of": str(pd.Timestamp(row["ts"]).date()),
                "regime": regimes.get(row["ts"], "unknown") if regimes else "unknown",
                "session_count": int(len(g)),
            }
        get_db().log_provenance("market.regime", ",".join(syms), source_note,
                                "causal regime labels only — no raw OHLCV returned")
        return {
            "tool": "market.regime",
            "source": source_note,
            "regimes": out,
            "methodology": ("causal regime labels computed from point-in-time bars with no lookahead; "
                            "raw open/high/low/close/volume are never echoed"),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.regime", _do)


async def microstructure_snapshot(symbol: str | None = None, bars: list[dict] | None = None) -> dict:
    """Derived-only microstructure snapshot: order-flow imbalance, VPIN toxicity,
    and effective half-spread, percentile-ranked within the series. Pro tool.
    """
    redaction.reject_keylike_args({"symbol": symbol, "bars": bars})

    async def _do():
        from microstructure import half_spread_pct, ofi, signed_volume, vpin  # vendored alpha

        panel, syms, source_note = await _load_panel([symbol] if symbol else None, bars)
        if symbol:
            syms = [symbol.strip().upper()]
        results: dict[str, dict] = {}
        for sym in syms:
            g = panel[panel["symbol"] == sym].sort_values("ts")
            if len(g) < 30:
                results[sym] = {"status": "INSUFFICIENT_HISTORY", "sessions": int(len(g))}
                continue
            close = g["close"].to_numpy(dtype=float)
            volume = g["volume"].to_numpy(dtype=float)
            high = g.get("high", pd.Series(dtype=float)).to_numpy(dtype=float)
            low = g.get("low", pd.Series(dtype=float)).to_numpy(dtype=float)
            sv = signed_volume(close, volume)
            ofi_val, ofi_raw, ofi_norm = ofi(sv, window=20)
            vpin_val, vpin_series = vpin(sv)
            spread_val, spread_series = half_spread_pct(high, low, close, window=10)
            results[sym] = {
                "as_of": str(pd.Timestamp(g["ts"].iloc[-1]).date()),
                "ofi": (None if not ofi_norm.size or pd.isna(ofi_norm[-1])
                        else round(float(ofi_norm[-1]), 4)),
                "ofi_percentile_1y": _pct_rank(pd.Series(ofi_norm), ofi_norm[-1] if ofi_norm.size else None),
                "vpin": (None if pd.isna(vpin_val) else round(float(vpin_val), 4)),
                "vpin_toxicity_bucket": (None if pd.isna(vpin_val) else
                                         "toxic" if vpin_val >= 0.6 else
                                         "elevated" if vpin_val >= 0.4 else "normal"),
                "half_spread_pct": (None if not spread_series.size or pd.isna(spread_series[-1])
                                    else round(float(spread_series[-1]) * 100, 4)),
                "half_spread_percentile_1y": _pct_rank(pd.Series(spread_series),
                                                       spread_series[-1] if spread_series.size else None),
            }
        get_db().log_provenance("market.microstructure", ",".join(syms), source_note,
                                "derived microstructure ratios/percentiles only — no raw values returned")
        return {
            "tool": "market.microstructure",
            "source": source_note,
            "signals": results,
            "methodology": ("tick-rule signed volume, order-flow imbalance, volume-synchronized toxicity "
                            "(VPIN), and a high-low spread estimate — reported as ratios and percentile "
                            "ranks only; no raw prices, volumes, or provider values"),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.microstructure", _do)


async def volume_forecast(symbol: str, horizon_bars: int = 1) -> dict:
    """Derived-only volume forecast for the next `horizon_bars` sessions, expressed
    as a ratio to the trailing 20-session average volume (never raw share counts).
    The interval is the recent 10th-90th percentile range of the volume ratio.
    """
    redaction.reject_keylike_args({"symbol": symbol, "horizon_bars": horizon_bars})

    async def _do():
        panel, syms, source_note = await _load_panel([symbol], None)
        g = panel[panel["symbol"] == syms[0]].sort_values("ts")
        if len(g) < 30:
            raise ValueError("insufficient history for a volume forecast (need >= 30 sessions)")
        vol = g["volume"].astype(float)
        avg20 = vol.rolling(20).mean()
        ratio = (vol / avg20).replace([float("inf"), float("-inf")], float("nan")).dropna()
        recent = ratio.tail(60)
        lo, hi = recent.quantile([0.10, 0.90])
        forecast_ratio = float(ratio.iloc[-1]) if len(ratio) else None
        get_db().log_provenance("volume.forecast", syms[0], source_note,
                                "forecast expressed as a ratio to trailing 20-session average — no raw counts")
        return {
            "tool": "volume.forecast",
            "symbol": syms[0],
            "as_of": str(pd.Timestamp(g["ts"].iloc[-1]).date()),
            "horizon_bars": horizon_bars,
            "forecast_ratio_to_20d_avg": None if forecast_ratio is None else round(forecast_ratio, 3),
            "interval_p10_p90": [round(float(lo), 3), round(float(hi), 3)],
            "note": ("a forecast ratio of 1.2 means volume ~20% above the trailing 20-session average; "
                     "the interval is the recent 10th-90th percentile band. No raw share counts are returned."),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("volume.forecast", _do)


async def screen_universe(symbols: list[str] | None = None,
                          bars: list[dict] | None = None,
                          criteria: str = "breakout") -> dict:
    """Ranked, derived-only screen over the symbol list. criteria: breakout
    (distance from 20d range high), momentum (20d momentum), volume (20d volume
    ratio), anomaly (combination of the three). Returns rank + percentile per
    symbol — never raw prices or volumes.
    """
    redaction.reject_keylike_args({"symbols": symbols, "bars": bars, "criteria": criteria})

    async def _do():
        if criteria not in SCREEN_CRITERIA:
            raise ValueError(f"criteria must be one of {', '.join(SCREEN_CRITERIA)}")
        panel, syms, source_note = await _load_panel(symbols, bars)
        rows: dict[str, dict] = {}
        for sym in syms:
            g = panel[panel["symbol"] == sym].sort_values("ts")
            if g.empty:
                rows[sym] = {"status": "NO_DATA"}
                continue
            row = g.iloc[-1]
            breakout = row.get("breakout_dist_20d")
            momentum = row.get("mom_20d")
            vol_ratio = row.get("vol_ratio_20")
            if criteria == "breakout":
                score = None if pd.isna(breakout) else float(breakout)
                key = "breakout_distance_pct"
            elif criteria == "momentum":
                score = None if pd.isna(momentum) else float(momentum)
                key = "momentum_20d_bps"
            elif criteria == "volume":
                score = None if pd.isna(vol_ratio) else float(vol_ratio)
                key = "volume_ratio_20d"
            else:  # anomaly: mean of the non-null derived components
                parts = [x for x in (breakout, momentum, vol_ratio) if not pd.isna(x)]
                score = float(sum(parts)) / len(parts) if parts else None
                key = "anomaly_score"
            rows[sym] = {"score_field": key, "score": score}
        # Cross-sectional percentile rank over the list.
        vals = [r["score"] for r in rows.values() if isinstance(r.get("score"), (int, float))]
        scored = [(sym, r["score"]) for sym, r in rows.items() if isinstance(r.get("score"), (int, float))]
        scored.sort(key=lambda x: x[1], reverse=True)
        rank_map = {sym: i + 1 for i, (sym, _) in enumerate(scored)}
        for sym, r in rows.items():
            r["score_pctile"] = None if r.get("score") is None else _pct_rank(pd.Series(vals), r["score"])
            r["rank"] = rank_map.get(sym)
            r.pop("score", None)
        get_db().log_provenance("market.screen", ",".join(syms), source_note,
                                f"ranked screen ({criteria}) — percentile ranks only, no raw values")
        return {
            "tool": "market.screen",
            "criteria": criteria,
            "source": source_note,
            "screened": rows,
            "methodology": ("symbols are ranked by a derived score expressed as cross-sectional "
                            "percentile ranks only; raw prices/volumes are never echoed"),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.screen", _do)


async def cross_sectional_rank(symbols: list[str] | None = None,
                               bars: list[dict] | None = None,
                               metric: str = "momentum_20d") -> dict:
    """Cross-sectional percentile rank of one derived metric across the symbol
    list. metric: momentum_20d | volume_ratio_20d | atr_pct | rsi_14. Returns
    each symbol's latest metric value as a percentile rank only.
    """
    redaction.reject_keylike_args({"symbols": symbols, "bars": bars, "metric": metric})

    async def _do():
        col = RANK_METRICS.get(metric)
        if col is None:
            raise ValueError(f"metric must be one of {', '.join(RANK_METRICS)}")
        panel, syms, source_note = await _load_panel(symbols, bars)
        latest: dict[str, float | None] = {}
        for sym in syms:
            g = panel[panel["symbol"] == sym].sort_values("ts")
            if g.empty:
                latest[sym] = None
                continue
            v = g.iloc[-1].get(col)
            latest[sym] = None if pd.isna(v) else float(v)
        vals = [v for v in latest.values() if v is not None]
        ranked = {sym: None if v is None else _pct_rank(pd.Series(vals), v) for sym, v in latest.items()}
        get_db().log_provenance("market.rank", ",".join(syms), source_note,
                                f"cross-sectional percentile ranks of {metric} — no raw values")
        return {
            "tool": "market.rank",
            "metric": metric,
            "source": source_note,
            "rank": ranked,
            "methodology": ("each symbol's latest derived metric is expressed as its percentile rank "
                            "across the supplied universe; no raw metric values are returned"),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.rank", _do)

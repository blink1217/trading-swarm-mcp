"""swarm-data-mcp tool implementations (server-agnostic; no MCP imports)."""
from __future__ import annotations

import datetime as dt
import time

import pandas as pd

from swarm_mcp import vendor_path  # noqa: F401

from gym.panel import assert_no_lookahead, decision_features, prepare_panel  # vendored alpha
from provenance import (  # vendored guardrails
    BANNED_ACTUALS_SOURCES,
    ProvenanceViolation,
    assert_point_in_time,
    feature_tier,
)

from swarm_mcp import access, envelope, redaction, telemetry
from swarm_mcp.cache import bars as cache_bars
from swarm_mcp.cache import enrich as cache_enrich
from swarm_mcp.cache.db import get_db
from swarm_mcp.cache.freshness import session_date

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


async def _run(tool: str, fn):
    t0 = time.perf_counter()
    try:
        access.check_access()
        out = await fn()
        telemetry.record(tool, True, (time.perf_counter() - t0) * 1000.0)
        return redaction.redact(out)
    except access.AccessRequired as e:
        telemetry.record(tool, False, (time.perf_counter() - t0) * 1000.0)
        return {"tool": tool, "access": "REQUIRED",
                "error": redaction.redact_text(str(e)),
                **access.request_instructions()}
    except Exception as e:
        telemetry.record(tool, False, (time.perf_counter() - t0) * 1000.0)
        return {"tool": tool, "error": redaction.redact_text(f"{type(e).__name__}: {e}")}


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

    return await _run("get_bars", _do)


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

    return await _run("enrich_symbol", _do)


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
                f"no cached bars for {sym} up to {as_of} — run cache_warm first (or get_bars online)")
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
        db.log_provenance("build_features", sym, "cache+guards",
                          f"as_of={as_of_ts.date()} lookahead=PASS unscoreable={[v.split(':')[0] for v in violations]}")
        out = {
            "tool": "build_features",
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

    return await _run("build_features", _do)


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
            "tool": "cache_warm",
            "rows_upserted": result["rows_upserted"],
            "symbols": result["symbols"],
            "immutability": ("all finalized sessions are now immutable in cache — replays against them "
                             "cost zero API calls forever"),
            "limits": limits,
        }
        if escalation:
            out["escalation"] = escalation
        return out

    return await _run("cache_warm", _do)


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
        return {"tool": "cache_stats", **stats}

    return await _run("cache_stats", _do)


async def offline_mode(enabled: bool) -> dict:
    async def _do():
        state = cache_bars.set_offline(enabled)
        return {
            "tool": "offline_mode",
            "enabled": state,
            "semantics": ("cache-only: get_bars/build_features serve cached rows; enrich_symbol and "
                          "cache_warm raise until disabled" if state else
                          "network access restored for cache misses and enrichment"),
        }

    return await _run("offline_mode", _do)

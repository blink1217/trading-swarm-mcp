"""Cached bar access: immutable past served from cache, in-progress session
refetchable under TTL, explicit backfill only via cache_warm."""
from __future__ import annotations

import datetime as dt
import os

from swarm_mcp import vendor_path  # noqa: F401

from bars_fetch import fetch_daily_bars  # vendored alpha (GCP-free)

from swarm_mcp.cache.db import CacheDB
from swarm_mcp.cache.freshness import (
    BAR_IN_PROGRESS_TTL_S,
    age_seconds,
    session_date,
    utc_today,
    utcnow,
)
from swarm_mcp.cache.rate_limit import RateLimitedClient

PROVIDER = "alpaca"
TIMEFRAME = "1Day"
ADJUSTMENT = "split"
STALE_AFTER_DAYS = 3


class OfflineModeError(RuntimeError):
    pass


OFFLINE = {"enabled": False}


def set_offline(enabled: bool) -> bool:
    OFFLINE["enabled"] = bool(enabled)
    return OFFLINE["enabled"]


def offline_enabled() -> bool:
    return OFFLINE["enabled"]


def require_online(action: str) -> None:
    if OFFLINE["enabled"]:
        raise OfflineModeError(
            f"offline_mode is enabled — {action} requires network; disable offline_mode or rely on cached rows")


def api_keys() -> tuple[str, str]:
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET", "")
    if not key or not secret:
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET not set — this server is BYO-key and reads credentials from env only")
    return key, secret


def _need_api(symbols: list[str], by_symbol: dict[str, list[dict]], now: dt.datetime) -> list[str]:
    today = utc_today(now)
    need = []
    for s in symbols:
        rows = by_symbol.get(s, [])
        if not rows:
            need.append(s)
            continue
        latest = rows[-1]
        gap_days = (today - session_date(latest["ts"])).days
        if gap_days == 0:
            if age_seconds(latest["fetched_at"], now) > BAR_IN_PROGRESS_TTL_S:
                need.append(s)
        elif gap_days > STALE_AFTER_DAYS:
            need.append(s)
    return need


async def get_bars_cached(db: CacheDB, symbols: list[str], lookback_days: int,
                          now: dt.datetime | None = None) -> dict:
    now = now or utcnow()
    symbols = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if not symbols:
        raise ValueError("no symbols given")
    start = (utc_today(now) - dt.timedelta(days=max(1, int(lookback_days)) + 10)).isoformat()

    cached = db.get_bars(PROVIDER, symbols, TIMEFRAME, ADJUSTMENT, start=start)
    by_symbol: dict[str, list[dict]] = {}
    for r in cached:
        by_symbol.setdefault(r["symbol"], []).append(r)
    from_cache = len(cached)

    from_api = 0
    if not OFFLINE["enabled"]:
        need = _need_api(symbols, by_symbol, now)
        if need:
            key, secret = api_keys()
            days = max(1, int(lookback_days)) + 1
            async with RateLimitedClient(PROVIDER) as client:
                fetched = await fetch_daily_bars(client, need, days,
                                                 timeframe=TIMEFRAME, adjustment=ADJUSTMENT,
                                                 api_key=key, api_secret=secret)
            from_api = db.upsert_bars(PROVIDER, TIMEFRAME, ADJUSTMENT, fetched)
            db.log_api_call(PROVIDER, "bars", len(need), from_api, cached=False)
            cached = db.get_bars(PROVIDER, symbols, TIMEFRAME, ADJUSTMENT, start=start)
            by_symbol = {}
            for r in cached:
                by_symbol.setdefault(r["symbol"], []).append(r)
    else:
        db.log_api_call(PROVIDER, "bars", len(symbols), from_cache, cached=True)

    sessions_by_symbol = {}
    for s in symbols:
        rows = by_symbol.get(s, [])
        sessions_by_symbol[s] = {
            "rows": len(rows),
            "oldest_session": rows[0]["ts"] if rows else None,
            "newest_session": rows[-1]["ts"] if rows else None,
        }
    return {
        "rows": cached,
        "from_cache": from_cache,
        "from_api": from_api,
        "sessions_by_symbol": sessions_by_symbol,
    }


def _covered_to_depth(db: CacheDB, symbol: str, window_start: dt.date,
                      today: dt.date) -> bool:
    lo, hi = db.session_bounds(PROVIDER, symbol, TIMEFRAME, ADJUSTMENT)
    if lo is None or hi is None:
        return False
    if session_date(lo) > window_start:
        return False
    return (today - session_date(hi)).days <= STALE_AFTER_DAYS


async def warm_cache(db: CacheDB, symbols: list[str], years: float,
                     now: dt.datetime | None = None) -> dict:
    require_online("cache_warm backfill")
    now = now or utcnow()
    symbols = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if not symbols:
        raise ValueError("no symbols given")
    if not 0 < years <= 10:
        raise ValueError("years must be in (0, 10]")
    days = int(years * 253) + 20
    today = utc_today(now)
    window_start = today - dt.timedelta(days=days)

    need = [s for s in symbols if not _covered_to_depth(db, s, window_start, today)]
    if not need:
        db.log_api_call(PROVIDER, "bars_warm", len(symbols), 0, cached=True)
        summary = {}
        for s in symbols:
            lo, hi = db.session_bounds(PROVIDER, s, TIMEFRAME, ADJUSTMENT)
            summary[s] = {"rows": db.count_bars(PROVIDER, s, TIMEFRAME, ADJUSTMENT),
                          "oldest_session": lo, "newest_session": hi}
        return {"rows_upserted": 0, "api_calls": 0, "symbols": summary,
                "note": "all symbols already covered to the requested depth — zero API calls; "
                        "finalized sessions are immutable"}

    key, secret = api_keys()
    async with RateLimitedClient(PROVIDER) as client:
        fetched = await fetch_daily_bars(client, need, days,
                                         timeframe=TIMEFRAME, adjustment=ADJUSTMENT,
                                         api_key=key, api_secret=secret)
    n = db.upsert_bars(PROVIDER, TIMEFRAME, ADJUSTMENT, fetched)
    db.log_api_call(PROVIDER, "bars_warm", len(need), n, cached=False)
    summary = {}
    for s in symbols:
        lo, hi = db.session_bounds(PROVIDER, s, TIMEFRAME, ADJUSTMENT)
        summary[s] = {"rows": db.count_bars(PROVIDER, s, TIMEFRAME, ADJUSTMENT),
                      "oldest_session": lo, "newest_session": hi}
    return {"rows_upserted": n, "api_calls": 1, "symbols": summary}

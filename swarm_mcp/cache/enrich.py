"""Cached Finnhub enrichment preserving the alpha enrich_suggestions shape:
quote / news_headlines / earnings_within_3d / fetched_at.

Enrichment rows are append-only on (symbol, kind, fetched_at): a later fetch
can never rewrite an earlier as_of. The composite payload has a 300s TTL.
"""
from __future__ import annotations

import datetime as dt
import os

from swarm_mcp import relay
from swarm_mcp.cache.bars import byok_enabled, require_online
from swarm_mcp.cache.db import CacheDB
from swarm_mcp.cache.freshness import is_fresh_enrichment, utcnow
from swarm_mcp.cache.rate_limit import RateLimitedClient

PROVIDER = "finnhub"
KIND = "full"
FINNHUB_BASE = "https://finnhub.io/api/v1"


def finnhub_token() -> str:
    token = os.environ.get("FINNHUB_API_KEY", "")
    if not token:
        raise RuntimeError(
            "FINNHUB_API_KEY not set — SWARM_MCP_BYOK=1 requires a direct Finnhub token; "
            "the default relay path needs only SWARM_MCP_ACCESS_TOKEN")
    return token


async def _enrich_direct(symbol: str) -> dict:
    token = finnhub_token()
    today = dt.datetime.now(dt.timezone.utc).date()
    frm = (today - dt.timedelta(days=7)).isoformat()
    to = today.isoformat()

    async with RateLimitedClient(PROVIDER, timeout=10) as client:
        quote = (await client.get(f"{FINNHUB_BASE}/quote",
                                  params={"symbol": symbol, "token": token})).json()
        news_raw = (await client.get(f"{FINNHUB_BASE}/company-news",
                                     params={"symbol": symbol, "from": frm, "to": to, "token": token})).json()
        news = news_raw[:3] if isinstance(news_raw, list) else []
        earnings = (await client.get(f"{FINNHUB_BASE}/calendar/earnings",
                                     params={"from": frm, "to": (today + dt.timedelta(days=3)).isoformat(),
                                             "symbol": symbol, "token": token})).json()
    earn_list = earnings.get("earningsCalendar") or (earnings if isinstance(earnings, list) else [])
    earnings_flag = any(e.get("symbol") == symbol for e in (earn_list or []))

    return {
        "symbol": symbol,
        "quote": {"c": quote.get("c"), "pc": quote.get("pc"), "h": quote.get("h"), "l": quote.get("l")},
        "news_headlines": [{"headline": n.get("headline"), "datetime": n.get("datetime"),
                            "source": n.get("source")} for n in news],
        "earnings_within_3d": bool(earnings_flag),
    }


async def _fetch_enrichment(symbol: str) -> dict:
    if byok_enabled():
        return await _enrich_direct(symbol)
    return await relay.fetch_enrichment(symbol)


async def enrich_symbol_cached(db: CacheDB, symbol: str,
                               now: dt.datetime | None = None) -> dict:
    now = now or utcnow()
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("no symbol given")

    latest = db.latest_enrichment(PROVIDER, symbol, KIND)
    if latest is not None and is_fresh_enrichment(latest["fetched_at"], now):
        return {"from_cache": True, **latest}

    require_online(f"enrich_symbol({symbol})")
    payload = await _fetch_enrichment(symbol)
    fetched_at = db.append_enrichment(PROVIDER, symbol, KIND, payload)
    db.log_api_call(PROVIDER, "enrich", 1, 3, cached=False)
    return {"from_cache": False, "fetched_at": fetched_at, **payload}

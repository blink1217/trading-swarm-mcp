"""Hosted data relay client: bars + enrichment served through the 1.21
Initiative site instead of direct provider calls.

Users of the MCP do NOT need Alpaca/Finnhub credentials — the site holds the
provider keys behind the access token and returns the same bar rows / enrichment
payload shapes the direct paths produce, so the SQLite cache, provenance, and
point-in-time semantics are unchanged. The relay is fail-closed: any non-200
or network failure raises, never returns partial rows as if they were complete.
"""
from __future__ import annotations

import os

import httpx

from swarm_mcp import access

RELAY_BASE_ENV = "SWARM_MCP_RELAY_URL"
DEFAULT_RELAY_BASE = "https://1.21initiative.com/api/mcp"
TIMEOUT_S = 60.0

BAR_FIELDS = ("symbol", "ts", "open", "high", "low", "close", "volume")


class RelayError(RuntimeError):
    pass


def relay_base() -> str:
    return os.environ.get(RELAY_BASE_ENV, "").strip() or DEFAULT_RELAY_BASE


def _access_token() -> str:
    token = os.environ.get(access.ACCESS_TOKEN_ENV, "").strip()
    if not token:
        raise RelayError(
            f"no {access.ACCESS_TOKEN_ENV} is set — the hosted data relay requires the "
            f"same token as the access gate. Request one at {access.SITE_URL}")
    return token


def _check(body: dict) -> None:
    if not body.get("ok"):
        raise RelayError(
            f"data relay refused: {body.get('error', 'unknown error')} — "
            f"request access at {access.SITE_URL}")


async def fetch_bars(symbols: list[str], days: int, *,
                     timeframe: str = "1Day", adjustment: str = "split") -> list[dict]:
    """Fetch the most-recent `days` split-adjusted bars per symbol via the relay.

    Returns the same row shape as bars_fetch.fetch_daily_bars so callers and the
    SQLite upsert are unchanged: [{symbol, ts, open, high, low, close, volume}].
    """
    symbols = sorted({s.strip().upper() for s in symbols if s and s.strip()})
    if not symbols:
        raise ValueError("no symbols given")

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        try:
            r = await client.post(
                f"{relay_base()}/data/bars",
                json={"symbols": symbols, "days": int(days),
                      "timeframe": timeframe, "adjustment": adjustment},
                headers={"Authorization": f"Bearer {_access_token()}"},
            )
        except httpx.HTTPError as e:
            raise RelayError(
                f"data relay unreachable ({type(e).__name__}) — request access at "
                f"{access.SITE_URL}") from e
    if r.status_code != 200:
        raise RelayError(
            f"data relay refused (HTTP {r.status_code}) — token rejected or relay "
            f"unavailable; request access at {access.SITE_URL}")
    try:
        body = r.json()
    except ValueError as e:
        raise RelayError(f"data relay returned a non-JSON response ({type(e).__name__})") from e
    _check(body)

    rows = []
    for sym, bars in (body.get("bars") or {}).items():
        for b in bars:
            try:
                rows.append({
                    "symbol": sym,
                    "ts": str(b["t"]),
                    "open": float(b["o"]),
                    "high": float(b["h"]),
                    "low": float(b["l"]),
                    "close": float(b["c"]),
                    "volume": int(b["v"]),
                })
            except (KeyError, TypeError, ValueError) as e:
                raise RelayError(f"data relay returned a malformed bar for {sym}: {e}") from e
    return rows


async def fetch_enrichment(symbol: str) -> dict:
    """Fetch the Finnhub enrichment composite via the relay.

    Returns the same payload shape the direct path builds:
    {symbol, quote: {c, pc, h, l}, news_headlines: [...], earnings_within_3d}.
    The caller appends its own fetched_at via the append-only enrichment store.
    """
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("no symbol given")

    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        try:
            r = await client.post(
                f"{relay_base()}/data/enrich",
                json={"symbol": symbol},
                headers={"Authorization": f"Bearer {_access_token()}"},
            )
        except httpx.HTTPError as e:
            raise RelayError(
                f"data relay unreachable ({type(e).__name__}) — request access at "
                f"{access.SITE_URL}") from e
    if r.status_code != 200:
        raise RelayError(
            f"data relay refused (HTTP {r.status_code}) — token rejected or relay "
            f"unavailable; request access at {access.SITE_URL}")
    try:
        body = r.json()
    except ValueError as e:
        raise RelayError(f"data relay returned a non-JSON response ({type(e).__name__})") from e
    _check(body)

    quote = body.get("quote") or {}
    news = body.get("news_headlines") or []
    if not isinstance(news, list):
        news = []
    return {
        "symbol": symbol,
        "quote": {"c": quote.get("c"), "pc": quote.get("pc"),
                  "h": quote.get("h"), "l": quote.get("l")},
        "news_headlines": [
            {"headline": n.get("headline"), "datetime": n.get("datetime"),
             "source": n.get("source")}
            for n in news if isinstance(n, dict)
        ],
        "earnings_within_3d": bool(body.get("earnings_within_3d")),
    }

"""Storage-agnostic Alpaca bars fetcher for data-bridge.

Extracted from app.py so the fetch logic imports without GCP env vars or a
BigQuery client (app.py constructs those lazily). The row shape and request
contract are unchanged: Alpaca 1Day bars, split-adjusted, 50-symbol chunks.
"""
from __future__ import annotations

import os

import httpx

ALPACA_DATA = "https://data.alpaca.markets/v2/stocks/bars"

DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
    "SPY", "QQQ", "IWM",
]

BAR_FIELDS = ("symbol", "ts", "open", "high", "low", "close", "volume")


async def fetch_daily_bars(
    client: httpx.AsyncClient,
    symbols: list[str],
    days: int,
    *,
    timeframe: str = "1Day",
    adjustment: str = "split",
    api_key: str | None = None,
    api_secret: str | None = None,
) -> list[dict]:
    """Fetch the trailing `days` calendar days of daily bars for `symbols`.

    Alpaca semantics that bit us before (the tape was two sessions deep):
      * without `start` the API defaults to the beginning of the CURRENT day, so
        `days` was silently ignored — we now pass an explicit `start`;
      * `limit` counts bars across ALL symbols in the request (max 10,000), so
        deep history needs `next_page_token` pagination — we now page;
      * the free data plan serves the IEX feed (`ALPACA_FEED`, default `iex`);
        SIP requests 403 without a subscription.
    """
    import datetime as _dt

    key = os.environ.get("ALPACA_API_KEY", "") if api_key is None else api_key
    secret = os.environ.get("ALPACA_SECRET", "") if api_secret is None else api_secret
    hdr = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    if not hdr["APCA-API-KEY-ID"]:
        raise RuntimeError("ALPACA_API_KEY not configured")
    feed = os.environ.get("ALPACA_FEED", "iex")
    start = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=max(1, int(days)))).date().isoformat()
    rows = []
    chunk = 50
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        page_token = None
        for _page in range(200):  # hard stop; 50 symbols x 1100 days ~ 6 pages
            params = {"symbols": ",".join(batch), "timeframe": timeframe, "limit": "10000",
                      "adjustment": adjustment, "start": start, "feed": feed, "sort": "asc"}
            if page_token:
                params["page_token"] = page_token
            r = await client.get(ALPACA_DATA, params=params, headers=hdr, timeout=60)
            r.raise_for_status()
            body = r.json()
            for sym, bars in (body.get("bars") or {}).items():
                for b in bars:
                    rows.append({
                        "symbol": sym, "ts": b["t"],
                        "open": float(b["o"]), "high": float(b["h"]), "low": float(b["l"]),
                        "close": float(b["c"]), "volume": int(b["v"]),
                    })
            page_token = body.get("next_page_token")
            if not page_token:
                break
    return rows

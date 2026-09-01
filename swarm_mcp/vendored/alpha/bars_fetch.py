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
    key = os.environ.get("ALPACA_API_KEY", "") if api_key is None else api_key
    secret = os.environ.get("ALPACA_SECRET", "") if api_secret is None else api_secret
    hdr = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    if not hdr["APCA-API-KEY-ID"]:
        raise RuntimeError("ALPACA_API_KEY not configured")
    rows = []
    chunk = 50
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        r = await client.get(
            ALPACA_DATA,
            params={"symbols": ",".join(batch), "timeframe": timeframe, "limit": str(days), "adjustment": adjustment},
            headers=hdr, timeout=30,
        )
        r.raise_for_status()
        bars_by_symbol = r.json().get("bars", {})
        for sym, bars in bars_by_symbol.items():
            for b in bars:
                rows.append({
                    "symbol": sym, "ts": b["t"],
                    "open": float(b["o"]), "high": float(b["h"]), "low": float(b["l"]),
                    "close": float(b["c"]), "volume": int(b["v"]),
                })
    return rows

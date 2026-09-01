"""Shared test helpers: deterministic synthetic bars + async runner."""
from __future__ import annotations

import asyncio
import datetime as dt

import numpy as np
import pandas as pd


def run_async(coro):
    return asyncio.run(coro)


def synthetic_bars(tickers: list[str], days: int = 420, seed: int = 0,
                   end: dt.date | None = None) -> list[dict]:
    """Deterministic OHLCV rows (bars_1day shape) ending at `end` (default today)."""
    end = end or dt.datetime.now(dt.timezone.utc).date()
    dates = pd.bdate_range(end=end, periods=days)
    rng = np.random.default_rng(seed)
    rows = []
    for si, t in enumerate(tickers):
        drift = rng.normal(0.0004, 0.002)
        vol = rng.uniform(0.008, 0.025)
        price = rng.uniform(30.0, 400.0)
        for d in dates:
            ret = drift + rng.normal(0.0, vol)
            if si % 7 == 0 and 120 <= d.dayofyear % 365 <= 135:
                ret += rng.normal(0.0, vol * 2.5)
            open_p = price * (1 + rng.normal(0.0, vol * 0.3))
            close_p = price * (1 + ret)
            high_p = max(open_p, close_p) * (1 + abs(rng.normal(0.0, vol * 0.5)))
            low_p = min(open_p, close_p) * (1 - abs(rng.normal(0.0, vol * 0.5)))
            volume = int(rng.lognormal(14.0, 0.4))
            rows.append({"symbol": t, "ts": d.date().isoformat(),
                         "open": round(open_p, 2), "high": round(high_p, 2),
                         "low": round(low_p, 2), "close": round(close_p, 2),
                         "volume": volume})
            price = close_p
    return rows

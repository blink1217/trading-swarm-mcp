"""Per-provider token buckets + HTTP 429 exponential backoff.

Mirrors the README-documented provider budgets: Finnhub 60 req/min,
Alpaca 200 req/min. Backoff on 429 is 1s * 2^n.
"""
from __future__ import annotations

import asyncio
import time

import httpx

RATE_PER_MIN = {"alpaca": 200.0, "finnhub": 60.0}
MAX_429_RETRIES = 5


class TokenBucket:
    def __init__(self, rate_per_min: float):
        self.capacity = float(rate_per_min)
        self.tokens = float(rate_per_min)
        self.rate = float(rate_per_min) / 60.0
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.rate)


_buckets: dict[str, TokenBucket] = {}


def bucket(provider: str) -> TokenBucket:
    if provider not in _buckets:
        _buckets[provider] = TokenBucket(RATE_PER_MIN.get(provider, 60.0))
    return _buckets[provider]


def reset_buckets() -> None:
    _buckets.clear()


class RateLimitedClient(httpx.AsyncClient):
    """httpx client that rate-limits requests and retries 429s with 1s*2^n backoff."""

    def __init__(self, provider: str, **kwargs):
        super().__init__(**kwargs)
        self._provider = provider

    async def get(self, url, *, params=None, headers=None, timeout=30.0, **kwargs):
        b = bucket(self._provider)
        for attempt in range(MAX_429_RETRIES + 1):
            await b.acquire()
            r = await super().get(url, params=params, headers=headers, timeout=timeout, **kwargs)
            if r.status_code != 429:
                return r
            if attempt >= MAX_429_RETRIES:
                raise RuntimeError(f"provider {self._provider!r} kept returning 429 after "
                                   f"{MAX_429_RETRIES} exponential-backoff retries")
            await asyncio.sleep(1.0 * (2 ** attempt))
        raise RuntimeError("unreachable")

"""Hosted data relay client: bars + enrichment served through the 1.21
Initiative site instead of direct provider calls.

Users of the MCP do NOT need Alpaca/Finnhub credentials — the site holds the
provider keys behind the access token and returns the same bar rows / enrichment
payload shapes the direct paths produce, so the SQLite cache, provenance, and
point-in-time semantics are unchanged. The relay is fail-closed: any non-200
or network failure raises, never returns partial rows as if they were complete.

Refusal semantics: quota/plan refusals arrive as HTTP 402 with a structured
body ({ok:false, error, reason:'quota_exceeded'|'plan_required', upgrade_url,
quota:{...}}). Non-200 bodies are parsed BEFORE raising so users see the real
reason and the upgrade URL instead of a generic "token rejected" message.
"""
from __future__ import annotations

import os

import httpx

from swarm_mcp import access, request_context

RELAY_BASE_ENV = "SWARM_MCP_RELAY_URL"
DEFAULT_RELAY_BASE = "https://1.21initiative.com/api/mcp"
TIMEOUT_S = 60.0

BAR_FIELDS = ("symbol", "ts", "open", "high", "low", "close", "volume")

REFUSAL_REASONS = ("quota_exceeded", "plan_required", "inactive")


class RelayError(RuntimeError):
    """The relay refused or failed. `reason`/`upgrade_url` are set when the
    refusal body carried a structured quota/plan explanation."""

    def __init__(self, message: str, *, reason: str | None = None,
                 upgrade_url: str | None = None, quota: dict | None = None):
        super().__init__(message)
        self.reason = reason
        self.upgrade_url = upgrade_url
        self.quota = quota or {}


def relay_base() -> str:
    return os.environ.get(RELAY_BASE_ENV, "").strip() or DEFAULT_RELAY_BASE


def _access_token() -> str:
    token = request_context.current_token.get()
    if token:
        return token
    token = os.environ.get(access.ACCESS_TOKEN_ENV, "").strip()
    if not token:
        raise RelayError(
            f"no {access.ACCESS_TOKEN_ENV} is set — the hosted data relay requires the "
            f"same token as the access gate. Request one at {access.SITE_URL}")
    return token


def _refusal(status_code: int, r) -> RelayError:
    """Build the RelayError for a non-200 relay response.

    Parses the JSON body first: a structured quota/plan refusal (HTTP 402 with
    reason/upgrade_url) must surface the real reason instead of the generic
    fallback.
    """
    body = None
    try:
        body = r.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        reason = body.get("reason")
        if reason in REFUSAL_REASONS:
            upgrade_url = body.get("upgrade_url") or access.SITE_URL
            quota = body.get("quota") if isinstance(body.get("quota"), dict) else {}
            error = body.get("error") or reason
            parts = [f"data relay refused ({reason}): {error}"]
            if quota:
                used = quota.get("used")
                limit = quota.get("limit")
                resets = quota.get("resets_at")
                if used is not None and limit is not None:
                    parts.append(f"quota {used}/{limit} calls")
                if resets:
                    parts.append(f"resets {resets}")
            parts.append(f"upgrade or manage at {upgrade_url}")
            return RelayError(" — ".join(parts), reason=str(reason),
                              upgrade_url=upgrade_url, quota=quota)
        error = body.get("error")
        if error:
            return RelayError(f"data relay refused (HTTP {status_code}): {error}")
    return RelayError(
        f"data relay refused (HTTP {status_code}) — token rejected or relay "
        f"unavailable; request access at {access.SITE_URL}")


def _check(body: dict) -> None:
    if not body.get("ok"):
        reason = body.get("reason")
        if reason in REFUSAL_REASONS:
            upgrade_url = body.get("upgrade_url") or access.SITE_URL
            raise RelayError(
                f"data relay refused ({reason}): {body.get('error', reason)} — "
                f"upgrade or manage at {upgrade_url}",
                reason=str(reason), upgrade_url=upgrade_url,
                quota=body.get("quota") if isinstance(body.get("quota"), dict) else {})
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
        raise _refusal(r.status_code, r)
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


def _day_change_bucket(c, pc) -> str | None:
    """Derived intraday-change label from current/previous close."""
    if isinstance(c, (int, float)) and isinstance(pc, (int, float)) and pc:
        chg = (c - pc) / pc
        return (
            "strong_up" if chg >= 0.03 else "up" if chg > 0.003 else
            "strong_down" if chg <= -0.03 else "down" if chg < -0.003 else "flat")
    return None


async def fetch_enrichment(symbol: str) -> dict:
    """Fetch the Finnhub enrichment composite via the relay — DERIVED ONLY (M-04).

    The relay contract still returns raw quote/news from the site, but raw
    provider content (headline text, quote values) is no longer persisted to
    the caller's cache: this client reduces it to a headline count, a
    day-change bucket, and the earnings flag before anything is stored.
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
        raise _refusal(r.status_code, r)
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
        "news_count_7d": sum(1 for n in news if isinstance(n, dict)),
        "day_change_bucket": _day_change_bucket(quote.get("c"), quote.get("pc")),
        "earnings_within_3d": bool(body.get("earnings_within_3d")),
    }

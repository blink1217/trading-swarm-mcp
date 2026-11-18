"""Hosted data relay client tests: shape, fail-closed semantics, and the
BYOK/relay dispatch in the cache modules."""
from __future__ import annotations

import httpx
import pytest

import swarm_mcp.cache.bars as cache_bars
import swarm_mcp.cache.enrich as cache_enrich
import swarm_mcp.relay as relay
from swarm_mcp import request_context
from helpers import run_async


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    def __init__(self, handler):
        self.handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, json=None, headers=None):
        return self.handler(url, json, headers)


def _bars_payload():
    return {
        "ok": True, "provider": "alpaca-relay", "timeframe": "1Day",
        "adjustment": "split", "served_at": "2026-08-31T20:00:00Z",
        "from_cache": 2, "from_api": 0,
        "bars": {
            "AAA": [
                {"t": "2026-08-28", "o": 100.5, "h": 101.2, "l": 99.8, "c": 100.9, "v": 12345, "n": 1},
                {"t": "2026-08-31", "o": 100.9, "h": 102.0, "l": 100.4, "c": 101.7, "v": 23456, "n": 1},
            ],
        },
    }


def test_fetch_bars_relay_roundtrip(monkeypatch):
    seen = {}

    def handler(url, json, headers):
        seen.update(url=url, json=json, headers=headers)
        return FakeResponse(200, _bars_payload())

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))

    rows = run_async(relay.fetch_bars(["aaa"], days=30))
    assert seen["url"].endswith("/data/bars")
    assert seen["json"] == {"symbols": ["AAA"], "days": 30, "timeframe": "1Day", "adjustment": "split"}
    assert seen["headers"]["Authorization"] == "Bearer test-access-token"
    assert rows == [
        {"symbol": "AAA", "ts": "2026-08-28", "open": 100.5, "high": 101.2,
         "low": 99.8, "close": 100.9, "volume": 12345},
        {"symbol": "AAA", "ts": "2026-08-31", "open": 100.9, "high": 102.0,
         "low": 100.4, "close": 101.7, "volume": 23456},
    ]


def test_fetch_bars_fail_closed_on_rejection(monkeypatch):
    def handler(url, json, headers):
        return FakeResponse(401, {"ok": False, "error": "token rejected"})

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    with pytest.raises(relay.RelayError):
        run_async(relay.fetch_bars(["AAA"], days=30))


def test_fetch_bars_402_quota_exceeded_surfaces_reason(monkeypatch):
    payload = {
        "ok": False,
        "error": "monthly relay quota exceeded",
        "reason": "quota_exceeded",
        "upgrade_url": "https://1.21initiative.com/mcp/",
        "quota": {"used": 250, "limit": 250, "resets_at": "2026-10-01T00:00:00Z"},
    }

    def handler(url, json, headers):
        return FakeResponse(402, payload)

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    with pytest.raises(relay.RelayError) as ei:
        run_async(relay.fetch_bars(["AAA"], days=30))
    e = ei.value
    assert e.reason == "quota_exceeded"
    assert e.upgrade_url == "https://1.21initiative.com/mcp/"
    assert e.quota["limit"] == 250
    msg = str(e)
    assert "quota_exceeded" in msg
    assert "250/250" in msg
    assert "2026-10-01" in msg
    assert "https://1.21initiative.com/mcp/" in msg


def test_fetch_bars_402_plan_required_surfaces_reason(monkeypatch):
    payload = {
        "ok": False,
        "error": "deep backfill requires the Pro plan",
        "reason": "plan_required",
        "upgrade_url": "https://1.21initiative.com/mcp/",
    }

    def handler(url, json, headers):
        return FakeResponse(402, payload)

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    with pytest.raises(relay.RelayError) as ei:
        run_async(relay.fetch_bars(["AAA"], days=900))
    e = ei.value
    assert e.reason == "plan_required"
    assert e.upgrade_url == "https://1.21initiative.com/mcp/"
    assert "plan_required" in str(e)


def test_fetch_enrichment_402_surfaces_reason(monkeypatch):
    payload = {"ok": False, "error": "quota", "reason": "quota_exceeded",
               "upgrade_url": "https://1.21initiative.com/mcp/",
               "quota": {"used": 250, "limit": 250}}

    def handler(url, json, headers):
        return FakeResponse(402, payload)

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    with pytest.raises(relay.RelayError) as ei:
        run_async(relay.fetch_enrichment("AAA"))
    assert ei.value.reason == "quota_exceeded"


def test_fetch_bars_non200_without_structured_body_stays_generic(monkeypatch):
    def handler(url, json, headers):
        return FakeResponse(500, ValueError("not json"))

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    with pytest.raises(relay.RelayError) as ei:
        run_async(relay.fetch_bars(["AAA"], days=30))
    assert ei.value.reason is None
    assert "HTTP 500" in str(ei.value)


def test_check_parses_structured_refusal_in_200_body(monkeypatch):
    payload = {"ok": False, "error": "symbol cap exceeded for free plan",
               "reason": "plan_required",
               "upgrade_url": "https://1.21initiative.com/mcp/"}

    def handler(url, json, headers):
        return FakeResponse(200, payload)

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    with pytest.raises(relay.RelayError) as ei:
        run_async(relay.fetch_bars(["AAA"], days=30))
    assert ei.value.reason == "plan_required"
    assert "upgrade" in str(ei.value).lower()


def test_fetch_bars_fail_closed_on_network_error(monkeypatch):
    def handler(url, json, headers):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    with pytest.raises(relay.RelayError):
        run_async(relay.fetch_bars(["AAA"], days=30))


def test_fetch_bars_fail_closed_on_malformed_bar(monkeypatch):
    payload = {"ok": True, "bars": {"AAA": [{"t": "2026-08-31", "o": "x"}]}}

    def handler(url, json, headers):
        return FakeResponse(200, payload)

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    with pytest.raises(relay.RelayError):
        run_async(relay.fetch_bars(["AAA"], days=30))


def test_fetch_bars_requires_token(monkeypatch):
    monkeypatch.delenv("SWARM_MCP_ACCESS_TOKEN", raising=False)
    with pytest.raises(relay.RelayError, match="SWARM_MCP_ACCESS_TOKEN"):
        run_async(relay.fetch_bars(["AAA"], days=30))


def test_fetch_enrichment_relay_roundtrip(monkeypatch):
    payload = {
        "ok": True, "provider": "altdata-relay", "symbol": "AAA", "served_at": "2026-08-31T20:00:00Z",
        "from_cache": False,
        "quote": {"c": 101.7, "pc": 100.9, "h": 102.0, "l": 100.4},
        "news_headlines": [{"headline": "h", "datetime": 1725000000, "source": "s"}],
        "earnings_within_3d": True,
    }

    def handler(url, json, headers):
        assert url.endswith("/data/enrich")
        assert json == {"symbol": "AAA"}
        return FakeResponse(200, payload)

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    out = run_async(relay.fetch_enrichment("aaa"))
    # M-04: the relay client reduces raw provider content to derived fields —
    # headline text and raw quote values never reach the caller's cache.
    assert out == {
        "symbol": "AAA",
        "news_count_7d": 1,
        "day_change_bucket": "up",  # (101.7 - 100.9) / 100.9 ~= +0.79%
        "earnings_within_3d": True,
    }
    assert "quote" not in out and "news_headlines" not in out


def test_bars_dispatch_byok_prefers_direct(monkeypatch):
    """BYOK direct provider fetch works ONLY on local stdio (not hosted HTTP)."""
    monkeypatch.setenv("SWARM_MCP_BYOK", "1")
    monkeypatch.delenv("SWARM_MCP_TRANSPORT", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "AKFAKEKEY000000")
    monkeypatch.setenv("ALPACA_SECRET", "fake-secret")
    calls = []

    async def fake_direct(client, symbols, days, **kwargs):
        calls.append("direct")
        return []

    async def fake_relay(symbols, days, **kwargs):
        calls.append("relay")
        return []

    monkeypatch.setattr(cache_bars, "_fetch_daily_bars_direct", fake_direct)
    monkeypatch.setattr(cache_bars.relay, "fetch_bars", fake_relay)

    run_async(cache_bars.fetch_daily_bars(client=None, symbols=["AAA"], days=30))
    assert calls == ["direct"]


def test_hosted_ignores_byok_for_bars_and_enrichment(monkeypatch):
    from swarm_mcp.cache import enrich as cache_enrich

    monkeypatch.setenv("SWARM_MCP_BYOK", "1")
    monkeypatch.setenv("ALPACA_API_KEY", "AKFAKEKEY000000")
    monkeypatch.setenv("ALPACA_SECRET", "fake-secret")
    monkeypatch.setenv("FINNHUB_API_KEY", "fake-finnhub")
    calls = []

    async def fake_direct(client, symbols, days, **kwargs):
        calls.append("direct_bars")
        return []

    async def fake_relay(symbols, days, **kwargs):
        calls.append("relay_bars")
        return []

    async def fake_enrich_direct(symbol):
        calls.append("direct_enrich")
        return {}

    async def fake_enrich_relay(symbol):
        calls.append("relay_enrich")
        return {"symbol": symbol, "news_count_7d": 0, "day_change_bucket": "flat", "earnings_within_3d": False}

    monkeypatch.setattr(cache_bars, "_fetch_daily_bars_direct", fake_direct)
    monkeypatch.setattr(cache_bars.relay, "fetch_bars", fake_relay)
    monkeypatch.setattr(cache_enrich, "_enrich_direct", fake_enrich_direct)
    monkeypatch.setattr(cache_enrich.relay, "fetch_enrichment", fake_enrich_relay)

    tok_ctx = request_context.current_token.set("121_hosted_user_tok")
    try:
        run_async(cache_bars.fetch_daily_bars(client=None, symbols=["QQQ"], days=30))
        run_async(cache_enrich._fetch_enrichment("QQQ"))
    finally:
        request_context.current_token.reset(tok_ctx)

    assert calls == ["relay_bars", "relay_enrich"]

    # Even without current_token, SWARM_MCP_TRANSPORT=http forces relay
    monkeypatch.setenv("SWARM_MCP_TRANSPORT", "http")
    calls.clear()
    run_async(cache_bars.fetch_daily_bars(client=None, symbols=["QQQ"], days=30))
    run_async(cache_enrich._fetch_enrichment("QQQ"))
    assert calls == ["relay_bars", "relay_enrich"]


def test_http_server_warns_on_byok_and_redacts_provider_urls(monkeypatch, caplog):
    import logging
    from swarm_mcp import redaction
    from swarm_mcp.servers import http_server

    monkeypatch.setenv("SWARM_MCP_BYOK", "1")
    with caplog.at_level(logging.ERROR, logger="swarm_mcp.http"):
        http_server.build_app(require_approval_secret=False)
    assert any("SWARM_MCP_BYOK=1 is ignored on the hosted transport" in r.message for r in caplog.records)

    leaked = "HTTPStatusError: Client error '400 Bad Request' for url 'https://data.alpaca.markets/v2/stocks/bars?symbols=ZZ_Q&feed=iex'"
    scrubbed = redaction.redact_text(leaked)
    assert "alpaca.markets" not in scrubbed
    assert "feed=iex" not in scrubbed
    assert "[REDACTED_UPSTREAM_URL]" in scrubbed


def test_internal_runner_panel_fill_uses_internal_key(monkeypatch):
    monkeypatch.delenv("SWARM_MCP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("SWARM_MCP_INTERNAL_KEY", "internal-secret-xyz")
    captured = {}

    def handler(url, json, headers):
        captured["headers"] = headers
        return FakeResponse(200, {
            "ok": True,
            "bars": {"SPY": [{"t": "2026-11-17T00:00:00Z", "o": 500, "h": 502, "l": 499, "c": 501, "v": 1000}]},
        })

    monkeypatch.setattr(relay.httpx, "AsyncClient", lambda **kw: FakeClient(handler))
    ctx = request_context.internal_runner_fill.set(True)
    try:
        rows = run_async(relay.fetch_bars(["SPY"], 30))
    finally:
        request_context.internal_runner_fill.reset(ctx)
    assert len(rows) == 1
    assert captured["headers"] == {
        "X-Swarm-Internal-Key": "internal-secret-xyz",
        "X-Swarm-Runner-Purpose": "tournament-panel",
    }


def test_bars_dispatch_relay_by_default(monkeypatch):
    monkeypatch.delenv("SWARM_MCP_BYOK", raising=False)
    calls = []

    async def fake_direct(client, symbols, days, **kwargs):
        calls.append("direct")
        return []

    async def fake_relay(symbols, days, **kwargs):
        calls.append("relay")
        return []

    monkeypatch.setattr(cache_bars, "_fetch_daily_bars_direct", fake_direct)
    monkeypatch.setattr(cache_bars.relay, "fetch_bars", fake_relay)

    run_async(cache_bars.fetch_daily_bars(client=None, symbols=["AAA"], days=30))
    assert calls == ["relay"]


def test_enrich_dispatch(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_BYOK", "1")
    monkeypatch.setenv("ALTDATA_API_KEY", "fake-altdata-token")
    calls = []

    async def fake_direct(symbol):
        calls.append("direct")
        return {"symbol": symbol}

    async def fake_relay(symbol):
        calls.append("relay")
        return {"symbol": symbol}

    monkeypatch.setattr(cache_enrich, "_enrich_direct", fake_direct)
    monkeypatch.setattr(cache_enrich.relay, "fetch_enrichment", fake_relay)

    run_async(cache_enrich._fetch_enrichment("AAA"))
    assert calls == ["direct"]

    monkeypatch.setenv("SWARM_MCP_BYOK", "0")
    run_async(cache_enrich._fetch_enrichment("AAA"))
    assert calls == ["direct", "relay"]

"""swarm-data-mcp market.climate tool implementation — operating-area weather
research derived from keyless Open-Meteo (server-agnostic; no MCP imports)."""
from __future__ import annotations

import asyncio

from swarm_mcp import access, redaction
from swarm_mcp.cache import bars as cache_bars
from swarm_mcp.cache.db import get_db
from swarm_mcp.tool_runner import run_tool
from swarm_mcp.weather import climate as climate_engine


def _validate_areas(areas: list[dict]) -> None:
    for a in areas:
        if not str(a.get("symbol", "")).strip():
            raise ValueError("every area entry needs a symbol")
        try:
            lat, lon = float(a["lat"]), float(a["lon"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{a.get('symbol', '?')}: each area needs numeric lat/lon") from None
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise ValueError(f"{a.get('symbol', '?')}: lat/lon out of range")
        try:
            w = float(a.get("weight", 1.0) or 1.0)
        except (TypeError, ValueError):
            raise ValueError(f"{a.get('symbol', '?')}: weight must be numeric") from None
        if w <= 0.0:
            raise ValueError(f"{a.get('symbol', '?')}: weight must be positive")


async def climate_snapshot(symbols: list[str], areas: list[dict] | None = None) -> dict:
    redaction.reject_keylike_args({"symbols": symbols, "areas": areas})

    async def _do():
        if cache_bars.offline_enabled():
            raise ValueError("market.climate needs the keyless Open-Meteo network path — "
                             "refused in cache.offline mode")
        syms = sorted({s.strip().upper() for s in symbols or [] if s and s.strip()})
        if not syms:
            raise ValueError("provide symbols (registry-covered or with caller-supplied areas)")
        _validate_areas(areas or [])
        overrides: dict[str, list[dict]] = {}
        for a in areas or []:
            overrides.setdefault(str(a["symbol"]).strip().upper(), []).append(a)
        results = await asyncio.gather(*(climate_engine.fetch_symbol_climate(
            s, areas_override=overrides.get(s)) for s in syms))
        payload = {r["symbol"]: r for r in results}
        covered = sorted(s for s, r in payload.items() if r.get("status") == "OK")
        get_db().log_provenance("market.climate", ",".join(syms), "open-meteo keyless forecast+baseline",
                                "derived aggregates only — no raw per-day provider series returned")
        return {
            "tool": "market.climate",
            "source": "keyless open-meteo (14d forecast vs same 14 calendar days last year) per operating area",
            "climate": payload,
            "covered": covered,
            "uncovered": sorted(s for s, r in payload.items() if r.get("status") != "OK"),
            "registry_coverage": len(climate_engine.registry_symbols()),
            "methodology": ("weather is fetched for each operating area where the underlying's product "
                            "is made and/or sold (10-K-style footprint or caller-supplied areas) — never "
                            "for the listing exchange or HQ city. Derived sums/means/ratios/counts/buckets "
                            "only; raw per-day provider series are never returned. Forecast values are "
                            "current research context only and never valid for past decision dates."),
            "not_investment_advice": True,
            "learn_more": access.SITE_URL,
        }

    return await run_tool("market.climate", _do)

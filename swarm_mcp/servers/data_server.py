"""swarm-data-mcp — point-in-time bars + enrichment cache (stdio).

Server contract (Smithery-visible): title, description, homepage, icon, and
per-tool annotations/output schemas live here; the implementations stay in
swarm_mcp.tools.data_tools.
"""
from __future__ import annotations

from typing import Annotated, NotRequired, TypedDict

from pydantic import Field

from swarm_mcp.mcp_compat import MCPServer
from swarm_mcp.server_meta import (
    HOME_URL,
    ICON_URL,
    PACKAGE_VERSION,
    SITE_URL,
    annotations,
)
from swarm_mcp.tools import data_tools

SERVER_TITLE = "Swarm Data MCP"
SERVER_DESCRIPTION = (
    "Point-in-time market data assembly from the 1.21 Initiative trading swarm: "
    "derived-only technical signals (market.pulse, market.sentiment), "
    "provenance-guarded feature vectors, and an immutable local cache. No provider "
    "credentials needed."
)

INSTRUCTIONS = (
    "Point-in-time market data assembly extracted from the trading swarm. "
    "History is immutable once a session finalizes: finalized sessions are never re-fetched, "
    "the in-progress session refreshes at most every 60s, enrichment every 300s. "
    "Every response carries coverage, limits (vs the tape-depth gates), and an escalation block "
    "when local depth is insufficient. Data is served through the 1.21 Initiative hosted relay "
    "(https://1.21initiative.com/) — no Alpaca or Finnhub credentials are required; only "
    "SWARM_MCP_ACCESS_TOKEN. Provider credentials are never accepted as tool arguments. "
    "market.pulse and market.sentiment are the recommended general-purpose tools: they return only "
    "derived ratios, percentiles, and labels and never echo raw provider values back to the caller. "
    "Both accept an optional `bars` argument so you can supply your own OHLCV rows for symbols we "
    "don't carry. Raw bar/enrichment access (get_bars/enrich_symbol) is internal only and not "
    "exposed as tools, because those paths echo raw provider values; use features.build for the "
    "provenance-guarded feature vector. This server never places, cancels, or routes orders."
)


class BuildFeaturesOut(TypedDict, total=False):
    tool: str
    symbol: str
    as_of: str
    feature_order: list[str]
    vector: dict
    no_lookahead: str
    provenance: list[dict]
    violations: list[str]
    limits: dict
    escalation: dict
    access: str
    error: str
    request_access_at: str
    how: str


class MarketPulseOut(TypedDict, total=False):
    tool: str
    source: str
    signals: dict
    methodology: str
    not_investment_advice: bool
    learn_more: str
    access: str
    error: str
    request_access_at: str
    how: str


class SentimentPulseOut(TypedDict, total=False):
    tool: str
    symbol: str
    as_of: str
    headline_count_7d: int
    earnings_within_3d: bool
    day_change_bucket: str
    provenance: dict
    not_investment_advice: bool
    learn_more: str
    access: str
    error: str
    request_access_at: str
    how: str


class CacheWarmOut(TypedDict, total=False):
    tool: str
    rows_upserted: int
    symbols: dict
    immutability: str
    limits: dict
    escalation: dict
    access: str
    error: str
    request_access_at: str
    how: str


class CacheStatsOut(TypedDict, total=False):
    tool: str
    per_symbol: dict
    offline_mode: bool
    access: str
    error: str
    request_access_at: str
    how: str


class OfflineModeOut(TypedDict, total=False):
    tool: str
    enabled: bool
    semantics: str
    access: str
    error: str
    request_access_at: str
    how: str


mcp = MCPServer(
    name="swarm-data-mcp",
    title=SERVER_TITLE,
    description=SERVER_DESCRIPTION,
    instructions=INSTRUCTIONS,
    website_url=HOME_URL,
    icons=[{"src": ICON_URL, "mimeType": "image/png", "sizes": ["192x192"]}],
    version=PACKAGE_VERSION,
)


@mcp.tool(
    name="features.build",
    title="Build Feature Vector",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def build_features(
    symbol: Annotated[str, Field(description="Ticker symbol, e.g. AAPL.")],
    as_of: Annotated[str, Field(
        description="Point-in-time session date (YYYY-MM-DD); every feature is a causal "
                    "recomputation at this date, never a later revision.")],
) -> BuildFeaturesOut:
    """Exact FEATURE_ORDER feature vector for symbol at session as_of, with provenance per field.

    Runs the no-lookahead guard (every feature must equal a fresh causal recomputation at as_of)
    and the point-in-time provenance guards. Tier-B/C fields without recorded point-in-time
    evidence are returned as UNSCORABLE — never neutral-filled with 0.0.
    """
    return await data_tools.build_features(symbol=symbol, as_of=as_of)


@mcp.tool(
    name="market.pulse",
    title="Market Pulse",
    annotations=annotations(read_only=True, idempotent=True, open_world=True),
    structured_output=True,
)
async def market_pulse(
    symbols: Annotated[list[str] | None, Field(
        description="Optional list of tickers to analyze via the shared cache/relay "
                    "(default: none — supply symbols or your own bars).")] = None,
    bars: Annotated[list[dict] | None, Field(
        description="Optional caller-supplied OHLCV rows [{symbol, ts, open, high, low, "
                    "close, volume}] for symbols the shared cache does not carry.")] = None,
) -> MarketPulseOut:
    """Derived-only technical snapshot: trend, volatility, oscillator, volume, and regime signals.

    Never returns raw open/high/low/close/volume or provider quote values — only ratios,
    percentile ranks (trailing 1y), and causal regime labels. Pass `symbols` to use the shared
    cache/relay, or pass your own `bars` rows (same shape as get_bars) for symbols we don't carry.
    This is the recommended tool for general market analysis; not investment advice.
    """
    return await data_tools.market_pulse(symbols=symbols, bars=bars)


@mcp.tool(
    name="market.sentiment",
    title="Sentiment Pulse",
    annotations=annotations(read_only=True, idempotent=True, open_world=True),
    structured_output=True,
)
async def sentiment_pulse(
    symbol: Annotated[str, Field(description="Ticker symbol, e.g. TSLA.")],
) -> SentimentPulseOut:
    """Derived-only sentiment/news snapshot for one symbol: headline count, earnings flag,
    and day-change bucket. Never returns raw quote values or headline text — quantized signals
    only. Not investment advice.
    """
    return await data_tools.sentiment_pulse(symbol=symbol)


@mcp.tool(
    name="cache.warm",
    title="Warm Cache",
    annotations=annotations(read_only=False, idempotent=True, open_world=True),
    structured_output=True,
)
async def cache_warm(
    universe: Annotated[list[str] | None, Field(
        description="Optional list of tickers to backfill (default: the swarm's 13-symbol "
                    "universe).")] = None,
    years: Annotated[float, Field(
        description="Years of daily history to backfill (max limited by the plan's backfill "
                    "window).", gt=0.1, le=10.0)] = 1.0,
) -> CacheWarmOut:
    """Backfill history for a universe (default: the swarm's 13-symbol universe).

    After warming, every finalized session is immutable in cache — replays cost zero API calls.
    Requires network and a valid SWARM_MCP_ACCESS_TOKEN; refused in cache.offline mode.
    """
    return await data_tools.cache_warm(universe=universe, years=years)


@mcp.tool(
    name="cache.stats",
    title="Cache Stats",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def cache_stats() -> CacheStatsOut:
    """Cache inventory: rows per symbol, session bounds, depth in weeks, API-call accounting."""
    return await data_tools.cache_stats()


@mcp.tool(
    name="cache.offline",
    title="Toggle Offline Mode",
    annotations=annotations(read_only=False, idempotent=False, open_world=False),
    structured_output=True,
)
async def offline_mode(
    enabled: Annotated[bool, Field(
        description="True = cache-only mode (no network; reads from cache, fetches refused).")],
) -> OfflineModeOut:
    """Toggle cache-only mode: no network access; reads served from cache, fetches refused."""
    return await data_tools.offline_mode(enabled=enabled)


class RegimeSnapshotOut(TypedDict, total=False):
    tool: str
    source: str
    regimes: dict
    methodology: str
    not_investment_advice: bool
    learn_more: str
    access: str
    error: str
    request_access_at: str
    how: str


class MicrostructureOut(TypedDict, total=False):
    tool: str
    source: str
    signals: dict
    methodology: str
    not_investment_advice: bool
    learn_more: str
    access: str
    error: str
    request_access_at: str
    how: str


class VolumeForecastOut(TypedDict, total=False):
    tool: str
    symbol: str
    as_of: str
    horizon_bars: int
    forecast_ratio_to_20d_avg: float
    interval_p10_p90: list[float]
    note: str
    not_investment_advice: bool
    learn_more: str
    access: str
    error: str
    request_access_at: str
    how: str


class ScreenUniverseOut(TypedDict, total=False):
    tool: str
    criteria: str
    source: str
    screened: dict
    methodology: str
    not_investment_advice: bool
    learn_more: str
    access: str
    error: str
    request_access_at: str
    how: str


class CrossSectionalRankOut(TypedDict, total=False):
    tool: str
    metric: str
    source: str
    rank: dict
    methodology: str
    not_investment_advice: bool
    learn_more: str
    access: str
    error: str
    request_access_at: str
    how: str


@mcp.tool(
    name="market.regime",
    title="Regime Snapshot",
    annotations=annotations(read_only=True, idempotent=True, open_world=True),
    structured_output=True,
)
async def regime_snapshot(
    symbols: Annotated[list[str] | None, Field(
        description="Optional list of tickers to label via the shared cache/relay.")] = None,
    bars: Annotated[list[dict] | None, Field(
        description="Optional caller-supplied OHLCV rows [{symbol, ts, open, high, low, "
                    "close, volume}] for symbols the shared cache does not carry.")] = None,
) -> RegimeSnapshotOut:
    """Free tool — causal regime label per symbol (high_vol / drawdown / chop /
    melt_up / gap_heavy), derived from point-in-time bars with no lookahead.
    """
    return await data_tools.regime_snapshot(symbols=symbols, bars=bars)


@mcp.tool(
    name="market.microstructure",
    title="Microstructure Snapshot",
    annotations=annotations(read_only=True, idempotent=True, open_world=True),
    structured_output=True,
)
async def microstructure_snapshot(
    symbol: Annotated[str | None, Field(
        description="Ticker to analyze (served from the shared cache/relay); omit when passing "
                    "your own bars.")] = None,
    bars: Annotated[list[dict] | None, Field(
        description="Optional caller-supplied OHLCV rows [{symbol, ts, open, high, low, "
                    "close, volume}].")] = None,
) -> MicrostructureOut:
    """Derived-only microstructure snapshot: order-flow imbalance, VPIN toxicity
    bucket, and effective half-spread — percentile-ranked within the series.
    Pro tool.
    """
    return await data_tools.microstructure_snapshot(symbol=symbol, bars=bars)


@mcp.tool(
    name="volume.forecast",
    title="Volume Forecast",
    annotations=annotations(read_only=True, idempotent=True, open_world=True),
    structured_output=True,
)
async def volume_forecast(
    symbol: Annotated[str, Field(
        description="Ticker to forecast (served from the shared cache/relay).")],
    horizon_bars: Annotated[int, Field(
        description="Number of sessions ahead to forecast (1-10).", ge=1, le=10)] = 1,
) -> VolumeForecastOut:
    """Derived-only volume forecast expressed as a ratio to the trailing 20-session
    average, with a 10th-90th percentile interval. Never returns raw share counts.
    Pro tool.
    """
    return await data_tools.volume_forecast(symbol=symbol, horizon_bars=horizon_bars)


@mcp.tool(
    name="market.screen",
    title="Screen Universe",
    annotations=annotations(read_only=True, idempotent=True, open_world=True),
    structured_output=True,
)
async def screen_universe(
    symbols: Annotated[list[str] | None, Field(
        description="List of tickers to screen (via the shared cache/relay); omit when passing "
                    "your own bars.")] = None,
    bars: Annotated[list[dict] | None, Field(
        description="Optional caller-supplied OHLCV rows [{symbol, ts, open, high, low, "
                    "close, volume}].")] = None,
    criteria: Annotated[str, Field(
        description="Screen criterion: breakout | momentum | volume | anomaly.")] = "breakout",
) -> ScreenUniverseOut:
    """Ranked, derived-only screen over the symbol list — cross-sectional
    percentile ranks, never raw prices or volumes. Pro tool.
    """
    return await data_tools.screen_universe(symbols=symbols, bars=bars, criteria=criteria)


@mcp.tool(
    name="market.rank",
    title="Cross-Sectional Rank",
    annotations=annotations(read_only=True, idempotent=True, open_world=True),
    structured_output=True,
)
async def cross_sectional_rank(
    symbols: Annotated[list[str] | None, Field(
        description="List of tickers to rank (via the shared cache/relay); omit when passing "
                    "your own bars.")] = None,
    bars: Annotated[list[dict] | None, Field(
        description="Optional caller-supplied OHLCV rows [{symbol, ts, open, high, low, "
                    "close, volume}].")] = None,
    metric: Annotated[str, Field(
        description="Metric to rank by: momentum_20d | volume_ratio_20d | atr_pct | rsi_14.")] = "momentum_20d",
) -> CrossSectionalRankOut:
    """Cross-sectional percentile rank of one derived metric across the symbol
    list — no raw metric values returned. Pro tool.
    """
    return await data_tools.cross_sectional_rank(symbols=symbols, bars=bars, metric=metric)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

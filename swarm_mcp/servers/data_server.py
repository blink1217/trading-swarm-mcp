"""swarm-data-mcp — point-in-time bars + enrichment cache (stdio)."""
from __future__ import annotations

from swarm_mcp.mcp_compat import MCPServer
from swarm_mcp.tools import data_tools

INSTRUCTIONS = (
    "Point-in-time market data assembly extracted from the trading swarm. "
    "History is immutable once a session finalizes: finalized sessions are never re-fetched, "
    "the in-progress session refreshes at most every 60s, enrichment every 300s. "
    "Every response carries coverage, limits (vs the tape-depth gates), and an escalation block "
    "when local depth is insufficient. Data is served through the 1.21 Initiative hosted relay "
    "(https://1.21initiative.com/) — no Alpaca or Finnhub credentials are required; only "
    "SWARM_MCP_ACCESS_TOKEN. Provider credentials are never accepted as tool arguments. "
    "This server never places, cancels, or routes orders."
)

mcp = MCPServer(name="swarm-data-mcp", instructions=INSTRUCTIONS)


@mcp.tool()
async def get_bars(symbols: list[str], lookback_days: int = 30) -> dict:
    """Daily split-adjusted OHLCV bars for symbols with an immutable point-in-time cache.

    Serves finalized sessions from cache (zero API calls) and refreshes only the in-progress
    session. Response includes per-symbol session coverage, tape-depth limits, and an
    escalation block when local depth is below the tape-eligible gate.
    """
    return await data_tools.get_bars(symbols=symbols, lookback_days=lookback_days)


@mcp.tool()
async def enrich_symbol(symbol: str) -> dict:
    """Finnhub quote / 7-day news headlines / earnings-within-3-days for one symbol.

    Cached 300s; enrichment rows are append-only on fetched_at so a later fetch can never
    rewrite an earlier as_of. Tier-B: for past decision dates, point-in-time values must come
    from the archived tape — audit_features enforces this.
    """
    return await data_tools.enrich_symbol(symbol=symbol)


@mcp.tool()
async def build_features(symbol: str, as_of: str) -> dict:
    """Exact FEATURE_ORDER feature vector for symbol at session as_of, with provenance per field.

    Runs the no-lookahead guard (every feature must equal a fresh causal recomputation at as_of)
    and the point-in-time provenance guards. Tier-B/C fields without recorded point-in-time
    evidence are returned as UNSCORABLE — never neutral-filled with 0.0.
    """
    return await data_tools.build_features(symbol=symbol, as_of=as_of)


@mcp.tool()
async def cache_warm(universe: list[str] | None = None, years: float = 1.0) -> dict:
    """Backfill history for a universe (default: the swarm's 13-symbol universe).

    After warming, every finalized session is immutable in cache — replays cost zero API calls.
    Requires network and a valid SWARM_MCP_ACCESS_TOKEN; refused in offline_mode.
    """
    return await data_tools.cache_warm(universe=universe, years=years)


@mcp.tool()
async def cache_stats() -> dict:
    """Cache inventory: rows per symbol, session bounds, depth in weeks, API-call accounting."""
    return await data_tools.cache_stats()


@mcp.tool()
async def offline_mode(enabled: bool) -> dict:
    """Toggle cache-only mode: no network access; reads served from cache, fetches refused."""
    return await data_tools.offline_mode(enabled=enabled)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

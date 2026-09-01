"""swarm-warden-mcp — pre-trade invariant + leakage gate (stdio)."""
from __future__ import annotations

from swarm_mcp.mcp_compat import MCPServer
from swarm_mcp.tools import warden_tools

INSTRUCTIONS = (
    "Pre-trade invariant and leakage auditing — the same checker functions that gate live "
    "capital in the trading swarm, run locally against your own manifest. No IP upload: your "
    "orders, features, and genomes never leave this machine. Promotion verdicts are never "
    "issued locally; undecidable requests return INDETERMINATE_LOCAL with the exact missing "
    "statistical inputs, an audit_request block, and a cloud_job spec for the hosted "
    "tournament. This server never places, cancels, or routes orders."
)

mcp = MCPServer(name="swarm-warden-mcp", instructions=INSTRUCTIONS)


@mcp.tool()
async def validate_order(order: dict, equity: float,
                         current_positions: dict | None = None,
                         floor_overrides: dict | None = None) -> dict:
    """Validate a proposed order against the house floors (max position 25%, gross cap 60%).

    order: {symbol, notional, side: buy|sell}; current_positions: {symbol: notional}.
    Per-fund overrides are allowed but the response always reports the deviation from the
    house floors. Rejections quote the exact floor — the same function gating live capital.
    """
    return await warden_tools.validate_order(order=order, equity=equity,
                                             current_positions=current_positions,
                                             floor_overrides=floor_overrides)


@mcp.tool()
async def audit_features(manifest: list[dict], tape_started: str | None = None,
                         selected_symbols: list[str] | None = None,
                         snapshot_universe: list[str] | None = None) -> dict:
    """Audit a feature manifest for lookahead leakage and provenance violations.

    manifest: [{name, source, value_ts, as_of}]. Flags banned actuals sources
    (open-meteo.archive etc.), features predating tape start, and unknown features fail
    closed to tier C. Optional coverage check vs MIN_COVERAGE=0.90 for challenger-selected
    symbols against the archived snapshot universe.
    """
    return await warden_tools.audit_features(manifest=manifest, tape_started=tape_started,
                                             selected_symbols=selected_symbols,
                                             snapshot_universe=snapshot_universe)


@mcp.tool()
async def cost_check(gross_edge_bps: float, spread_bps: float, slippage_bps: float,
                     adverse_selection_bps: float) -> dict:
    """Convert a claimed gross edge into net-of-cost reality under the pessimistic fill model.

    Spread + slippage + adverse selection are charged at both entry and exit, always against
    the position — the same model the gym scores every episode with.
    """
    return await warden_tools.cost_check(gross_edge_bps=gross_edge_bps, spread_bps=spread_bps,
                                         slippage_bps=slippage_bps,
                                         adverse_selection_bps=adverse_selection_bps)


@mcp.tool()
async def validate_genome(genome: dict) -> dict:
    """Validate a genome against the bounded schema and return its canonical genome_hash.

    The hash is the reproducible strategy identity used as challenger_id in the hosted
    tournament and in audit_request blocks.
    """
    return await warden_tools.validate_genome_tool(genome=genome)


@mcp.tool()
async def explain_sizing(equity: float, atr_14: float, close: float,
                         weekend_approaching: bool = False, gross_exposure: float = 0.0,
                         overnight_gap: float = 0.0,
                         floor_overrides: dict | None = None) -> dict:
    """Step-by-step mirror of the live C# risk engine sizing: ATR sizing, position-notional
    cap, overnight-gap halving, weekend gross headroom. Reports the house floors and any
    deviation when per-fund overrides are supplied.
    """
    return await warden_tools.explain_sizing(equity=equity, atr_14=atr_14, close=close,
                                             weekend_approaching=weekend_approaching,
                                             gross_exposure=gross_exposure,
                                             overnight_gap=overnight_gap,
                                             floor_overrides=floor_overrides)


@mcp.tool()
async def request_promotion_verdict(challenger_genome: dict,
                                    champion_genome: dict | None = None) -> dict:
    """Request a promotion verdict. Always INDETERMINATE_LOCAL: the selection machinery is
    server-side. Names the exact missing statistical inputs and emits an audit_request plus
    a cloud_job spec for the hosted tournament.
    """
    return await warden_tools.request_promotion_verdict(challenger_genome=challenger_genome,
                                                        champion_genome=champion_genome)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

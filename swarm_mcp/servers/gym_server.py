"""swarm-gym-mcp — regime fragility probe (stdio)."""
from __future__ import annotations

from swarm_mcp.mcp_compat import MCPServer
from swarm_mcp.tools import gym_tools

INSTRUCTIONS = (
    "Regime-fragility probing on the swarm's deterministic tier-A replay gym. Answers the "
    "question a local backtest cannot: which regime kills the strategy, and is the sample "
    "even large enough to have an opinion? Reads bars from the shared cache warmed by "
    "swarm-data-mcp (or accepts inline bars). Local runs are seed-capped for statistical "
    "honesty, tier-B/C mutations refuse to score (never neutral-filled), every statistic is "
    "labelled with its power, and promotion verdicts are NEVER issued locally — "
    "undecidable outputs end with a cloud_job spec for the hosted tournament."
)

mcp = MCPServer(name="swarm-gym-mcp", instructions=INSTRUCTIONS)


@mcp.tool()
async def label_regimes(symbols: list[str] | None = None, bars: list[dict] | None = None,
                        min_symbols: int = 8) -> dict:
    """Label the causal market regimes (high_vol, drawdown, chop, melt_up, gap_heavy) over
    cached bars and report the episode pool per regime, flagging thin pools. Labels use
    trailing data only — no lookahead.
    """
    return await gym_tools.label_regimes(symbols=symbols, bars=bars, min_symbols=min_symbols)


@mcp.tool()
async def probe_fragility(genome: dict, champion_genome: dict | None = None,
                          seeds: list[int] | None = None, per_regime: int = 2,
                          symbols: list[str] | None = None, bars: list[dict] | None = None,
                          min_symbols: int = 8) -> dict:
    """Replay a genome over the capped episode-seed matrix and surface per-regime fragility.

    Returns per-regime net bps, worst regime, turnover, exposure caps, and hard-constraint
    violations — never a promotion verdict. Seeds are capped at 8 and per_regime at 2;
    tier-B/C mutations against a champion raise the gym's TierScoringRefusal (UNSCORABLE).
    """
    return await gym_tools.probe_fragility(genome=genome, champion_genome=champion_genome,
                                           seeds=seeds, per_regime=per_regime,
                                           symbols=symbols, bars=bars, min_symbols=min_symbols)


@mcp.tool()
async def paired_preview(champion_genome: dict, challenger_genome: dict,
                         seeds: list[int] | None = None, per_regime: int = 2,
                         symbols: list[str] | None = None, bars: list[dict] | None = None,
                         min_symbols: int = 8) -> dict:
    """Paired champion-vs-challenger preview on identical market paths with the promotion
    gate bypassed. Every statistic is labelled UNDERPOWERED and the exact seed count needed
    to clear MIN_EPISODES=20 is named. Never returns a promotion.
    """
    return await gym_tools.paired_preview(champion_genome=champion_genome,
                                          challenger_genome=challenger_genome,
                                          seeds=seeds, per_regime=per_regime,
                                          symbols=symbols, bars=bars, min_symbols=min_symbols)


@mcp.tool()
async def estimate_cloud_run(seeds: list[int] | None = None, per_regime: int = 4,
                             n_genomes: int = 1) -> dict:
    """Estimate the hosted run: required episodes (5 regimes x per_regime x seeds x genomes),
    wall-clock, budget semantics (MONTHLY_CAP_USD=150, breaker at 80%), and a submit-ready
    POST /tournament/run body for the Strategy Validation Audit booking flow.
    """
    return await gym_tools.estimate_cloud_run(seeds=seeds, per_regime=per_regime,
                                              n_genomes=n_genomes)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""swarm-gym-mcp — regime fragility probe (stdio).

Server contract (Smithery-visible): title, description, homepage, icon, and
per-tool annotations/output schemas live here; the implementations stay in
swarm_mcp.tools.gym_tools.
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
from swarm_mcp.tools import gym_tools

SERVER_TITLE = "Swarm Gym MCP"
SERVER_DESCRIPTION = (
    "Regime-fragility probing on the 1.21 Initiative trading swarm's deterministic replay "
    "gym: label causal market regimes, replay genomes on a capped episode-seed matrix, and "
    "estimate hosted runs. No promotion verdicts are ever issued locally."
)

INSTRUCTIONS = (
    "Regime-fragility probing on the swarm's deterministic tier-A replay gym. Answers the "
    "question a local backtest cannot: which regime kills the strategy, and is the sample "
    "even large enough to have an opinion? Reads bars from the shared cache warmed by "
    "swarm-data-mcp (or accepts inline bars). Local runs are seed-capped for statistical "
    "honesty, tier-B/C mutations refuse to score (never neutral-filled), every statistic is "
    "labelled with its power, and promotion verdicts are NEVER issued locally — "
    "undecidable outputs end with a cloud_job spec for the hosted tournament."
)


class LabelRegimesOut(TypedDict, total=False):
    tool: str
    regimes: list[str]
    regime_counts: dict
    pool_size: int
    thin_pools: list[str]
    thin_pool_warning: str
    labels_are_causal: str
    depth: dict
    escalation: dict
    access: str
    error: str
    request_access_at: str
    how: str


class ProbeFragilityOut(TypedDict, total=False):
    tool: str
    verdict: str
    valid_genome: bool
    errors: list[str]
    genome_hash: str
    fidelity: str
    n_episodes: int
    seeds: list[int]
    per_regime: int
    mean_weekly_bps: float
    per_regime_bps: dict
    worst_regime: str
    worst_regime_bps: float
    turnover_pct_avg: float
    max_gross_exposure: float
    max_position_pct: float
    hard_constraint_violations: list[dict]
    lookahead_violations: list[str]
    underpowered: bool
    power_note: str
    depth: dict
    cloud_job: dict
    fidelity_note: str
    access: str
    error: str
    request_access_at: str
    how: str


class PairedPreviewOut(TypedDict, total=False):
    tool: str
    verdict: str
    promotion: str
    champion_hash: str
    challenger_hash: str
    n_paired_episodes: int
    seeds: list[int]
    per_regime: int
    statistics: dict
    gate_requirements: dict
    identical_paths: str
    depth: dict
    cloud_job: dict
    valid_genomes: bool
    errors: list[str]
    access: str
    error: str
    request_access_at: str
    how: str


class EstimateCloudRunOut(TypedDict, total=False):
    tool: str
    episodes_required: int
    formula: str
    regimes: list[str]
    wall_clock_estimate_s: float
    wall_clock_note: str
    budget: dict
    submit_bodies: dict
    handoff: str
    access: str
    error: str
    request_access_at: str
    how: str


mcp = MCPServer(
    name="swarm-gym-mcp",
    title=SERVER_TITLE,
    description=SERVER_DESCRIPTION,
    instructions=INSTRUCTIONS,
    website_url=HOME_URL,
    icons=[{"src": ICON_URL, "mimeType": "image/png", "sizes": ["192x192"]}],
    version=PACKAGE_VERSION,
)


@mcp.tool(
    name="gym.label_regimes",
    title="Label Regimes",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def label_regimes(
    symbols: Annotated[list[str] | None, Field(
        description="Optional tickers served from the shared cache; omit when passing your "
                    "own bars.")] = None,
    bars: Annotated[list[dict] | None, Field(
        description="Optional inline OHLCV rows [{symbol, ts, open, high, low, close, "
                    "volume}].")] = None,
    min_symbols: Annotated[int, Field(
        description="Minimum symbols required for a valid episode pool.", ge=1, le=20)] = 8,
) -> LabelRegimesOut:
    """Label the causal market regimes (high_vol, drawdown, chop, melt_up, gap_heavy) over
    cached bars and report the episode pool per regime, flagging thin pools. Labels use
    trailing data only — no lookahead.
    """
    return await gym_tools.label_regimes(symbols=symbols, bars=bars, min_symbols=min_symbols)


@mcp.tool(
    name="gym.probe_fragility",
    title="Probe Fragility",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def probe_fragility(
    genome: Annotated[dict, Field(
        description="Genome to replay over the capped episode-seed matrix.")],
    champion_genome: Annotated[dict | None, Field(
        description="Optional champion genome; tier-B/C mutations vs it refuse to score.")] = None,
    seeds: Annotated[list[int] | None, Field(
        description="Optional episode seeds (max 8 locally).")] = None,
    per_regime: Annotated[int, Field(
        description="Episodes per regime (1 or 2 locally).", ge=1, le=2)] = 2,
    symbols: Annotated[list[str] | None, Field(
        description="Optional tickers from the shared cache; omit when passing bars.")] = None,
    bars: Annotated[list[dict] | None, Field(
        description="Optional inline OHLCV rows.")] = None,
    min_symbols: Annotated[int, Field(
        description="Minimum symbols for a valid episode pool.", ge=1, le=20)] = 8,
) -> ProbeFragilityOut:
    """Replay a genome over the capped episode-seed matrix and surface per-regime fragility.

    Returns per-regime net bps, worst regime, turnover, exposure caps, and hard-constraint
    violations — never a promotion verdict. Seeds are capped at 8 and per_regime at 2;
    tier-B/C mutations against a champion raise the gym's TierScoringRefusal (UNSCORABLE).
    """
    return await gym_tools.probe_fragility(genome=genome, champion_genome=champion_genome,
                                           seeds=seeds, per_regime=per_regime,
                                           symbols=symbols, bars=bars, min_symbols=min_symbols)


@mcp.tool(
    name="gym.paired_preview",
    title="Paired Preview",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def paired_preview(
    champion_genome: Annotated[dict, Field(
        description="Current champion genome.")],
    challenger_genome: Annotated[dict, Field(
        description="Challenger genome to compare against the champion.")],
    seeds: Annotated[list[int] | None, Field(
        description="Optional episode seeds (max 8 locally).")] = None,
    per_regime: Annotated[int, Field(
        description="Episodes per regime (1 or 2 locally).", ge=1, le=2)] = 2,
    symbols: Annotated[list[str] | None, Field(
        description="Optional tickers from the shared cache; omit when passing bars.")] = None,
    bars: Annotated[list[dict] | None, Field(
        description="Optional inline OHLCV rows.")] = None,
    min_symbols: Annotated[int, Field(
        description="Minimum symbols for a valid episode pool.", ge=1, le=20)] = 8,
) -> PairedPreviewOut:
    """Paired champion-vs-challenger preview on identical market paths with the promotion
    gate bypassed. Every statistic is labelled UNDERPOWERED and the exact seed count needed
    to clear MIN_EPISODES=20 is named. Never returns a promotion.
    """
    return await gym_tools.paired_preview(champion_genome=champion_genome,
                                          challenger_genome=challenger_genome,
                                          seeds=seeds, per_regime=per_regime,
                                          symbols=symbols, bars=bars, min_symbols=min_symbols)


@mcp.tool(
    name="gym.estimate_cloud_run",
    title="Estimate Cloud Run",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def estimate_cloud_run(
    seeds: Annotated[list[int] | None, Field(
        description="Optional episode seeds (default [0..4]).")] = None,
    per_regime: Annotated[int, Field(
        description="Episodes per regime (default 4 on the hosted side).", ge=1, le=10)] = 4,
    n_genomes: Annotated[int, Field(
        description="Number of genomes in the run.", ge=1, le=100)] = 1,
) -> EstimateCloudRunOut:
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

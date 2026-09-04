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
from swarm_mcp.tools import gym_tools, tournament_tools

SERVER_TITLE = "Swarm Gym MCP"
SERVER_DESCRIPTION = (
    "Regime-fragility probing on the 1.21 Initiative trading swarm's deterministic replay "
    "gym, plus the Shadow Tournament: label causal market regimes, replay genomes on a capped "
    "episode-seed matrix locally, then submit a genome to be scored against the swarm's live "
    "champion on identical hosted paths (paired outcome + ELO). No promotion verdicts are ever "
    "issued locally."
)

INSTRUCTIONS = (
    "Regime-fragility probing on the swarm's deterministic tier-A replay gym. Answers the "
    "question a local backtest cannot: which regime kills the strategy, and is the sample "
    "even large enough to have an opinion? Reads bars from the shared cache warmed by "
    "swarm-data-mcp (or accepts inline bars). Local runs are seed-capped for statistical "
    "honesty, tier-B/C mutations refuse to score (never neutral-filled), every statistic is "
    "labelled with its power, and promotion verdicts are NEVER issued locally. When a local "
    "result is UNDERPOWERED, call tournament.submit: the hosted Shadow Tournament replays the "
    "genome and the swarm's current champion on the same 100 paired episodes, returns the "
    "paired outcome and ELO, and (with contribute=true, half price) licenses the genome to the "
    "swarm's evolution loop. The strategy-contributor tier (contribute=true plus a disclosure "
    "and/or strategy_code — code is NEVER executed, only read statically by the hosted LLM "
    "reviewer) unlocks leaderboard attribution and league-seat eligibility after review. "
    "Poll with tournament.verdict; tournament.leaderboard is free."
)


class TournamentSubmitOut(TypedDict, total=False):
    tool: str
    status: str
    job_id: str
    genome_hash: str
    contribute: bool
    contribution: str
    disclosure_status: str
    strategy_code_sent: bool
    analysis_contract: str
    valid_disclosure: bool
    disclosure_errors: list[str]
    valid_strategy_code: bool
    strategy_code_errors: list[str]
    credits_charged: int
    quota: dict
    geometry: dict
    what_was_sent: str
    retention: str
    next: str
    valid_genome: bool
    errors: list[str]
    note: str
    access: str
    error: str
    request_access_at: str
    how: str


class TournamentVerdictOut(TypedDict, total=False):
    tool: str
    job_id: str
    status: str
    genome_hash: str
    contribute: bool
    credits_charged: int
    rating: float
    result: dict
    verdict_semantics: str
    next: str
    access: str
    error: str
    request_access_at: str
    how: str


class TournamentLeaderboardOut(TypedDict, total=False):
    tool: str
    updated_at: str | None
    champion_rating: float
    total_runs: int
    contributed_runs: int
    challenger_wins: int
    top: list[dict]
    geometry: dict
    pricing: dict
    anonymised: str
    access: str
    error: str
    request_access_at: str
    how: str


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
    promotion: str | None
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
    """Estimate a hosted run: required episodes (5 regimes x per_regime x seeds x genomes),
    wall-clock, the credit cost at the published rate card (1 credit per hosted episode;
    fixed price for a Shadow Tournament submit), and the tournament.submit shape.
    """
    return await gym_tools.estimate_cloud_run(seeds=seeds, per_regime=per_regime,
                                              n_genomes=n_genomes)


@mcp.tool(
    name="tournament.submit",
    title="Shadow Tournament: Submit",
    annotations=annotations(read_only=False, idempotent=False, open_world=True),
    structured_output=True,
)
async def tournament_submit(
    genome: Annotated[dict, Field(
        description="Genome parameter vector (public schema) to score against the swarm's live "
                    "champion. Validate locally with warden.validate_genome first.")],
    contribute: Annotated[bool, Field(
        description="true: license the genome vector + outcome to the swarm's evolution loop as an "
                    "external challenger and pay half price. Combine with `disclosure` and/or "
                    "`strategy_code` for the strategy-contributor tier (leaderboard attribution + "
                    "league-seat eligibility after review). false (default): the vector is deleted "
                    "after scoring; only hash + outcome remain.")] = False,
    disclosure: Annotated[dict | None, Field(
        description="Strategy-contributor tier: author-written decision-logic disclosure "
                    "(version 1; fields hypothesis, universe, selection, entry_timing, risk_sizing, "
                    "weekend_hold, expected_edge — describe how the strategy decides, never code, "
                    "data dumps, or secrets). Requires contribute=true. Validated locally before "
                    "anything is sent.")] = None,
    strategy_code: Annotated[str | None, Field(
        description="Strategy-contributor tier: submit strategy CODE for STATIC LLM review. The code "
                    "is treated as inert text — it is NEVER executed by us or the swarm; the hosted "
                    "reviewer reads it with an LLM and stores only the structured explanation + a "
                    "code hash (raw code is discarded after review). Remove keys/secrets first. "
                    "Requires contribute=true.")] = None,
) -> TournamentSubmitOut:
    """Submit a genome to the hosted Shadow Tournament. Charges credits (200, or 100 with
    contribute=true), then replays the genome AND the swarm's current champion over the identical
    100-episode seed matrix on the hosted bars_1day panel — the geometry a local run cannot reach.
    Sends only the genome vector, its hash and the contribute flag, plus (strategy tier) a
    disclosure and/or strategy_code — never symbols, bars or credentials, and never executes code.
    Returns a job_id to poll with tournament.verdict.
    """
    return await tournament_tools.submit(genome=genome, contribute=contribute,
                                         disclosure=disclosure, strategy_code=strategy_code)


@mcp.tool(
    name="tournament.verdict",
    title="Shadow Tournament: Verdict",
    annotations=annotations(read_only=True, idempotent=True, open_world=True),
    structured_output=True,
)
async def tournament_verdict(
    job_id: Annotated[str, Field(description="job_id returned by tournament.submit.")],
) -> TournamentVerdictOut:
    """Poll a Shadow Tournament job. Returns the paired outcome vs the champion
    (CHALLENGER_BEATS_CHAMPION / CHALLENGER_LOSES / INCONCLUSIVE), Wilcoxon p, bootstrap CI,
    worst-regime margin, violations, and the challenger's ELO rating. Not a promotion: the
    promotion gate stays inside the swarm's registry. No charge to poll.
    """
    return await tournament_tools.verdict(job_id=job_id)


@mcp.tool(
    name="tournament.leaderboard",
    title="Shadow Tournament: Leaderboard",
    annotations=annotations(read_only=True, idempotent=True, open_world=True),
    structured_output=True,
)
async def tournament_leaderboard() -> TournamentLeaderboardOut:
    """Anonymised Shadow Tournament board: champion rating, top challengers by ELO (12-char hash
    prefixes only), run counts and the current credit prices. Free on every plan.
    """
    return await tournament_tools.leaderboard()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

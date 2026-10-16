"""swarm-warden-mcp — pre-trade invariant + leakage gate (stdio).

Server contract (Smithery-visible): title, description, homepage, icon, and
per-tool annotations/output schemas live here; the implementations stay in
swarm_mcp.tools.warden_tools.
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
from swarm_mcp.tools import warden_tools

SERVER_TITLE = "Swarm Warden MCP"
SERVER_DESCRIPTION = (
    "Pre-trade invariant and leakage auditing from the 1.21 Initiative trading swarm: "
    "order validation, cost reality checks, genome schema validation, sizing explanation, "
    "and feature-manifest leakage audits. Runs locally — no IP upload."
)

INSTRUCTIONS = (
    "Pre-trade invariant and leakage auditing — the same checker functions that gate live "
    "capital in the trading swarm, run locally against your own manifest. No IP upload: your "
    "orders, features, and genomes never leave this machine. Promotion verdicts are never "
    "issued locally; undecidable requests return INDETERMINATE_LOCAL with the exact missing "
    "statistical inputs, an audit_request block, and a cloud_job spec for the hosted "
    "tournament. This server never places, cancels, or routes orders."
)


class ValidateOrderOut(TypedDict, total=False):
    tool: str
    verdict: str
    violations: list[str]
    post_position_pct: float
    post_gross_pct: float
    house_floors: dict
    provenance: str
    safety: str
    fund_overrides: dict
    rejection_quote: str
    access: str
    error: str
    request_access_at: str
    how: str


class AuditFeaturesOut(TypedDict, total=False):
    tool: str
    verdict: str
    features: list[dict]
    violations: list[str]
    coverage: dict
    banned_actuals_sources: list[str]
    provenance: str
    access: str
    error: str
    request_access_at: str
    how: str


class CostCheckOut(TypedDict, total=False):
    tool: str
    one_way_cost_bps: float
    round_trip_cost_bps: float
    gross_edge_bps: float
    net_return_bps: float
    verdict: str
    provenance: str
    access: str
    error: str
    request_access_at: str
    how: str


class ValidateGenomeOut(TypedDict, total=False):
    tool: str
    valid: bool
    errors: list[str]
    genome_hash: str
    schema_version: int | str | None
    evaluated_schema_version: int | None
    schema_version_current: int
    note: str
    access: str
    error: str
    request_access_at: str
    how: str


class ExplainSizingOut(TypedDict, total=False):
    tool: str
    verdict: str
    qty: int
    notional: float
    trail_stop: float
    reasons: list[str]
    steps: list[dict]
    house_floors: dict
    overrides_deviation: dict
    provenance: str
    safety: str
    access: str
    error: str
    request_access_at: str
    how: str


class PromotionVerdictOut(TypedDict, total=False):
    tool: str
    verdict: str
    why: str
    missing_inputs: list[str]
    audit_request: dict
    cloud_job: dict
    regimes_required: list[str]
    safety: str
    access: str
    error: str
    request_access_at: str
    how: str


class OptimizePortfolioOut(TypedDict, total=False):
    tool: str
    verdict: str
    method: str
    target_weights: dict
    cash_proxy_symbol: str
    cash_sweep_weight: float
    gross_risk_exposure_pct: float
    estimated_portfolio_vol_annual: float
    estimated_turnover_one_way: float
    selected_risk_assets: list[str]
    filtered_out: list[dict]
    house_floors: dict
    overrides_deviation: dict
    provenance: str
    safety: str
    access: str
    error: str
    request_access_at: str
    how: str


class PlanRebalanceOut(TypedDict, total=False):
    tool: str
    verdict: str
    drawdown_pct: float
    circuit_breaker_tripped: bool
    psi_drift_tripped: bool
    effective_target_weights: dict
    skipped_within_drift_band: list[str]
    spread_violations: list[str]
    sequenced_plan: list[dict]
    house_floors: dict
    provenance: str
    safety: str
    access: str
    error: str
    request_access_at: str
    how: str


mcp = MCPServer(
    name="swarm-warden-mcp",
    title=SERVER_TITLE,
    description=SERVER_DESCRIPTION,
    instructions=INSTRUCTIONS,
    website_url=HOME_URL,
    icons=[{"src": ICON_URL, "mimeType": "image/png", "sizes": ["192x192"]}],
    version=PACKAGE_VERSION,
)


@mcp.tool(
    name="warden.validate_order",
    title="Validate Order",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def validate_order(
    order: Annotated[dict, Field(
        description="Proposed order: {symbol, notional, side: buy|sell}.")],
    equity: Annotated[float, Field(
        description="Account equity (base currency) used for the exposure floors.", gt=0.0)],
    current_positions: Annotated[dict | None, Field(
        description="Optional {symbol: notional} map of current positions.")] = None,
    floor_overrides: Annotated[dict | None, Field(
        description="Optional per-fund overrides {max_position_pct, max_gross_exposure_pct}; "
                    "the response always reports the deviation from the house floors.")] = None,
) -> ValidateOrderOut:
    """Validate a proposed order against the house floors (max position 25%, gross cap 60%).

    order: {symbol, notional, side: buy|sell}; current_positions: {symbol: notional}.
    Per-fund overrides are allowed but the response always reports the deviation from the
    house floors. Rejections quote the exact floor — the same function gating live capital.
    """
    return await warden_tools.validate_order(order=order, equity=equity,
                                             current_positions=current_positions,
                                             floor_overrides=floor_overrides)


@mcp.tool(
    name="warden.audit_features",
    title="Audit Feature Manifest",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def audit_features(
    manifest: Annotated[list[dict], Field(
        description="Feature manifest: [{name, source, value_ts, as_of}] to audit.")],
    tape_started: Annotated[str | None, Field(
        description="Optional tape start date (YYYY-MM-DD); features predating it are "
                    "flagged.")] = None,
    selected_symbols: Annotated[list[str] | None, Field(
        description="Optional challenger-selected symbols for the coverage check.")] = None,
    snapshot_universe: Annotated[list[str] | None, Field(
        description="Optional archived snapshot universe to measure coverage against.")] = None,
) -> AuditFeaturesOut:
    """Audit a feature manifest for lookahead leakage and provenance violations.

    manifest: [{name, source, value_ts, as_of}]. Flags banned actuals sources
    (open-meteo.archive etc.), features predating tape start, and unknown features fail
    closed to tier C. Optional coverage check vs MIN_COVERAGE=0.90 for challenger-selected
    symbols against the archived snapshot universe.
    """
    return await warden_tools.audit_features(manifest=manifest, tape_started=tape_started,
                                             selected_symbols=selected_symbols,
                                             snapshot_universe=snapshot_universe)


@mcp.tool(
    name="warden.cost_check",
    title="Cost Check",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def cost_check(
    gross_edge_bps: Annotated[float, Field(
        description="Claimed gross edge in basis points before costs.")],
    spread_bps: Annotated[float, Field(description="Average bid-ask spread in bps.", ge=0.0)],
    slippage_bps: Annotated[float, Field(description="Expected slippage in bps.", ge=0.0)],
    adverse_selection_bps: Annotated[float, Field(
        description="Adverse-selection drag in bps.", ge=0.0)],
) -> CostCheckOut:
    """Convert a claimed gross edge into net-of-cost reality under the pessimistic fill model.

    Spread + slippage + adverse selection are charged at both entry and exit, always against
    the position — the same model the gym scores every episode with.
    """
    return await warden_tools.cost_check(gross_edge_bps=gross_edge_bps, spread_bps=spread_bps,
                                         slippage_bps=slippage_bps,
                                         adverse_selection_bps=adverse_selection_bps)


@mcp.tool(
    name="warden.validate_genome",
    title="Validate Genome",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def validate_genome(
    genome: Annotated[dict, Field(
        description="Genome object (bounded schema, versioned).")],
) -> ValidateGenomeOut:
    """Validate a genome against the bounded schema and return its canonical genome_hash.

    The hash is the reproducible strategy identity used as challenger_id in the hosted
    tournament and in audit_request blocks.
    """
    return await warden_tools.validate_genome_tool(genome=genome)


@mcp.tool(
    name="warden.explain_sizing",
    title="Explain Sizing",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def explain_sizing(
    equity: Annotated[float, Field(
        description="Account equity (base currency).", gt=0.0)],
    atr_14: Annotated[float, Field(
        description="14-bar average true range of the instrument.", gt=0.0)],
    close: Annotated[float, Field(
        description="Current close/reference price.", gt=0.0)],
    weekend_approaching: Annotated[bool, Field(
        description="True when the position would be held into the weekend (gross headroom "
                    "applies).")] = False,
    gross_exposure: Annotated[float, Field(
        description="Current portfolio gross exposure in currency.", ge=0.0)] = 0.0,
    overnight_gap: Annotated[float, Field(
        description="Expected overnight gap (price units); gaps above 1.2x ATR halve size.",
        ge=0.0)] = 0.0,
    floor_overrides: Annotated[dict | None, Field(
        description="Optional per-fund overrides; deviation from house floors is reported.")] = None,
) -> ExplainSizingOut:
    """Step-by-step mirror of the live C# risk engine sizing: ATR sizing, position-notional
    cap, overnight-gap halving, weekend gross headroom. Reports the house floors and any
    deviation when per-fund overrides are supplied.
    """
    return await warden_tools.explain_sizing(equity=equity, atr_14=atr_14, close=close,
                                             weekend_approaching=weekend_approaching,
                                             gross_exposure=gross_exposure,
                                             overnight_gap=overnight_gap,
                                             floor_overrides=floor_overrides)


@mcp.tool(
    name="warden.promotion_verdict",
    title="Request Promotion Verdict",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def request_promotion_verdict(
    challenger_genome: Annotated[dict, Field(
        description="The challenger genome under evaluation.")],
    champion_genome: Annotated[dict | None, Field(
        description="Optional current champion genome; mutation tiers vs it are reported.")] = None,
) -> PromotionVerdictOut:
    """Request a promotion verdict. Always INDETERMINATE_LOCAL: the selection machinery is
    server-side. Names the exact missing statistical inputs and emits an audit_request plus
    a cloud_job spec for the hosted tournament.
    """
    return await warden_tools.request_promotion_verdict(challenger_genome=challenger_genome,
                                                        champion_genome=champion_genome)


@mcp.tool(
    name="warden.optimize_portfolio",
    title="Optimize Portfolio Weights",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def optimize_portfolio(
    candidates: Annotated[list[dict], Field(
        description="Candidate risk assets: [{symbol, momentum_score, realized_vol_annual, "
                    "above_trend_sma?, chandelier_triggered?, current_weight?}].")],
    method: Annotated[str, Field(
        description="Allocation method: vol_target_momentum | hrp | mvo_shrinkage.")] = "vol_target_momentum",
    cash_proxy_symbol: Annotated[str, Field(
        description="Defensive cash proxy ticker receiving unallocated weight.")] = "SGOV",
    cash_momentum_score: Annotated[float, Field(
        description="Composite momentum score of the defensive cash proxy.")] = 0.0,
    target_volatility: Annotated[float, Field(
        description="Target annualized portfolio volatility (e.g. 0.12 for 12%).",
        gt=0.0, le=1.0)] = 0.12,
    top_k: Annotated[int, Field(
        description="Maximum number of active risk slots to select.", ge=1, le=20)] = 2,
    turnover_cap: Annotated[float, Field(
        description="Maximum one-way portfolio turnover fraction per rebalance.",
        gt=0.0, le=1.0)] = 0.50,
    floor_overrides: Annotated[dict | None, Field(
        description="Optional per-fund overrides {max_position_pct, max_gross_exposure_pct}.")] = None,
) -> OptimizePortfolioOut:
    """Computes volatility-targeted dual-momentum, hierarchical risk parity, or shrinkage
    mean-variance portfolio weights under the house concentration and turnover caps.
    Residual capital is swept to the defensive cash proxy; never places or routes trades. Pro tool.
    """
    return await warden_tools.optimize_portfolio(
        candidates=candidates, method=method, cash_proxy_symbol=cash_proxy_symbol,
        cash_momentum_score=cash_momentum_score, target_volatility=target_volatility,
        top_k=top_k, turnover_cap=turnover_cap, floor_overrides=floor_overrides,
    )


@mcp.tool(
    name="warden.plan_rebalance",
    title="Pre-Trade Rebalance Risk Gate",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def plan_rebalance(
    equity: Annotated[float, Field(
        description="Current account equity in base currency.", gt=0.0)],
    high_water_mark: Annotated[float, Field(
        description="Portfolio historical high-water mark equity.", gt=0.0)],
    target_weights: Annotated[dict, Field(
        description="Target portfolio weights {symbol: weight_fraction}.")],
    current_positions: Annotated[dict | None, Field(
        description="Optional current position notionals {symbol: notional}.")] = None,
    quotes: Annotated[dict | None, Field(
        description="Optional quote snapshot {symbol: {bid, ask} or {mid, spread_bps}}.")] = None,
    cash_proxy_symbol: Annotated[str, Field(
        description="Defensive cash sweep ticker.")] = "SGOV",
    max_drawdown_limit: Annotated[float, Field(
        description="Maximum drawdown from high-water mark before circuit-breaker liquidation.",
        gt=0.0, le=0.50)] = 0.12,
    drift_threshold: Annotated[float, Field(
        description="Minimum weight drift fraction required to trigger a rebalance step.",
        ge=0.0, le=0.25)] = 0.03,
    max_spread_bps: Annotated[float, Field(
        description="Maximum allowed bid-ask spread in bps before rejecting execution.",
        gt=0.0)] = 15.0,
    limit_collar_bps: Annotated[float, Field(
        description="Marketable limit price collar around midprice in bps.",
        ge=0.0)] = 10.0,
    feature_psi: Annotated[float, Field(
        description="Optional Population Stability Index of input features (>0.25 halts risk additions).",
        ge=0.0)] = 0.0,
) -> PlanRebalanceOut:
    """Checks the portfolio drawdown circuit breaker, feature drift guard, 3% turnover
    drift buffer, and bid-ask spread gate. Sequences sell reductions before buy
    additions with marketable limit collars; never routes trades.
    """
    return await warden_tools.plan_rebalance(
        equity=equity, high_water_mark=high_water_mark, target_weights=target_weights,
        current_positions=current_positions, quotes=quotes,
        cash_proxy_symbol=cash_proxy_symbol, max_drawdown_limit=max_drawdown_limit,
        drift_threshold=drift_threshold, max_spread_bps=max_spread_bps,
        limit_collar_bps=limit_collar_bps, feature_psi=feature_psi,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()


# ---- prompt templates (7.2): copy-paste agent workflows ---------------
from swarm_mcp import server_prompts  # noqa: E402

mcp.prompt(name='pre_trade_check', title='Pre-Trade Check', description='The full pre-trade gate sequence (validate_order, cost_check, explain_sizing) with every verdict quoted verbatim; refusals are never softened.')(server_prompts.pre_trade_check)

mcp.prompt(name='audit_my_strategy', title='Audit My Strategy', description='Genome audit: validate_genome plus audit_features findings quoted exactly, then the tournament path a promotion claim actually requires.')(server_prompts.audit_my_strategy)


import functools  # noqa: E402

# ---- read-only docs resources (7.3; generated by scripts/build_docs_resources.py) ----
from swarm_mcp import server_prompts  # noqa: E402

mcp.resource("swarm://docs/methodology", name='Methodology', description='How the PIT significance verdicts are computed (neutralization, Newey-West inference, scale-free variants, staleness, placebo).', mime_type="text/markdown")(functools.partial(server_prompts._read_doc, 'methodology.md'))

mcp.resource("swarm://docs/verdicts", name='Verdict semantics', description='SIGNIFICANT / NOT_SIGNIFICANT / NOT_JUDGED / UNVERIFIED - exactly what each verdict means and never means.', mime_type="text/markdown")(functools.partial(server_prompts._read_doc, 'verdicts.md'))

mcp.resource("swarm://docs/plans", name='Plans', description='Free/Pro plan limits and the credit rate card.', mime_type="text/markdown")(functools.partial(server_prompts._read_doc, 'plans.md'))

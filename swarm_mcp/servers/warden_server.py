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
from swarm_mcp.tools import audit_tools, warden_tools

SERVER_TITLE = "Swarm Warden & Audit MCP"
SERVER_DESCRIPTION = (
    "Quant Swarm Audit (`audit.*`) and pre-trade invariant/leakage checkers (`warden.*`): "
    "guided backtest auditing (CSV/Parquet returns, trades, costs, feature manifests), "
    "order validation, cost reality checks, genome schema validation, and sizing explanation."
)

INSTRUCTIONS = (
    "Start backtest and strategy evaluations with the guided Quant Swarm Audit workflow: "
    "`audit.preflight` -> `audit.quote` -> `audit.run` -> `audit.status` -> `audit.report` -> `audit.compare`. "
    "For point checks, use `warden.*`. Over local stdio, your orders, returns, features, and genomes "
    "stay on this machine unless you explicitly opt into `mode='hosted'`. Promotion verdicts are never "
    "issued locally; `warden.promotion_verdict` runs a free local readiness check (`INDETERMINATE_LOCAL`). "
    "This server never places, cancels, or routes orders."
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
    valid_genome: bool
    schema_errors: list[str]
    highest_mutated_tier: str | None
    readiness_status: str
    next_action: str
    credits_charged: int
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
    title="Promotion Readiness & Prerequisite Check",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def request_promotion_verdict(
    challenger_genome: Annotated[dict, Field(
        description="The challenger genome under evaluation.")],
    champion_genome: Annotated[dict | None, Field(
        description="Optional current champion genome; mutation tiers vs it are reported.")] = None,
) -> PromotionVerdictOut:
    """Deterministic local promotion-readiness & prerequisite check (free, 0 credits).
    Always returns verdict=INDETERMINATE_LOCAL because promotion selection machinery
    (Deflated Sharpe, PBO cap, trials ledger) runs server-side only. Validates genome
    schema, checks Tier-A/B/C mutation eligibility vs champion_genome, returns
    readiness_status (READY_FOR_HOSTED_TOURNAMENT | BLOCKED_SCHEMA_INVALID |
    BLOCKED_TIER_B_C_REQUIRES_TAPE), names missing statistical inputs, and emits an
    audit_request plus cloud_job spec for tournament.submit.
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


class AuditGenericOut(TypedDict, total=False):
    tool: str
    preflight_id: str
    job_id: str
    job_id_a: str
    job_id_b: str
    strategy_name: str
    strategy_a: str
    strategy_b: str
    eligible: bool
    status: str
    mode: str
    validation_errors: list[str]
    input_sha256: str
    input_sha256_a: str
    input_sha256_b: str
    artifact_hashes: dict
    cost_assumptions: dict
    returns_summary: dict
    trades_summary: dict | None
    has_benchmark: bool
    has_features_manifest: bool
    coverage_matrix: dict
    package_version: str
    random_seeds: list[int]
    execution_plan: list[dict]
    not_tested_count: int
    estimated_runtime_seconds: float
    credits_cost: int
    gbp_cost: dict
    outbound_data_preview: dict
    approval_token: str
    idempotency_key: str
    idempotent_replay: bool
    credits_charged: int
    required_credits: int
    max_credits: int
    overall_assessment: str
    assessment_before: str
    assessment_after: str
    remediation_verdict: str
    counts_by_category: dict
    headline_metrics: dict
    metrics: dict
    metrics_diff: dict
    findings: list[dict]
    resolved_findings: list[dict]
    new_warnings: list[dict]
    finding_transitions: list[dict]
    reproducibility_bundle: dict
    markdown_report: str
    html_report: str
    written_files: dict
    generated_at: str
    next_step: str
    reason: str
    not_investment_advice: bool
    access: str
    error: str
    request_access_at: str
    how: str


@mcp.tool(
    name="audit.preflight",
    title="Quant Swarm Audit — Preflight & Scope Matrix",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def audit_preflight(
    returns_path: Annotated[str | None, Field(
        description="Path to CSV/Parquet/JSON strategy returns (columns: ts/date, strategy_return/return).")] = None,
    returns_rows: Annotated[list[dict] | None, Field(
        description="Inline list of strategy return rows [{ts, strategy_return}].")] = None,
    trades_path: Annotated[str | None, Field(
        description="Optional path to CSV/Parquet/JSON trade records (ts, symbol, pnl_bps, position_pct).")] = None,
    trades_rows: Annotated[list[dict] | None, Field(
        description="Optional inline trade records [{ts, symbol, pnl_bps, position_pct}].")] = None,
    benchmark_path: Annotated[str | None, Field(
        description="Optional path to CSV/Parquet/JSON benchmark return series.")] = None,
    benchmark_rows: Annotated[list[dict] | None, Field(
        description="Optional inline benchmark return rows [{ts, return}].")] = None,
    cost_assumptions_path: Annotated[str | None, Field(
        description="Optional path to JSON execution cost assumptions {spread_bps, slippage_bps, adverse_selection_bps}.")] = None,
    cost_assumptions: Annotated[dict | None, Field(
        description="Optional inline execution cost assumptions dict.")] = None,
    features_manifest_path: Annotated[str | None, Field(
        description="Optional path to JSON feature provenance manifest {as_of, features: [...]}.")] = None,
    features_manifest: Annotated[dict | None, Field(
        description="Optional inline feature provenance manifest dict.")] = None,
    strategy_name: Annotated[str, Field(
        description="Human-readable strategy or experiment identifier.")] = "candidate_strategy",
) -> AuditGenericOut:
    """Validates strategy returns, trades, benchmark, cost assumptions, and feature manifests locally at 0 credits.
    Returns a Coverage & Scope Matrix separating available checks, NOT_TESTED checks, and an outbound data preview.
    """
    return await audit_tools.preflight(
        returns_path=returns_path, returns_rows=returns_rows,
        trades_path=trades_path, trades_rows=trades_rows,
        benchmark_path=benchmark_path, benchmark_rows=benchmark_rows,
        cost_assumptions_path=cost_assumptions_path, cost_assumptions=cost_assumptions,
        features_manifest_path=features_manifest_path, features_manifest=features_manifest,
        strategy_name=strategy_name,
    )


@mcp.tool(
    name="audit.quote",
    title="Quant Swarm Audit — Deterministic Quote & Approval Token",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def audit_quote(
    preflight_id: Annotated[str | None, Field(
        description="Preflight ID returned by `audit.preflight`.")] = None,
    mode: Annotated[str, Field(
        description="Execution mode: 'local' (0 credits, £0.00) or 'hosted' (20 credits).")] = "local",
    random_seeds: Annotated[list[int] | None, Field(
        description="Deterministic bootstrap seeds (default [0, 1, 2, 3, 4]).")] = None,
    returns_path: Annotated[str | None, Field(
        description="Optional returns path if calling quote directly without preflight_id.")] = None,
    returns_rows: Annotated[list[dict] | None, Field(
        description="Optional inline returns rows if calling quote directly.")] = None,
    trades_path: Annotated[str | None, Field(
        description="Optional trades path.")] = None,
    trades_rows: Annotated[list[dict] | None, Field(
        description="Optional inline trades rows.")] = None,
    benchmark_path: Annotated[str | None, Field(
        description="Optional benchmark path.")] = None,
    benchmark_rows: Annotated[list[dict] | None, Field(
        description="Optional inline benchmark rows.")] = None,
    cost_assumptions_path: Annotated[str | None, Field(
        description="Optional cost assumptions JSON path.")] = None,
    cost_assumptions: Annotated[dict | None, Field(
        description="Optional inline cost assumptions dict.")] = None,
    features_manifest_path: Annotated[str | None, Field(
        description="Optional feature manifest JSON path.")] = None,
    features_manifest: Annotated[dict | None, Field(
        description="Optional inline feature manifest dict.")] = None,
    strategy_name: Annotated[str, Field(
        description="Strategy identifier.")] = "candidate_strategy",
) -> AuditGenericOut:
    """Prices a validated preflight manifest before execution.
    Returns the deterministic execution plan, runtime estimate, exact credit/GBP cost, and an input-bound approval_token.
    """
    return await audit_tools.quote(
        preflight_id=preflight_id, mode=mode, random_seeds=random_seeds,
        returns_path=returns_path, returns_rows=returns_rows,
        trades_path=trades_path, trades_rows=trades_rows,
        benchmark_path=benchmark_path, benchmark_rows=benchmark_rows,
        cost_assumptions_path=cost_assumptions_path, cost_assumptions=cost_assumptions,
        features_manifest_path=features_manifest_path, features_manifest=features_manifest,
        strategy_name=strategy_name,
    )


@mcp.tool(
    name="audit.run",
    title="Quant Swarm Audit — Execute Strategy Audit",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def audit_run(
    approval_token: Annotated[str, Field(
        description="Input-bound approval_token returned by `audit.quote`.")],
    idempotency_key: Annotated[str, Field(
        description="Caller-supplied idempotency key to prevent duplicate execution or billing on retries.")],
    max_credits: Annotated[int, Field(
        description="Hard spend cap in credits (0 for local runs; >=20 for hosted runs).", ge=0)] = 0,
    preflight_id: Annotated[str | None, Field(
        description="Preflight ID returned by `audit.preflight`.")] = None,
    mode: Annotated[str, Field(
        description="Execution mode: 'local' (0 credits) or 'hosted' (20 credits).")] = "local",
    random_seeds: Annotated[list[int] | None, Field(
        description="Deterministic bootstrap seeds (must match the seeds used in `audit.quote`).")] = None,
    returns_path: Annotated[str | None, Field(
        description="Optional returns path.")] = None,
    returns_rows: Annotated[list[dict] | None, Field(
        description="Optional inline returns rows.")] = None,
    trades_path: Annotated[str | None, Field(
        description="Optional trades path.")] = None,
    trades_rows: Annotated[list[dict] | None, Field(
        description="Optional inline trades rows.")] = None,
    benchmark_path: Annotated[str | None, Field(
        description="Optional benchmark path.")] = None,
    benchmark_rows: Annotated[list[dict] | None, Field(
        description="Optional inline benchmark rows.")] = None,
    cost_assumptions_path: Annotated[str | None, Field(
        description="Optional cost assumptions JSON path.")] = None,
    cost_assumptions: Annotated[dict | None, Field(
        description="Optional inline cost assumptions dict.")] = None,
    features_manifest_path: Annotated[str | None, Field(
        description="Optional feature manifest JSON path.")] = None,
    features_manifest: Annotated[dict | None, Field(
        description="Optional inline feature manifest dict.")] = None,
    strategy_name: Annotated[str, Field(
        description="Strategy identifier.")] = "candidate_strategy",
) -> AuditGenericOut:
    """Executes the Quant Swarm Audit and records a reproducible job bundle.
    Enforces approval_token verification, idempotency_key deduplication, and max_credits spend cap; never charges on preflight refusals.
    """
    return await audit_tools.run(
        approval_token=approval_token, idempotency_key=idempotency_key,
        max_credits=max_credits, preflight_id=preflight_id, mode=mode,
        random_seeds=random_seeds, returns_path=returns_path, returns_rows=returns_rows,
        trades_path=trades_path, trades_rows=trades_rows,
        benchmark_path=benchmark_path, benchmark_rows=benchmark_rows,
        cost_assumptions_path=cost_assumptions_path, cost_assumptions=cost_assumptions,
        features_manifest_path=features_manifest_path, features_manifest=features_manifest,
        strategy_name=strategy_name,
    )


@mcp.tool(
    name="audit.status",
    title="Quant Swarm Audit — Poll Job Status",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def audit_status(
    job_id: Annotated[str, Field(
        description="Audit job_id returned by `audit.run`.")],
) -> AuditGenericOut:
    """Polls an audit job by job_id in read-only mode at 0 credits.
    Returns completion status, overall diagnostic assessment, finding counts by category, and input SHA-256 hash.
    """
    return await audit_tools.status(job_id=job_id)


@mcp.tool(
    name="audit.report",
    title="Quant Swarm Audit — Export Reproducible Evidence Report",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def audit_report(
    job_id: Annotated[str, Field(
        description="Completed audit job_id returned by `audit.run`.")],
    output_html_path: Annotated[str | None, Field(
        description="Optional file path to write the standalone shareable HTML audit report.")] = None,
    output_json_path: Annotated[str | None, Field(
        description="Optional file path to write the machine-readable JSON audit bundle.")] = None,
    output_markdown_path: Annotated[str | None, Field(
        description="Optional file path to write the Markdown audit report.")] = None,
) -> AuditGenericOut:
    """Generates the structured JSON audit bundle, reproducibility manifest, and shareable HTML/Markdown reports.
    Every finding includes category (OBSERVED_PROBLEM | SENSITIVITY_WARNING | INSUFFICIENT_EVIDENCE | PASSED | NOT_TESTED), evidence, impact, next experiment, and limitation.
    """
    return await audit_tools.report(
        job_id=job_id, output_html_path=output_html_path,
        output_json_path=output_json_path, output_markdown_path=output_markdown_path,
    )


@mcp.tool(
    name="audit.compare",
    title="Quant Swarm Audit — Compare Before/After Audit Bundles",
    annotations=annotations(read_only=True, idempotent=True, open_world=False),
    structured_output=True,
)
async def audit_compare(
    job_id_a: Annotated[str, Field(
        description="Baseline / before-remediation audit job_id.")],
    job_id_b: Annotated[str, Field(
        description="Candidate / after-remediation audit job_id.")],
    output_html_path: Annotated[str | None, Field(
        description="Optional file path to write the standalone before/after comparison HTML report.")] = None,
) -> AuditGenericOut:
    """Compares two completed audit jobs (version A vs version B or before/after remediation).
    Returns a structured diff of Sharpe, net edge, cost sensitivity, drawdown, trade concentration, and resolved vs new warnings.
    """
    return await audit_tools.compare(
        job_id_a=job_id_a, job_id_b=job_id_b, output_html_path=output_html_path,
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

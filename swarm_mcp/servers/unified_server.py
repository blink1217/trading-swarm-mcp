"""Unified `quant-swarm` MCP server (`uvx quant-swarm`) with curated 12-tool default profile.

Profiles (`--profile` or `SWARM_MCP_PROFILE`):
  * `core` (default): 12 high-signal tools (~2.4k schema tokens) covering
    free offline zero-trust backtest forensics (`audit.backtest`, `warden.*`,
    `gym.probe_fragility`) and hosted Point-in-Time alternative data (`pit.*`,
    `market.*_pit`, `market.regime`).
  * `warden`: Specialist `swarm-warden-mcp` server.
  * `data`: Specialist `swarm-data-mcp` server.
  * `gym`: Specialist `swarm-gym-mcp` server.
  * `full`: All specialist tools combined in a single server.
"""
from __future__ import annotations

import argparse
import functools
import os

from swarm_mcp import server_prompts
from swarm_mcp.mcp_compat import MCPServer
from swarm_mcp.server_meta import (
    ICON_URL,
    PACKAGE_VERSION,
    SITE_URL,
    annotations,
)
from swarm_mcp.servers import data_server, gym_server, warden_server

SERVER_TITLE = "Quant Swarm — Backtest Forensics & Point-in-Time Market Data"
SERVER_DESCRIPTION = (
    "Zero-trust backtest forensics (lookahead leakage, Newey-West + Benjamini-Hochberg "
    "significance, cost survival, trade concentration) and Point-in-Time SEC/macro data."
)
INSTRUCTIONS = (
    "Use `audit.backtest` for one-shot backtest forensics at 0 credits with zero account "
    "required locally. Use `warden.*` for pre-trade position, cost, prop-challenge, and "
    "feature-leakage guardrails, and `gym.probe_fragility` for multi-regime stress testing. "
    "Hosted Point-in-Time tools (`pit.analysis`, `market.fundamentals_pit`, "
    "`market.insider_institutional_pit`) require `SWARM_MCP_ACCESS_TOKEN` from "
    "https://1.21initiative.com/mcp/. Never places or routes live orders."
)

REMOTE_INSTRUCTIONS = (
    "Use `audit.backtest_inline` for one-shot backtest forensics on inline CSV/row payloads "
    "(<= 250 KB, 0 compute credits); local file-path auditing (`audit.backtest`, `audit.preflight`, "
    "`audit.quote`, `audit.run`, `audit.report`, `audit.compare`) lives on local `stdio` "
    "(`uvx quant-swarm`). Use `warden.*` for pre-trade position, cost, prop-challenge, and "
    "feature-leakage guardrails, `gym.probe_fragility` (with inline `bars`) for multi-regime "
    "stress testing, and `pit.analysis`, `market.fundamentals_pit`, "
    "`market.insider_institutional_pit`, and `market.regime` for Point-in-Time market signals. "
    "Requires `Authorization: Bearer <SWARM_MCP_ACCESS_TOKEN>` from "
    "https://1.21initiative.com/mcp/. Never places or routes live orders."
)

CORE_STDIO_TOOL_NAMES = (
    "audit.backtest",
    "warden.audit_features",
    "warden.cost_check",
    "warden.validate_order",
    "warden.prop_challenge_check",
    "warden.defensive_posture",
    "warden.explain_sizing",
    "pit.analysis",
    "market.fundamentals_pit",
    "market.insider_institutional_pit",
    "market.regime",
    "gym.probe_fragility",
)

CORE_REMOTE_TOOL_NAMES = (
    "audit.backtest_inline",
    "warden.audit_features",
    "warden.cost_check",
    "warden.validate_order",
    "warden.prop_challenge_check",
    "warden.defensive_posture",
    "warden.explain_sizing",
    "pit.analysis",
    "market.fundamentals_pit",
    "market.insider_institutional_pit",
    "market.regime",
    "gym.probe_fragility",
)

# Registry of (name, title, read_only, idempotent, open_world, fn) across all modules
_ALL_TOOL_SPECS = [
    # Audit
    ("audit.backtest", "Quant Swarm Audit — One-Shot Backtest Forensics", True, True, False, warden_server.audit_backtest),
    ("audit.backtest_inline", "Quant Swarm Audit — Inline Backtest Forensics (Remote Safe)", True, True, False, warden_server.audit_backtest_inline),
    ("audit.preflight", "Quant Swarm Audit — Preflight & Scope Matrix", True, True, False, warden_server.audit_preflight),
    ("audit.quote", "Quant Swarm Audit — Deterministic Quote & Approval Token", True, True, False, warden_server.audit_quote),
    ("audit.run", "Quant Swarm Audit — Execute Strategy Audit", True, True, False, warden_server.audit_run),
    ("audit.status", "Quant Swarm Audit — Poll Job Status", True, True, False, warden_server.audit_status),
    ("audit.report", "Quant Swarm Audit — Export Reproducible Evidence Report", True, True, False, warden_server.audit_report),
    ("audit.compare", "Quant Swarm Audit — Compare Before/After Audit Bundles", True, True, False, warden_server.audit_compare),
    # Warden
    ("warden.validate_order", "Validate Order", True, True, False, warden_server.validate_order),
    ("warden.audit_features", "Audit Feature Manifest", True, True, False, warden_server.audit_features),
    ("warden.cost_check", "Cost Check", True, True, False, warden_server.cost_check),
    ("warden.validate_genome", "Validate Genome", True, True, False, warden_server.validate_genome),
    ("warden.explain_sizing", "Explain Sizing", True, True, False, warden_server.explain_sizing),
    ("warden.promotion_verdict", "Promotion Readiness & Prerequisite Check", True, True, False, warden_server.request_promotion_verdict),
    ("warden.optimize_portfolio", "Optimize Portfolio Weights", True, True, False, warden_server.optimize_portfolio),
    ("warden.plan_rebalance", "Pre-Trade Rebalance Risk Gate", True, True, False, warden_server.plan_rebalance),
    ("warden.prop_challenge_check", "Prop-Challenge Compliance & Drawdown Check", True, True, False, warden_server.prop_challenge_check),
    ("warden.defensive_posture", "Causal Portfolio Defensive Posture Classifier", True, True, False, warden_server.defensive_posture),
    # Data & PIT
    ("pit.analysis", "PIT Significance Explained", True, True, False, data_server.pit_analysis),
    ("market.fundamentals_pit", "Point-in-Time Fundamental Quality", True, True, True, data_server.market_fundamentals_pit),
    ("market.insider_institutional_pit", "Point-in-Time Smart Money & Filing Flow", True, True, True, data_server.market_insider_institutional_pit),
    ("market.supply_chain_pit", "Point-in-Time Supply-Chain & Cross-Border Flow", True, True, True, data_server.market_supply_chain_pit),
    ("market.regime", "Regime Snapshot", True, True, True, data_server.regime_snapshot),
    ("market.pulse", "Market Pulse", True, True, True, data_server.market_pulse),
    ("market.sentiment", "Sentiment Pulse", True, True, True, data_server.sentiment_pulse),
    ("market.climate", "Operating-Area Climate Research", True, True, True, data_server.climate_snapshot),
    ("market.microstructure", "Microstructure Snapshot", True, True, True, data_server.microstructure_snapshot),
    ("volume.forecast", "Volume Forecast", True, True, True, data_server.volume_forecast),
    ("market.screen", "Screen Universe", True, True, True, data_server.screen_universe),
    ("market.rank", "Cross-Sectional Rank", True, True, True, data_server.cross_sectional_rank),
    ("market.flow", "Institutional & Off-Exchange Flow", True, True, True, data_server.market_flow),
    ("market.macro_nowcast", "Macro & Volatility Regime Nowcast", True, True, True, data_server.market_macro_nowcast),
    ("market.transfer_entropy", "Directed Cross-Asset Information Flow", True, True, True, data_server.market_transfer_entropy),
    ("features.build", "Build Feature Vector", True, True, False, data_server.build_features),
    ("cache.warm", "Warm Cache", False, True, True, data_server.cache_warm),
    ("cache.stats", "Cache Stats", True, True, False, data_server.cache_stats),
    ("cache.offline", "Toggle Offline Mode", False, False, False, data_server.offline_mode),
    # Gym & Optional Tournament
    ("gym.probe_fragility", "Probe Fragility", True, True, False, gym_server.probe_fragility),
    ("gym.label_regimes", "Label Regimes", True, True, False, gym_server.label_regimes),
    ("gym.paired_preview", "Paired Preview", True, True, False, gym_server.paired_preview),
    ("gym.estimate_cloud_run", "Estimate Cloud Run", True, True, False, gym_server.estimate_cloud_run),
    ("gym.evaluate_graph", "Evaluate ExpressionGraph", True, True, False, gym_server.evaluate_graph),
    ("gym.prop_challenge_sim", "Prop-Challenge Monte Carlo Pass & Breach Simulator", True, True, False, gym_server.prop_challenge_sim),
    ("gym.evaluate_formula", "Symbolic Formula Causal & Lookahead Evaluator", True, True, True, gym_server.evaluate_formula),
    ("tournament.submit", "Shadow Tournament: Submit", False, False, True, gym_server.tournament_submit),
    ("tournament.verdict", "Shadow Tournament: Verdict", True, True, True, gym_server.tournament_verdict),
    ("tournament.leaderboard", "Shadow Tournament: Leaderboard", True, True, True, gym_server.tournament_leaderboard),
    ("tournament.my_entries", "Shadow Tournament: My Entries & Rescore History", True, True, True, gym_server.tournament_my_entries),
    ("tournament.forward_league", "Forward League Pre-Commitment Settlement Status & Ledger", True, True, False, gym_server.tournament_forward_league),
]

REMOTE_OMITTED_TOOLS = frozenset({
    "cache.warm",
    "cache.offline",
    "cache.stats",
    "audit.backtest",
    "audit.preflight",
    "audit.quote",
    "audit.run",
    "audit.report",
    "audit.compare",
})


def attach_server_prompts_and_resources(srv: MCPServer, server_key: str = "unified") -> None:
    """Attach only the prompts and resources whose backing tools exist on `server_key`."""
    if server_key in ("warden", "unified"):
        srv.prompt(
            name="pre_trade_check",
            title="Pre-Trade Check",
            description="The full pre-trade gate sequence (validate_order, cost_check, explain_sizing) with every verdict quoted verbatim; refusals are never softened.",
        )(server_prompts.pre_trade_check)
    if server_key == "warden":
        srv.prompt(
            name="audit_my_strategy",
            title="Audit My Strategy",
            description="Genome audit: validate_genome plus audit_features findings quoted exactly, then the tournament path a promotion claim actually requires.",
        )(server_prompts.audit_my_strategy)
    if server_key in ("data", "unified"):
        srv.prompt(
            name="explain_pit_significance",
            title="Explain PIT Significance",
            description="Explained PIT-correlation significance for one underlying: quotes every (dataset|metric|horizon) verdict verbatim.",
        )(server_prompts.explain_pit_significance)
    if server_key == "data":
        srv.prompt(
            name="regime_check",
            title="Regime Check",
            description="The current market-regime label and derived snapshot for one underlying, with the warden checks to run before any order.",
        )(server_prompts.regime_check)
    if server_key == "gym":
        srv.prompt(
            name="stress_test_genome",
            title="Stress Test Genome",
            description="Regime labels (with UNDERPOWERED flags) + fragility probe + a costed hand-off to tournament.submit; never submits automatically and never elevates gym output to a promotion verdict.",
        )(server_prompts.stress_test_genome)

    srv.resource(
        "swarm://docs/methodology",
        name="Methodology",
        description="How the PIT significance verdicts are computed (neutralization, Newey-West inference, scale-free variants, staleness, placebo).",
        mime_type="text/markdown",
    )(functools.partial(server_prompts._read_doc, "methodology.md"))
    if server_key in ("data", "unified"):
        srv.resource(
            "swarm://docs/pit-datasets",
            name="PIT datasets",
            description="Which point-in-time dataset families are searched, and the known-at rule that makes each one point-in-time.",
            mime_type="text/markdown",
        )(functools.partial(server_prompts._read_doc, "pit-datasets.md"))
    srv.resource(
        "swarm://docs/verdicts",
        name="Verdict semantics",
        description="SIGNIFICANT / NOT_SIGNIFICANT / NOT_JUDGED / UNVERIFIED - exactly what each verdict means and never means.",
        mime_type="text/markdown",
    )(functools.partial(server_prompts._read_doc, "verdicts.md"))
    srv.resource(
        "swarm://docs/plans",
        name="Plans",
        description="Free/Pro plan limits and the credit rate card.",
        mime_type="text/markdown",
    )(functools.partial(server_prompts._read_doc, "plans.md"))


def _attach_prompts_and_resources(srv: MCPServer) -> None:
    attach_server_prompts_and_resources(srv, "unified")


def build_unified_server(profile: str = "core", *, remote_http: bool = False) -> MCPServer:
    norm = (profile or "core").strip().lower()
    if norm == "warden" and not remote_http:
        return warden_server.mcp
    if norm == "data" and not remote_http:
        return data_server.mcp
    if norm == "gym" and not remote_http:
        return gym_server.mcp
    if norm not in ("core", "full", "warden", "data", "gym"):
        raise ValueError(f"Unknown profile {profile!r}; expected core|warden|data|gym|full")

    srv = MCPServer(
        name="quant-swarm",
        title=SERVER_TITLE,
        description=SERVER_DESCRIPTION,
        instructions=REMOTE_INSTRUCTIONS if remote_http else INSTRUCTIONS,
        website_url=SITE_URL,
        icons=[{"src": ICON_URL, "mimeType": "image/png", "sizes": ["192x192"]}],
        version=PACKAGE_VERSION,
    )

    if norm == "core":
        wanted = set(CORE_REMOTE_TOOL_NAMES if remote_http else CORE_STDIO_TOOL_NAMES)
    else:
        wanted = {spec[0] for spec in _ALL_TOOL_SPECS}
        if remote_http:
            wanted -= REMOTE_OMITTED_TOOLS

    spec_by_name = {spec[0]: spec for spec in _ALL_TOOL_SPECS}
    ordered_names = (
        (CORE_REMOTE_TOOL_NAMES if remote_http else CORE_STDIO_TOOL_NAMES)
        if norm == "core"
        else [spec[0] for spec in _ALL_TOOL_SPECS if spec[0] in wanted]
    )

    for name in ordered_names:
        _, title, ro, idem, ow, fn = spec_by_name[name]
        srv.tool(
            name=name,
            title=title,
            annotations=annotations(read_only=ro, idempotent=idem, open_world=ow),
            structured_output=True,
        )(fn)

    attach_server_prompts_and_resources(
        srv,
        "unified" if norm in ("core", "full") else norm,
    )
    return srv


mcp = build_unified_server("core", remote_http=False)
remote_mcp = build_unified_server("core", remote_http=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="quant-swarm",
        description="Quant Swarm MCP server (default: 12-tool core profile).",
    )
    parser.add_argument(
        "--profile",
        choices=("core", "warden", "data", "gym", "full"),
        default=os.environ.get("SWARM_MCP_PROFILE", "core"),
        help="Tool profile to expose over stdio (default: core = 12 tools).",
    )
    args = parser.parse_args(argv)
    srv = mcp if args.profile == "core" else build_unified_server(args.profile, remote_http=False)
    srv.run(transport="stdio")


if __name__ == "__main__":
    main()

"""Response envelope: coverage/limits/escalation, INDETERMINATE_LOCAL, cloud_job.

Thresholds are imported from the vendored guardrails checker subset so the
numbers quoted to users are the same constants that gate live capital — never
hand-copied.
"""
from __future__ import annotations

import swarm_mcp.vendor_path  # noqa: F401

from budget import BREAKER_FRACTION, MONTHLY_CAP_USD  # guardrails (vendored)
from gates import (  # guardrails (vendored)
    MAX_PBO,
    MIN_COVERAGE,
    MIN_DSR_MARGIN,
    MIN_EPISODES,
    MIN_WORST_REGIME_MARGIN,
    REGIMES,
    TAPE_DEPTH_TAPE_ELIGIBLE_WEEKS,
    TAPE_DEPTH_TIER_A_WEEKS,
)

VERDICT_INDETERMINATE = "INDETERMINATE_LOCAL"

TAPE_REPLAY_ROADMAP_NOTE = (
    "tape-tier replay is ROADMAP, not available: the tape_replay service is hosted-only; "
    "tier-B/C scoring cannot be produced locally and is never neutral-filled")

HOSTED_PANEL_NOTE = (
    "the hosted bars_1day panel (single-writer, MERGE-deduped, data-bridge-maintained) "
    "is the source that satisfies the tape-depth gates")


def depth_weeks(oldest_session, newest_session) -> float:
    if oldest_session is None or newest_session is None:
        return 0.0
    return max(0.0, (newest_session - oldest_session).days / 7.0)


def limits_block(local_depth_weeks: float) -> dict:
    return {
        "local_depth_weeks": round(local_depth_weeks, 2),
        "tier_a_gate_weeks": TAPE_DEPTH_TIER_A_WEEKS,
        "tape_eligible_weeks": TAPE_DEPTH_TAPE_ELIGIBLE_WEEKS,
        "meets_tier_a_gate": local_depth_weeks >= TAPE_DEPTH_TIER_A_WEEKS,
        "meets_tape_eligible": local_depth_weeks >= TAPE_DEPTH_TAPE_ELIGIBLE_WEEKS,
    }


def escalation_block(reasons: list[str]) -> dict:
    return {
        "target": HOSTED_PANEL_NOTE,
        "reasons": reasons,
        "tape_tier_replay": TAPE_REPLAY_ROADMAP_NOTE,
    }


def coverage_block(rows_from_cache: int, rows_from_api: int, sessions_by_symbol: dict) -> dict:
    return {
        "rows_from_cache": rows_from_cache,
        "rows_from_api": rows_from_api,
        "sessions_by_symbol": sessions_by_symbol,
    }


def promotion_missing_inputs() -> list[str]:
    return [
        (f"paired episodes >= MIN_EPISODES={MIN_EPISODES} on identical FULL-HISTORY paths — local probes "
         "run capped seeds on your cached window; the hosted tournament runs the complete bars_1day panel"),
        f"PBO <= MAX_PBO={MAX_PBO} requires combinatorially-symmetric CV splits (CSCV) over the full history — hosted compute only",
        f"DSR margin > MIN_DSR_MARGIN={MIN_DSR_MARGIN} requires the monotonic cumulative n_trials held by the hosted registry",
        (f"worst-regime margin > MIN_WORST_REGIME_MARGIN={MIN_WORST_REGIME_MARGIN} requires all "
         f"{len(REGIMES)} regimes ({', '.join(REGIMES)}) x multiple seeds on identical paths"),
    ]


def audit_request_block(genome_hash: str, violation_summary: list[str]) -> dict:
    return {
        "genome_hash": genome_hash,
        "violation_summary": violation_summary,
        "note": ("hash + violation summary only — no proprietary payload is included; "
                 "to proceed, request access at https://1.21initiative.com/ — the audit "
                 "booking flow captures contact details and is how hosted keys are issued and metered"),
    }


def cloud_job_block(genome_hash: str, seeds: list[int], per_regime: int, reason: str) -> dict:
    from swarm_mcp import plans

    return {
        "endpoint": "tournament.submit",
        "handoff": ("call tournament.submit(genome=<the genome>, contribute=<bool>) — the Shadow "
                    "Tournament re-scores it against the swarm's live champion on the hosted panel "
                    f"({plans.TOURNAMENT['episodes']} paired episodes, above MIN_EPISODES) and returns "
                    "a job_id to poll with tournament.verdict"),
        "body": {
            "challenger_id": genome_hash,
            "seeds": seeds,
            "per_regime": per_regime,
            "panel": "bars_1day",
        },
        "hosted_geometry": {
            "seeds": plans.TOURNAMENT["seeds"],
            "per_regime": plans.TOURNAMENT["per_regime"],
            "episodes": plans.TOURNAMENT["episodes"],
        },
        "credits": {
            "full": plans.TOURNAMENT["credits_full"],
            "contribute": plans.TOURNAMENT["credits_contribute"],
            "note": ("contribute=true licenses the genome vector + outcome to the swarm's evolution "
                     "loop and halves the price; contribute=false deletes the vector after scoring"),
        },
        "prerequisites": (
            "a funded credit pack (Pro); challenger_id is the genome_hash emitted by "
            "warden.validate_genome and must match the submitted genome"),
        "reason_local_insufficient": reason,
    }


def indeterminate_local(missing_inputs: list[str] | None = None, *,
                        genome_hash: str | None = None,
                        violation_summary: list[str] | None = None,
                        seeds: list[int] | None = None,
                        per_regime: int | None = None,
                        reason: str | None = None) -> dict:
    out: dict = {
        "verdict": VERDICT_INDETERMINATE,
        "why": ("the selection machinery (objective scoring + promotion gates) is server-side; "
                "local tools are checkers and never issue promotion verdicts"),
        "missing_inputs": missing_inputs or promotion_missing_inputs(),
    }
    if genome_hash is not None:
        out["audit_request"] = audit_request_block(genome_hash, violation_summary or [])
    if seeds is not None and per_regime is not None and genome_hash is not None:
        out["cloud_job"] = cloud_job_block(genome_hash, seeds, per_regime,
                                           reason or "local compute is capped for statistical honesty")
    return out


TOOL_UNLOCK_PREVIEWS: dict[str, str] = {
    "gym.prop_challenge_sim": (
        "Unlock 500-path Monte Carlo prop-firm evaluation simulations to compute your exact "
        "pass probability, daily drawdown breach risk, consistency-lock rate, and optimal "
        "risk-per-trade before paying a challenge reset fee."
    ),
    "gym.probe_fragility": (
        "Unlock multi-regime fragility replay across high_vol, drawdown, chop, melt_up, and "
        "gap_heavy episodes to expose which regime breaks your strategy before deploying capital."
    ),
    "gym.paired_preview": (
        "Unlock paired head-to-head episode replay comparing your challenger genome against a "
        "baseline on identical market paths with Wilcoxon p-values and worst-regime margins."
    ),
    "gym.evaluate_graph": (
        "Unlock multi-node ExpressionGraph (strategy-spec-v3) DAG evaluation across all 5 "
        "causal market regimes with turnover and worst-regime net bps diagnostics."
    ),
    "tournament.submit": (
        "Unlock the 100-paired-episode Hosted Shadow Tournament against the live Swarm Champion "
        "(half price with contribute=true) and enter the ELO leaderboard."
    ),
    "warden.optimize_portfolio": (
        "Unlock constrained portfolio weight optimization (volatility-targeted dual momentum, "
        "Hierarchical Risk Parity, and Ledoit-Wolf shrinkage MVO) with house exposure caps."
    ),
    "market.flow": (
        "Unlock point-in-time FINRA dark-pool short-volume z-scores, SEC Form 4 insider net "
        "buy-sell ratios, 13F institutional breadth, and Congressional trading flow."
    ),
    "market.fundamentals_pit": (
        "Unlock filing-date-gated SEC XBRL fundamentals: Piotroski F-Score, Altman Z-Score, "
        "accruals quality, and gross profitability with zero restatement lookahead."
    ),
    "market.macro_nowcast": (
        "Unlock point-in-time macro regime nowcasts (yield-curve slope, credit-spread z-score, "
        "VIX term structure, real-rate proxy, and shipping stress)."
    ),
    "market.screen": (
        "Unlock multi-factor cross-sectional screening across up to 50 symbols per call with "
        "point-in-time momentum, volatility, and quality filters."
    ),
    "gym.evaluate_formula": (
        "Unlock causal symbolic formula evaluation (CS_ZScore, CS_Rank, TS_Corr, KaufmanER) "
        "with a strict no-lookahead truncation check, Spearman rank IC, Newey-West t_hac, "
        "and quantile spread diagnostics."
    ),
    "market.insider_institutional_pit": (
        "Unlock point-in-time filing-gated insider cluster buying buckets, institutional "
        "accumulation percentiles, quiet-session stealth absorption, and smart-money divergence z-scores."
    ),
    "market.supply_chain_pit": (
        "Unlock point-in-time freight velocity percentiles, cross-border flow pressure, "
        "and pre-earnings supply-chain conviction buckets."
    ),
    "market.rank": (
        "Unlock cross-sectional percentile ranking and composite factor scoring across your "
        "symbol universe."
    ),
}


def upgrade_required(tool: str, plan: str, upgrade_url: str) -> dict:
    """Non-error envelope for a Pro tool attempted on a non-paid plan.

    The tool stays LISTED in Cursor/Claude; each attempt returns this instead of
    an error, so every attempt is a conversion opportunity rather than a failure.
    Advisory on the open-source stdio servers; hard-enforced on the hosted
    streamable-HTTP endpoint.
    """
    preview = TOOL_UNLOCK_PREVIEWS.get(
        tool,
        f"Unlock {tool} plus 50 symbols/call, 10-year backfill, and hosted gym/tournament compute.",
    )
    sep = "&" if "?" in upgrade_url else "?"
    checkout_url = f"{upgrade_url}{sep}upgrade=1&pack=credits-10k&tool={tool}"
    return {
        "tool": tool,
        "access": "UPGRADE_REQUIRED",
        "reason": "plan_required",
        "plan": plan,
        "upgrade_url": upgrade_url,
        "checkout_url": checkout_url,
        "unlock_preview": preview,
        "starter_pack": {
            "pack_id": "credits-10k",
            "credits": 10_000,
            "price_gbp": 19,
            "price_usd": 24,
            "price_eur": 22,
            "validity_days": 90,
            "billing": "one-time payment, never auto-renews",
        },
        "note": (f"{preview} This tool needs a funded credit pack (Pro) — buy a one-time pack at "
                 f"{checkout_url} (10k credits for £19 (~$24 / €22) or 100k for £149 (~$189 / €175), 90-day validity, no "
                 "subscription). Credits pay for relay data calls, hosted compute (1 credit per "
                 "simulated episode) and Shadow Tournament runs. Free tokens include the warden "
                 "checkers, market.pulse, market.sentiment, market.regime, cache.stats, "
                 "cache.offline and tournament.leaderboard. This client-side check is advisory; "
                 "quota and plan enforcement happen server-side."),
    }


def budget_reference() -> dict:
    return {
        "monthly_cap_usd": MONTHLY_CAP_USD,
        "breaker_fraction": BREAKER_FRACTION,
        "semantics": ("the hosted evolution loop charges every job pre-flight against a monthly ledger "
                      "and opens the budget breaker at the breaker fraction of the cap"),
    }

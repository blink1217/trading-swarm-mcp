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
    return {
        "endpoint": "POST /tournament/run",
        "handoff": "manual in v1 — submit via the Strategy Validation Audit booking flow at https://1.21initiative.com/",
        "body": {
            "challenger_id": genome_hash,
            "seeds": seeds,
            "per_regime": per_regime,
            "panel": "bars_1day",
        },
        "prerequisites": (
            "the genome must first be registered in the hosted registry (done during the audit booking); "
            "challenger_id is the genome_hash emitted by validate_genome"),
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


def budget_reference() -> dict:
    return {
        "monthly_cap_usd": MONTHLY_CAP_USD,
        "breaker_fraction": BREAKER_FRACTION,
        "semantics": ("the hosted evolution loop charges every job pre-flight against a monthly ledger "
                      "and opens the budget breaker at the breaker fraction of the cap"),
    }

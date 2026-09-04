"""Commercial plans: free / pro / institutional.

Naming discipline: "plan" is the commercial gate; "tier" is reserved for the
guardrail data tiers A/B/C (provenance.FEATURE_TIERS, tiers.GENE_TIERS). Never
mix the two vocabularies.

This module is the CLIENT-SIDE view of the plans. The server-side source of
truth is the site's src/mcp/plans.ts; enforcement of anything that costs money
(relay quota, symbol counts, hosted Pro tools) happens server-side. The checks
here are ADVISORY on the open-source stdio servers by design: the free plan is
the zero-cost, un-enforceable surface and we do not build DRM. The hosted
streamable-HTTP endpoint (swarm_mcp.servers.http_server) enforces Pro tools for
real because it is ours.

tests/test_entitlements.py asserts these sets stay in sync with plans.ts.
"""
from __future__ import annotations

FREE = "free"
PRO = "pro"
INSTITUTIONAL = "institutional"

PAID_PLANS = (PRO, INSTITUTIONAL)

# Billing model: Pro is CREDIT-BASED, not a subscription. Customers buy
# one-time credit packs (fixed call count, 90-day validity) on the site; the
# relay meters `credits_remaining` server-side. monthly_calls is 0 for pro —
# there is deliberately no unlimited tier. Mirrored from src/mcp/plans.ts.
PLANS: dict[str, dict] = {
    FREE: {"monthly_calls": 250, "max_symbols": 10},
    PRO: {"monthly_calls": 0, "max_symbols": 50},
    INSTITUTIONAL: {"monthly_calls": None, "max_symbols": None},
}

# One-time credit packs (mirror of src/mcp/plans.ts CREDIT_PACKS):
# pack_id -> {calls, price_gbp}
CREDIT_PACKS: dict[str, dict] = {
    "credits-10k": {"calls": 10_000, "price_gbp": 19},
    "credits-100k": {"calls": 100_000, "price_gbp": 149},
}

CREDITS_VALIDITY_DAYS = 90

# Compute rate card (mirror of src/mcp/plans.ts COMPUTE_RATES). One credit is
# the unit for everything that costs the operator money: a relay data call, a
# hosted single-shot Pro tool, one simulated gym episode on the hosted
# endpoint, or a Shadow Tournament submit. Local stdio execution is NEVER
# metered — your CPU, your electricity.
COMPUTE_RATES: dict[str, int] = {
    "relay.call": 1,
    "hosted.tool": 1,
    "hosted.episode": 1,
    "tournament.submit": 200,
}

# Shadow Tournament geometry (mirror of plans.ts TOURNAMENT): the submitted
# genome is re-scored against the swarm's live champion on the hosted panel,
# 5 regimes x 4 per regime x 5 seeds x 2 genomes = 200 paired episodes.
TOURNAMENT: dict = {
    "seeds": [0, 1, 2, 3, 4],
    "per_regime": 4,
    "regimes": 5,
    "genomes": 2,
    "episodes": 200,
    "credits_full": 200,
    "credits_contribute": 100,
}

# Contributors license the genome vector + outcome to the swarm's own
# evolution loop (external challengers for the proposer) and pay half.
CONTRIBUTE_DISCOUNT = 0.5

FREE_TOOLS = frozenset({
    "warden.validate_order",
    "warden.cost_check",
    "warden.explain_sizing",
    "warden.validate_genome",
    "warden.audit_features",
    "market.pulse",
    "market.regime",
    "market.sentiment",
    "market.climate",
    "cache.stats",
    "cache.offline",
    "tournament.leaderboard",
})

PRO_TOOLS = frozenset({
    "features.build",
    "cache.warm",
    "warden.promotion_verdict",
    "gym.label_regimes",
    "gym.probe_fragility",
    "gym.paired_preview",
    "gym.estimate_cloud_run",
    "market.microstructure",
    "volume.forecast",
    "market.screen",
    "market.rank",
    "tournament.verdict",
    "tournament.submit",
})


def tournament_credits(contribute: bool) -> int:
    return int(TOURNAMENT["credits_contribute"] if contribute else TOURNAMENT["credits_full"])


def tool_requires_paid_plan(tool: str, plan: str | None, status: str | None = "active") -> bool:
    """Advisory check: would this tool need a paid plan for this entitlement?

    Used by the stdio servers' shared runner. A missing plan (entitlement
    unknown) fails open — advice only; real enforcement lives on the site's
    relay/verify endpoints and the hosted HTTP server. A paid plan whose
    status is not ``active`` (``exhausted``/``expired`` credit pool,
    ``inactive``) is treated as free.
    """
    if plan in PAID_PLANS and status == "active":
        return False
    return tool in PRO_TOOLS

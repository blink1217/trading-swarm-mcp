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

FREE_TOOLS = frozenset({
    "warden.validate_order",
    "warden.cost_check",
    "warden.explain_sizing",
    "warden.validate_genome",
    "warden.audit_features",
    "market.pulse",
    "market.regime",
    "market.sentiment",
    "cache.stats",
    "cache.offline",
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
    "tournament.leaderboard",
})


def tool_requires_paid_plan(tool: str, plan: str | None, status: str | None = "active") -> bool:
    """Advisory check: would this tool need a paid plan for this entitlement?

    Used by the stdio servers' shared runner. A missing plan (entitlement
    unknown) fails open — advice only; real enforcement lives on the site's
    relay/verify endpoints and the hosted HTTP server.
    """
    if plan in PAID_PLANS and status == "active":
        return False
    return tool in PRO_TOOLS

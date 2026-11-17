"""Shared tool runner: access gate -> advisory plan gate -> telemetry -> redaction.

Previously duplicated verbatim in tools/data_tools.py, tools/warden_tools.py and
tools/gym_tools.py; kept in one place so the access gate, the advisory plan gate
and the error envelope cannot drift between servers.

The plan gate here is ADVISORY (see swarm_mcp/plans.py): the open-source stdio
servers cannot and should not enforce commercial limits — the site's relay,
/api/mcp/verify and the hosted streamable-HTTP endpoint are the enforcement
points. A free entitlement hitting a Pro tool returns an UPGRADE_REQUIRED
envelope (not an error), so the tool keeps listing and each attempt becomes a
conversion event.
"""
from __future__ import annotations

import os
import time

from swarm_mcp import access, envelope, plans, redaction, request_context, telemetry

# Pure local-CPU tools that run 100% offline over stdio with zero network calls
# and zero token required when SWARM_MCP_ACCESS_TOKEN is unset. Hosted/relay
# tools (pit.*, market.*, features.build, cache.warm, tournament.*,
# gym.estimate_cloud_run) still require a valid site token.
LOCAL_OFFLINE_TOOLS = frozenset({
    "audit.backtest",
    "audit.backtest_inline",
    "audit.preflight",
    "audit.quote",
    "audit.run",
    "audit.status",
    "audit.report",
    "audit.compare",
    "warden.validate_order",
    "warden.cost_check",
    "warden.explain_sizing",
    "warden.validate_genome",
    "warden.audit_features",
    "warden.plan_rebalance",
    "warden.prop_challenge_check",
    "warden.defensive_posture",
    "warden.promotion_verdict",
    "warden.optimize_portfolio",
    "cache.stats",
    "cache.offline",
    "gym.label_regimes",
    "gym.probe_fragility",
    "gym.paired_preview",
    "gym.evaluate_graph",
    "gym.prop_challenge_sim",
    "gym.evaluate_formula",
})


def _can_run_offline_without_token(tool: str) -> bool:
    if os.environ.get("SWARM_MCP_TRANSPORT", "").strip().lower() == "http":
        return False
    if request_context.is_hosted():
        return False
    if os.environ.get(access.ACCESS_TOKEN_ENV, "").strip():
        return False
    return tool in LOCAL_OFFLINE_TOOLS


def _upgrade_response(tool: str, ent: access.Entitlement) -> dict:
    return envelope.upgrade_required(tool, ent.plan, ent.upgrade_url)


async def run_tool(tool: str, fn):
    t0 = time.perf_counter()
    try:
        if not _can_run_offline_without_token(tool):
            access.check_access()
            ent = access.current_entitlement()
            if ent is not None and not ent.allows_tool(tool):
                telemetry.record(tool, False, (time.perf_counter() - t0) * 1000.0)
                return redaction.redact(_upgrade_response(tool, ent))
        out = await fn()
        telemetry.record(tool, True, (time.perf_counter() - t0) * 1000.0)
        return redaction.redact(out)
    except access.AccessRequired as e:
        telemetry.record(tool, False, (time.perf_counter() - t0) * 1000.0)
        return {"tool": tool, "access": "REQUIRED",
                "error": redaction.redact_text(str(e)),
                **access.request_instructions()}
    except Exception as e:
        telemetry.record(tool, False, (time.perf_counter() - t0) * 1000.0)
        return {"tool": tool, "error": redaction.redact_text(f"{type(e).__name__}: {e}")}

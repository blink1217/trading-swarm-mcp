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

import time

from swarm_mcp import access, envelope, plans, redaction, telemetry


def _upgrade_response(tool: str, ent: access.Entitlement) -> dict:
    return envelope.upgrade_required(tool, ent.plan, ent.upgrade_url)


async def run_tool(tool: str, fn):
    t0 = time.perf_counter()
    try:
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

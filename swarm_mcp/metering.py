"""Hosted compute metering — the piece that keeps the hosted endpoint from
running at a loss.

Relay data calls are metered by the site when they happen. Hosted *compute*
(gym replays, feature builds, promotion-verdict envelopes) used to be free:
any Pro token could burn Cloud Run CPU indefinitely, and a token whose credit
pool had expired kept its Pro entitlement. This module prices every hosted
Pro tool call in credits per the published rate card (plans.COMPUTE_RATES)
and charges the site's ``POST /api/mcp/meter`` BEFORE the tool runs. The site
answers with the same structured 402 refusals the relay uses, so a client sees
the real reason and the upgrade URL.

Local stdio execution is never metered — it is the user's CPU. Only the hosted
streamable-HTTP server (swarm_mcp.servers.http_server) calls ``charge``.
"""
from __future__ import annotations

import math

import httpx

from swarm_mcp import plans, relay

REGIME_COUNT = int(plans.TOURNAMENT["regimes"])
DEFAULT_LOCAL_SEEDS = 2
TIMEOUT_S = 15.0

# Pro tools whose hosted cost is one cheap computation.
SINGLE_SHOT_TOOLS = frozenset({
    "features.build",
    "warden.promotion_verdict",
    "gym.label_regimes",
    "gym.estimate_cloud_run",
})

# Pro tools priced per simulated episode (5 regimes x per_regime x seeds
# [x 2 genomes for the paired preview]).
EPISODE_TOOLS = {"gym.probe_fragility": 1, "gym.paired_preview": 2}

# Pro tools that are metered elsewhere: market.* / cache.warm / volume.forecast
# hit the relay (metered there per call); tournament.submit is charged by the
# site on submit; tournament.verdict / leaderboard are authenticate-only polls.
RELAY_METERED_TOOLS = frozenset({
    "cache.warm",
    "market.microstructure",
    "volume.forecast",
    "market.screen",
    "market.rank",
    "tournament.submit",
    "tournament.verdict",
})


class MeterRefused(RuntimeError):
    """The site refused to charge this call (402/429) or was unreachable."""

    def __init__(self, message: str, *, status_code: int, reason: str | None = None,
                 upgrade_url: str | None = None, quota: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.upgrade_url = upgrade_url
        self.quota = quota or {}


def _episodes(arguments: dict, genomes: int) -> int:
    seeds = arguments.get("seeds")
    n_seeds = len(seeds) if isinstance(seeds, list) and seeds else DEFAULT_LOCAL_SEEDS
    per_regime = arguments.get("per_regime", 2)
    try:
        per_regime = max(1, int(per_regime))
    except (TypeError, ValueError):
        per_regime = 2
    return REGIME_COUNT * per_regime * n_seeds * genomes


def compute_units(tool: str, arguments: dict | None) -> int:
    """Credits the hosted endpoint charges for this tools/call (0 = not metered here)."""
    args = arguments if isinstance(arguments, dict) else {}
    if tool in EPISODE_TOOLS:
        return max(1, _episodes(args, EPISODE_TOOLS[tool]) * plans.COMPUTE_RATES["hosted.episode"])
    if tool in SINGLE_SHOT_TOOLS:
        return plans.COMPUTE_RATES["hosted.tool"]
    return 0


def meter_url() -> str:
    return f"{relay.relay_base()}/meter"


def charge(token: str, tool: str, units: int) -> dict:
    """Charge ``units`` credits for ``tool`` against ``token``. Fail closed.

    Returns the site's payload (``charged``, ``quota``, ``plan``) on success and
    raises :class:`MeterRefused` for 402/429/unreachable — a meter that fails
    open is not a meter.
    """
    if units <= 0:
        return {"ok": True, "charged": 0}
    try:
        r = httpx.post(meter_url(), json={"tool": tool, "units": int(units)},
                       headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT_S)
    except httpx.HTTPError as e:
        raise MeterRefused(f"compute meter unreachable ({type(e).__name__}) — hosted compute "
                           "is refused rather than served unmetered", status_code=503) from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 200 and isinstance(body, dict) and body.get("ok"):
        return body
    reason = body.get("reason") if isinstance(body, dict) else None
    error = body.get("error") if isinstance(body, dict) else None
    raise MeterRefused(
        error or f"compute meter refused (HTTP {r.status_code})",
        status_code=r.status_code if r.status_code in (402, 429) else 503,
        reason=str(reason) if reason else None,
        upgrade_url=body.get("upgrade_url") if isinstance(body, dict) else None,
        quota=body.get("quota") if isinstance(body, dict) and isinstance(body.get("quota"), dict) else None,
    )


def seeds_for_budget(credits: int, per_regime: int, genomes: int = 1) -> int:
    """How many seeds a credit budget affords at the hosted rate (helper for docs/tools)."""
    per_seed = REGIME_COUNT * max(1, per_regime) * max(1, genomes) * plans.COMPUTE_RATES["hosted.episode"]
    return max(0, math.floor(credits / per_seed))

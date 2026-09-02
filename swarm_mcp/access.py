"""Access gate: every tool call requires a token issued via https://1.21initiative.com/.

The token is the GTM gate. Prospects request access at the site (which captures
their contact details); the site issues a token and meters its usage. The server
validates the token and refuses every tool call without a valid one — Cursor and
Claude still LIST the tools, so trying one is what routes the prospect to the site.

Token resolution order:
1. Request-scoped token (remote streamable-HTTP servers): set by the HTTP auth
   middleware from the Authorization header, validated once per request.
2. SWARM_MCP_ACCESS_TOKEN (local stdio servers): validated with a 300s cache.

Validation order:
1. No token -> denied (envelope points at the site).
2. Token equals SWARM_MCP_LOCAL_TOKEN (documented dev/offline bootstrap) -> granted
   with a full (pro) entitlement: it is a local override on your own machine.
3. Otherwise the token is POSTed to SWARM_MCP_TOKEN_VERIFY_URL
   (default https://1.21initiative.com/api/mcp/verify). The site answers
   {"ok": true, "plan": ..., "status": ..., "features": [...], "quota": {...},
   "upgrade_url": ...}; older site revisions answer just {"ok": true}, which is
   read as a free entitlement (backwards compatible). The verify call IS the
   usage meter — only the token itself is sent, never symbols, genomes, prices,
   or provider credentials.
4. Network failure or rejection -> denied (fail closed: a gate that fails open
   is not a gate).

Positive validations (with their parsed Entitlement) are cached in-process for
VALID_TTL_S so a tool session does not hammer the site and transient site
outages do not instantly lock out a recently-verified token.

Plan vocabulary note: "plan" (free/pro/institutional) is the commercial gate;
"tier" stays reserved for guardrail data tiers A/B/C.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from swarm_mcp import request_context

SITE_URL = "https://1.21initiative.com/"
ACCESS_TOKEN_ENV = "SWARM_MCP_ACCESS_TOKEN"
VERIFY_URL_ENV = "SWARM_MCP_TOKEN_VERIFY_URL"
DEFAULT_VERIFY_URL = "https://1.21initiative.com/api/mcp/verify"
LOCAL_TOKEN_ENV = "SWARM_MCP_LOCAL_TOKEN"
VALID_TTL_S = 300.0

_cache: dict[str, tuple[float, "Entitlement"]] = {}
_MAX_CACHE = 512


class AccessRequired(RuntimeError):
    """A tool was called without a valid access token."""


@dataclass
class Entitlement:
    """What the site says a token may do. Parsed from the enriched /verify
    payload; a legacy {"ok": true} payload yields the free default."""

    plan: str = "free"
    status: str = "active"
    features: list[str] | None = None  # None = site did not say; use local sets
    quota: dict = field(default_factory=dict)
    upgrade_url: str = SITE_URL

    @property
    def is_paid(self) -> bool:
        from swarm_mcp import plans

        return self.plan in plans.PAID_PLANS

    @property
    def is_funded(self) -> bool:
        """Paid plan with a live entitlement. The site reports ``exhausted`` /
        ``expired`` for a pro token whose credit pool is empty or past its
        90-day window; such a token is NOT funded and gets the free surface."""
        from swarm_mcp import plans

        return self.plan in plans.PAID_PLANS and self.status == "active"

    def allows_tool(self, tool: str) -> bool:
        """Server-authoritative tool allowance when the site sends a features
        list; falls back to the client's advisory plan sets."""
        from swarm_mcp import plans

        if self.is_funded:
            return True
        if self.features is not None:
            return tool in self.features
        return tool not in plans.PRO_TOOLS


def request_instructions() -> dict:
    return {
        "request_access_at": SITE_URL,
        "how": (f"request a token at {SITE_URL} (Strategy Validation Audit access), then set "
                f"{ACCESS_TOKEN_ENV} in this server's env configuration"),
    }


def resolve_token() -> str:
    return request_context.current_token.get() or os.environ.get(ACCESS_TOKEN_ENV, "").strip()


def verify_entitlement(token: str) -> Entitlement | None:
    """Validate an explicit token and return its entitlement (None = rejected).

    Used by the remote HTTP auth middleware; the entitlement is stashed on the
    request context there so the hosted endpoint can hard-refuse Pro tools.
    """
    if not token:
        return None
    return _verify(token)


def validate_token(token: str) -> bool:
    """Validate an explicit token (boolean form for older call sites)."""
    return verify_entitlement(token) is not None


def check_access() -> None:
    token = resolve_token()
    if not token:
        raise AccessRequired(
            f"access token required — no {ACCESS_TOKEN_ENV} is set. "
            f"Request one at {SITE_URL}")
    if _verify(token) is None:
        raise AccessRequired(f"access token rejected — request one at {SITE_URL}")


def current_entitlement() -> Entitlement | None:
    """Entitlement for the currently resolved token, if recently verified."""
    token = resolve_token()
    if not token:
        return None
    entry = _cache.get(token)
    if entry is None:
        return None
    until, ent = entry
    if time.monotonic() >= until:
        return None
    return ent


def _parse_entitlement(payload: dict) -> Entitlement:
    """Parse the enriched /verify payload, tolerating the legacy {ok:true} shape."""
    features = payload.get("features")
    if not isinstance(features, list):
        features = None
    else:
        features = [str(f) for f in features]
    quota = payload.get("quota")
    if not isinstance(quota, dict):
        quota = {}
    upgrade_url = payload.get("upgrade_url")
    if not isinstance(upgrade_url, str) or not upgrade_url.strip():
        upgrade_url = SITE_URL
    return Entitlement(
        plan=str(payload.get("plan") or "free"),
        status=str(payload.get("status") or "active"),
        features=features,
        quota=quota,
        upgrade_url=upgrade_url,
    )


def _verify(token: str) -> Entitlement | None:
    now = time.monotonic()
    entry = _cache.get(token)
    if entry is not None and now < entry[0]:
        return entry[1]

    local = os.environ.get(LOCAL_TOKEN_ENV, "").strip()
    if local and token == local:
        ent = Entitlement(plan="pro", status="active", features=None,
                          quota={}, upgrade_url=SITE_URL)
        _remember(token, now, ent)
        return ent

    import httpx

    verify_url = os.environ.get(VERIFY_URL_ENV, "").strip() or DEFAULT_VERIFY_URL
    try:
        r = httpx.post(verify_url, json={"token": token}, timeout=10.0)
    except httpx.HTTPError:
        return None
    if r.status_code == 200:
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and bool(payload.get("ok")):
            ent = _parse_entitlement(payload)
            _remember(token, now, ent)
            return ent
    return None


def _remember(token: str, now: float, ent: Entitlement) -> None:
    if len(_cache) >= _MAX_CACHE:
        _cache.clear()
    _cache[token] = (now + VALID_TTL_S, ent)


def reset_access_cache() -> None:
    _cache.clear()

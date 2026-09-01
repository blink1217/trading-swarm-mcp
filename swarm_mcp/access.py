"""Access gate: every tool call requires a token issued via https://1.21initiative.com/.

The token is the GTM gate. Prospects request access at the site (which captures
their contact details); the site issues a token and meters its usage. The server
validates the token and refuses every tool call without a valid one — Cursor and
Claude still LIST the tools, so trying one is what routes the prospect to the site.

Validation order:
1. No SWARM_MCP_ACCESS_TOKEN -> denied (envelope points at the site).
2. Token equals SWARM_MCP_LOCAL_TOKEN (documented dev/offline bootstrap) -> granted.
3. Otherwise the token is POSTed to SWARM_MCP_TOKEN_VERIFY_URL
   (default https://1.21initiative.com/api/mcp/verify); 200 + {"ok": true} grants
   access. The verify call IS the usage meter — only the token itself is sent,
   never symbols, genomes, prices, or provider credentials.
4. Network failure or rejection -> denied (fail closed: a gate that fails open
   is not a gate).

Positive validations are cached in-process for VALID_TTL_S so a tool session does
not hammer the site and transient site outages do not instantly lock out a
recently-verified token.
"""
from __future__ import annotations

import os
import time

SITE_URL = "https://1.21initiative.com/"
ACCESS_TOKEN_ENV = "SWARM_MCP_ACCESS_TOKEN"
VERIFY_URL_ENV = "SWARM_MCP_TOKEN_VERIFY_URL"
DEFAULT_VERIFY_URL = "https://1.21initiative.com/api/mcp/verify"
LOCAL_TOKEN_ENV = "SWARM_MCP_LOCAL_TOKEN"
VALID_TTL_S = 300.0

_cache_ok_until = 0.0


class AccessRequired(RuntimeError):
    """A tool was called without a valid access token."""


def request_instructions() -> dict:
    return {
        "request_access_at": SITE_URL,
        "how": (f"request a token at {SITE_URL} (Strategy Validation Audit access), then set "
                f"{ACCESS_TOKEN_ENV} in this server's env configuration"),
    }


def check_access() -> None:
    global _cache_ok_until
    token = os.environ.get(ACCESS_TOKEN_ENV, "").strip()
    if not token:
        raise AccessRequired(
            f"access token required — no {ACCESS_TOKEN_ENV} is set. "
            f"Request one at {SITE_URL}")

    now = time.monotonic()
    if now < _cache_ok_until:
        return

    local = os.environ.get(LOCAL_TOKEN_ENV, "").strip()
    if local and token == local:
        _cache_ok_until = now + VALID_TTL_S
        return

    import httpx

    verify_url = os.environ.get(VERIFY_URL_ENV, "").strip() or DEFAULT_VERIFY_URL
    try:
        r = httpx.post(verify_url, json={"token": token}, timeout=10.0)
    except httpx.HTTPError as e:
        raise AccessRequired(
            f"access token could not be verified ({type(e).__name__} contacting the verify "
            f"endpoint) — request access at {SITE_URL}") from e
    if r.status_code == 200:
        try:
            ok = bool(r.json().get("ok"))
        except ValueError:
            ok = False
        if ok:
            _cache_ok_until = now + VALID_TTL_S
            return
    raise AccessRequired(f"access token rejected — request one at {SITE_URL}")


def reset_access_cache() -> None:
    global _cache_ok_until
    _cache_ok_until = 0.0

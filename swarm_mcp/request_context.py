"""Per-request state for the remote (streamable-HTTP) MCP servers.

Each incoming HTTP request carries its own access token; the auth middleware
validates it once and stashes it here so the access gate and the hosted data
relay client resolve the token per request instead of from server env.
"""
from __future__ import annotations

import contextvars

current_token: contextvars.ContextVar[str] = contextvars.ContextVar(
    "swarm_mcp_request_token", default="")

# Entitlement (plan/status/features/quota) resolved for the current request by
# the hosted HTTP auth middleware; None when the token carries no entitlement
# info or outside a request scope.
current_entitlement: contextvars.ContextVar = contextvars.ContextVar(
    "swarm_mcp_request_entitlement", default=None)

"""Shared server metadata + tool annotation helpers (mcp >= 2.0).

These are the Smithery-visible contract fields: server title/description/
homepage/icon and per-tool readOnly/destructive/idempotent/openWorld hints.
"""
from __future__ import annotations

from mcp.types import ToolAnnotations

PACKAGE_VERSION = "0.4.0"
SITE_URL = "https://1.21initiative.com/mcp/"
HOME_URL = "https://1.21initiative.com/"
ICON_URL = "https://1.21initiative.com/icons/icon-192.png"

# Canonical hosted streamable-HTTP endpoint. server.json, the npm launcher,
# the llama-index integration and this module must agree on this URL —
# tests/test_registry_metadata.py enforces it. Deploy-time operators can
# override at runtime via SWARM_MCP_REMOTE_URL without touching this constant.
REMOTE_BASE_URL = "https://swarm-mcp-503318750546.europe-west1.run.app"


def annotations(
    *,
    read_only: bool = True,
    idempotent: bool = True,
    open_world: bool = False,
) -> ToolAnnotations:
    """Standard annotations: every tool here is non-destructive by design."""
    return ToolAnnotations(
        title=None,
        read_only_hint=read_only,
        destructive_hint=False,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )

"""MCP SDK compatibility: requires mcp >= 2.0 (MCPServer).

Tool registration uses mcp 2.x-only features (ToolAnnotations, structured
output schemas, server title/description/website_url/icons), so the mcp 1.x
FastMCP fallback is gone; pin `mcp>=2.0` (see pyproject.toml).
"""
from __future__ import annotations

try:
    from mcp.server.mcpserver import MCPServer
except ModuleNotFoundError as e:  # pragma: no cover
    raise ModuleNotFoundError(
        "quant-swarm requires mcp>=2.0 (MCPServer). Upgrade with: "
        "pip install -U 'mcp>=2.0'"
    ) from e

__all__ = ["MCPServer"]

"""MCP SDK compatibility: requires mcp >= 2.0 (MCPServer).

Tool registration uses mcp 2.x-only features (ToolAnnotations, structured
output schemas, server title/description/website_url/icons), so the mcp 1.x
FastMCP fallback is gone; pin `mcp>=2.0` (see pyproject.toml).
"""
from __future__ import annotations

import inspect

try:
    from mcp.server.mcpserver import MCPServer as _BaseMCPServer
except ModuleNotFoundError as e:  # pragma: no cover
    raise ModuleNotFoundError(
        "quant-swarm requires mcp>=2.0 (MCPServer). Upgrade with: "
        "pip install -U 'mcp>=2.0'"
    ) from e


class MCPServer(_BaseMCPServer):
    """MCPServer wrapper that normalizes docstring indentation across Python 3.12 and 3.13+."""

    def tool(self, *args, **kwargs):
        decorator = super().tool(*args, **kwargs)

        def _wrap(fn):
            if getattr(fn, "__doc__", None):
                fn.__doc__ = inspect.cleandoc(fn.__doc__)
            return decorator(fn)

        return _wrap


__all__ = ["MCPServer"]


"""MCP SDK compatibility: requires mcp >= 2.0 (MCPServer).

Tool registration uses mcp 2.x-only features (ToolAnnotations, structured
output schemas, server title/description/website_url/icons), so the mcp 1.x
FastMCP fallback is gone; pin `mcp>=2.0` (see pyproject.toml).
"""
from __future__ import annotations

import functools
import inspect
import json

from mcp.types import CallToolResult, TextContent

try:
    from mcp.server.mcpserver import MCPServer as _BaseMCPServer
except ModuleNotFoundError as e:  # pragma: no cover
    raise ModuleNotFoundError(
        "quant-swarm requires mcp>=2.0 (MCPServer). Upgrade with: "
        "pip install -U 'mcp>=2.0'"
    ) from e


def _is_error_envelope(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return True
    if result.get("access") in ("REQUIRED", "UPGRADE_REQUIRED"):
        return True
    return False


def _to_error_call_result(result: dict) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result))],
        structured_content=result,
        is_error=True,
    )


class MCPServer(_BaseMCPServer):
    """MCPServer wrapper that normalizes docstrings and maps error envelopes to isError=True."""

    def tool(self, *args, **kwargs):
        decorator = super().tool(*args, **kwargs)

        def _wrap(fn):
            if getattr(fn, "__doc__", None):
                fn.__doc__ = inspect.cleandoc(fn.__doc__)

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def _async_handler(*f_args, **f_kwargs):
                    res = await fn(*f_args, **f_kwargs)
                    if _is_error_envelope(res):
                        return _to_error_call_result(res)
                    return res

                return decorator(_async_handler)

            @functools.wraps(fn)
            def _sync_handler(*f_args, **f_kwargs):
                res = fn(*f_args, **f_kwargs)
                if _is_error_envelope(res):
                    return _to_error_call_result(res)
                return res

            return decorator(_sync_handler)

        return _wrap


__all__ = ["MCPServer"]


"""MCP SDK compatibility: mcp 2.x renamed FastMCP to MCPServer.

Both generations expose the same decorator surface used here (.tool, .run).
"""
from __future__ import annotations

try:
    from mcp.server.mcpserver import MCPServer
except ModuleNotFoundError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore

__all__ = ["MCPServer"]

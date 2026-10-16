"""Reusable MCP stdio driver — the scripts/record_demo.py:38-68 pattern promoted
to a library: official `mcp` SDK `stdio_client` + `ClientSession` against
``python -m swarm_mcp.servers.{data,warden,gym}_server`` subprocesses,
initialize -> tools/list -> tools/call with structured payloads parsed the way
record_demo does. This closes the gap where the suite had no stdio e2e at all.

``fixture_weather=True`` routes the data server through harness.serve so the
committed Open-Meteo fixture stands in for the live keyless API (Tier 0 stays
offline); warden/gym always launch the plain server modules.
"""
from __future__ import annotations

import json
import sys
import time
from contextlib import asynccontextmanager

from harness import fixtures

SERVER_MODULES = {
    "data": "swarm_mcp.servers.data_server",
    "warden": "swarm_mcp.servers.warden_server",
    "gym": "swarm_mcp.servers.gym_server",
}


def server_command(server: str, *, fixture_weather: bool = False) -> tuple[str, list[str]]:
    if server not in SERVER_MODULES:
        raise ValueError(f"unknown server {server!r}; expected one of {sorted(SERVER_MODULES)}")
    if fixture_weather:
        return sys.executable, ["-m", "harness.serve", server, "--fixture-weather"]
    return sys.executable, ["-m", SERVER_MODULES[server]]


def normalize_tool(tool) -> dict:
    """Wire-contract view of one tools/list entry: name + input/output schemas +
    annotations (read_only/idempotent/destructive/open_world hints)."""
    dump = tool.model_dump(by_alias=True, exclude_none=False)
    ann = dump.get("annotations") or {}
    return {
        "name": dump["name"],
        "inputSchema": dump.get("inputSchema"),
        "outputSchema": dump.get("outputSchema"),
        "annotations": {
            "readOnlyHint": ann.get("readOnlyHint"),
            "idempotentHint": ann.get("idempotentHint"),
            "destructiveHint": ann.get("destructiveHint"),
            "openWorldHint": ann.get("openWorldHint"),
        },
    }


class Driver:
    """One initialized stdio session against a single swarm MCP server."""

    def __init__(self, session, server: str):
        self.session = session
        self.server = server
        self._tools: list[dict] | None = None

    async def list_tools(self) -> list[dict]:
        if self._tools is None:
            result = await self.session.list_tools()
            self._tools = [normalize_tool(t) for t in result.tools]
        return self._tools

    async def tool_contract(self, name: str) -> dict:
        for entry in await self.list_tools():
            if entry["name"] == name:
                return entry
        raise KeyError(f"{self.server} server does not expose {name!r}")

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        t0 = time.perf_counter()
        result = await self.session.call_tool(name, arguments or {})
        duration_ms = (time.perf_counter() - t0) * 1000.0
        text = result.content[0].text if result.content else None
        payload = None
        if text:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
        if payload is None:
            structured = getattr(result, "structuredContent", None)
            if structured is None:
                structured = getattr(result, "structured_content", None)
            payload = structured if isinstance(structured, dict) else {"raw_text": text}
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        return {
            "tool": name,
            "arguments": arguments or {},
            "payload": payload,
            "is_error": bool(is_error),
            "text": text,
            "duration_ms": duration_ms,
        }


@asynccontextmanager
async def open_session(server: str, env: dict[str, str], *, fixture_weather: bool = False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command, args = server_command(server, fixture_weather=fixture_weather)
    params = StdioServerParameters(command=command, args=args, env=env,
                                   cwd=str(fixtures.ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield Driver(session, server)


async def collect_contracts(server: str, env: dict[str, str],
                            *, fixture_weather: bool = False) -> list[dict]:
    """One initialize -> tools/list round-trip (the contract snapshot source)."""
    async with open_session(server, env, fixture_weather=fixture_weather) as driver:
        return await driver.list_tools()

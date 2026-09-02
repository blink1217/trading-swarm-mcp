"""Connection configs + LlamaIndex tool spec for quant-swarm.

The three servers ship in one package (``quant-swarm``); one access
token from https://1.21initiative.com/mcp/ covers all of them. Usage::

    from llama_index.tools.quant_swarm import QuantSwarmToolSpec

    spec = QuantSwarmToolSpec("swarm-data", access_token="121_...")
    tools = await spec.to_tool_list()
    # agent = FunctionCallingAgentWorker.from_tools(tools, llm=llm).as_agent()

Requires ``uv`` (https://docs.astral.sh/uv/) on PATH for the stdio servers,
and the official MCP bridge: ``pip install llama-index-tools-mcp``.
"""
from __future__ import annotations

import os

PACKAGE = "quant-swarm"

SERVERS = {
    "swarm-data": "swarm-data-mcp",
    "swarm-warden": "swarm-warden-mcp",
    "swarm-gym": "swarm-gym-mcp",
}

# Canonical hosted endpoint (see swarm_mcp/server_meta.py — tests keep the
# distribution artifacts in agreement). Override at runtime with
# SWARM_MCP_REMOTE_URL if the service moves behind a custom domain.
REMOTE_BASE = os.environ.get(
    "SWARM_MCP_REMOTE_URL",
    "https://swarm-mcp-503318750546.europe-west1.run.app",
).rstrip("/")


def server_configs(access_token: str | None = None, package: str = PACKAGE) -> dict:
    """Stdio connection configs for the three servers (uvx-based).

    The token falls back to ``SWARM_MCP_ACCESS_TOKEN``; without any token the
    servers still start and list every tool, and each call returns an
    access-required envelope pointing at the free signup.
    """
    token = access_token if access_token is not None else os.environ.get(
        "SWARM_MCP_ACCESS_TOKEN", "")
    configs = {}
    for name, script in SERVERS.items():
        cfg: dict = {"command": "uvx", "args": ["--from", package, script], "env": {}}
        if token:
            cfg["env"]["SWARM_MCP_ACCESS_TOKEN"] = token
        configs[name] = cfg
    return configs


def remote_urls() -> dict:
    """Hosted streamable-HTTP endpoints (bearer token or ``?apiToken=``)."""
    return {
        "swarm-data": f"{REMOTE_BASE}/mcp/data",
        "swarm-warden": f"{REMOTE_BASE}/mcp/warden",
        "swarm-gym": f"{REMOTE_BASE}/mcp/gym",
    }


class QuantSwarmToolSpec:
    """Exposes one quant-swarm server as LlamaIndex tools.

    Wraps ``llama_index.tools.mcp.McpToolSpec`` so every tool the server
    lists becomes a LlamaIndex tool with its native schema.
    """

    def __init__(self, server: str = "swarm-data", access_token: str | None = None):
        if server not in SERVERS:
            raise ValueError(f"unknown server {server!r} — one of {sorted(SERVERS)}")
        try:
            from llama_index.tools.mcp import BasicMCPClient, McpToolSpec
        except ImportError as exc:  # pragma: no cover — optional dependency
            raise ImportError(
                "QuantSwarmToolSpec needs the official MCP bridge: "
                "pip install llama-index-tools-mcp") from exc
        cfg = server_configs(access_token)[server]
        self._client = BasicMCPClient(cfg["command"], cfg["args"], env=cfg["env"] or None)
        self._spec = McpToolSpec(client=self._client)

    async def to_tool_list(self):
        """All tools listed by the server, as LlamaIndex tool objects."""
        return await self._spec.to_tool_list()

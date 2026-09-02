"""Thin LlamaIndex tool-spec wrapper for the quant-swarm servers.

This is a distribution shim, not a reimplementation: it hands LlamaIndex the
three stdio servers through ``llama-index-tools-mcp``'s ``McpToolSpec`` (the
official MCP bridge) plus a dependency-free helper for the connection configs.
"""
from llama_index.tools.quant_swarm.base import QuantSwarmToolSpec, server_configs

__all__ = ["QuantSwarmToolSpec", "server_configs"]

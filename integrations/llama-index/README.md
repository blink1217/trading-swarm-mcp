# llama-index-tools-quant-swarm

LlamaIndex tool-spec wrapper for [quant-swarm](https://github.com/blink1217/trading-swarm-mcp) —
the pre-trade checkers that gate live capital, as MCP servers.

One package, three servers, one access token (free at
<https://1.21initiative.com/mcp/>):

| Server | Hook |
| --- | --- |
| `swarm-data` | Derived-only market signals + point-in-time feature building |
| `swarm-warden` | Pre-trade invariant + leakage auditing |
| `swarm-gym` | Regime-fragility probing |

## Install

```bash
pip install llama-index-tools-quant-swarm
# needs uv on PATH for the stdio servers: https://docs.astral.sh/uv/
```

## Usage

```python
import asyncio
from llama_index.tools.quant_swarm import QuantSwarmToolSpec

async def main():
    spec = QuantSwarmToolSpec("swarm-warden", access_token="121_...")
    tools = await spec.to_tool_list()
    for tool in tools:
        print(tool.metadata.name)

asyncio.run(main())
```

`server_configs()` returns the raw stdio configs if you want to drive the
official `llama-index-tools-mcp` bridge yourself; `remote_urls()` lists the
hosted streamable-HTTP endpoints.

The wrapper is deliberately thin: schemas, listing, and call semantics come
straight from the server. Without a token every tool still lists; each call
returns an access-required envelope pointing at the free signup.

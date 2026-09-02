# LangChain — quant-swarm

No separate package: use
[`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters)
against the `quant-swarm` servers directly.

## Install

```bash
pip install langchain-mcp-adapters langgraph
# needs uv on PATH for the stdio servers: https://docs.astral.sh/uv/
```

## stdio (local, all three servers)

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI  # or any LangChain chat model

client = MultiServerMCPClient({
    "swarm-data": {
        "command": "uvx",
        "args": ["--from", "quant-swarm", "swarm-data-mcp"],
        "transport": "stdio",
        "env": {"SWARM_MCP_ACCESS_TOKEN": "<token from https://1.21initiative.com/mcp/>"},
    },
    "swarm-warden": {
        "command": "uvx",
        "args": ["--from", "quant-swarm", "swarm-warden-mcp"],
        "transport": "stdio",
        "env": {"SWARM_MCP_ACCESS_TOKEN": "<token>"},
    },
    "swarm-gym": {
        "command": "uvx",
        "args": ["--from", "quant-swarm", "swarm-gym-mcp"],
        "transport": "stdio",
        "env": {"SWARM_MCP_ACCESS_TOKEN": "<token>"},
    },
})

tools = await client.get_tools()
agent = create_react_agent(ChatOpenAI(model="gpt-4o"), tools)
```

## Hosted streamable-HTTP (no local install)

```python
client = MultiServerMCPClient({
    "swarm-data": {
        "url": "https://swarm-mcp-503318750546.europe-west1.run.app/mcp/data",
        "transport": "streamable_http",
        "headers": {"Authorization": "Bearer <token>"},
    },
})
```

(Equivalently, append `?apiToken=<token>` to the URL instead of the header.)

## Notes

- One access token covers all three servers; get one free at
  <https://1.21initiative.com/mcp/>. Without a token, tools still list and
  each call returns an access-required envelope.
- Pro tools (e.g. `features.build`, `gym.probe_fragility`,
  `warden.promotion_verdict`) list on the free plan but are refused at call
  time with a `402` + `upgrade_url` — pro capacity is one-time credit packs,
  no subscription.
- The package is `quant-swarm` on PyPI; Smithery listing is
  the listing slug is [`blink-kt/quant-swarm`](https://smithery.ai/servers/blink-kt/quant-swarm).

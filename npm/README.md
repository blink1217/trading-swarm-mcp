# quant-swarm-mcp (npm launcher)

Node launcher for the
[quant-swarm](https://github.com/blink1217/trading-swarm-mcp) MCP
servers — the pre-trade checkers that gate live capital — so Node-only users
can install from npm. The package is `quant-swarm` on
PyPI (`quant-swarm`); `quant-swarm-mcp` is this npm launcher (Smithery slug `quant-swarm`,
slug `quant-swarm`).

```bash
# stdio via uvx (requires uv: https://docs.astral.sh/uv/)
npx quant-swarm-mcp data
npx quant-swarm-mcp swarm-warden-mcp

# hosted streamable-HTTP via mcp-remote (no local Python needed)
npx quant-swarm-mcp --remote data
```

Servers: `data` / `warden` / `gym` (aliases for `swarm-data-mcp`,
`swarm-warden-mcp`, `swarm-gym-mcp`).

Set `SWARM_MCP_ACCESS_TOKEN` to your token from
<https://1.21initiative.com/mcp/> (free plan issued instantly). Without a
token the servers still start and list every tool; each call returns an
access-required envelope pointing at the signup page.

In an MCP client config (`mcpServers`):

```json
{
  "swarm-data": {
    "command": "npx",
    "args": ["-y", "quant-swarm-mcp", "data"],
    "env": { "SWARM_MCP_ACCESS_TOKEN": "<token>" }
  }
}
```

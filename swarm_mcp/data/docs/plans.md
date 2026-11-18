### Plans
- **Free Offline (`stdio`, no token):** Unlimited local `audit.backtest`, `warden.*`, `cache.*`, and inline-bars `gym.*` runs (`0 credits`).
- **Free Hosted Token (`SWARM_MCP_ACCESS_TOKEN`):** 250 relay calls/month (10 symbols/call) + 100 bonus Pro trial credits on signup at [1.21initiative.com/mcp/](https://1.21initiative.com/mcp/).
- **One-Time Credit Packs (No Subscription):** `10,000` credits (£19 / ~$24) or `100,000` credits (£149 / ~$189), valid 90 days (1 credit per relay call, single-shot Pro tool, or hosted gym episode).

---

## Security, Privacy & Agent Honesty Rules

- **Strict Transport Isolation:** Local file-path auditing (`audit.backtest`, `audit.preflight`, `audit.quote`, `audit.run`, `audit.report`, `audit.compare`) and local SQLite cache controls (`cache.warm`, `cache.stats`, `cache.offline`) exist **only** on `stdio` and are jailed to your active workspace (`SWARM_ALLOWED_WORKSPACE` or `cwd`). Remote HTTP (`https://mcp.1.21initiative.com/mcp`) accepts only bounded inline CSV/row payloads (`<= 250 KB`) with zero server disk I/O and enforces per-token burst rate limits.
- **Zero Order Execution:** Read-only diagnostic and research tools only—never places, modifies, or routes live orders. Diagnostic evidence only, not investment advice.
- **Recommended Agent Rule:** When calling any `quant-swarm` tool, instruct your agent to **quote its verdict verbatim** and **never upgrade a raw p-value** into a significance claim when `bh_q_value > 0.05` or `INSUFFICIENT_EVIDENCE` is returned.

## Documentation & Links
- **Product & API Keys:** [https://1.21initiative.com/mcp/](https://1.21initiative.com/mcp/)
- **Source & Issues:** [https://github.com/blink1217/trading-swarm-mcp](https://github.com/blink1217/trading-swarm-mcp)
- **License:** MIT (`swarm_mcp` wrapper/tools) AND Source-Available (`swarm_mcp/vendored/guardrails/LICENSE.md`).

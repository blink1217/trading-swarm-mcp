# `quant-swarm` — Zero-Trust Backtest Forensics & Point-in-Time Market Data (MCP)

<!-- mcp-name: io.github.blink1217/quant-swarm -->

Audit trading backtests locally for **lookahead bias, Newey-West + Benjamini-Hochberg overlap-adjusted significance, outlier concentration, and realistic transaction-cost decay**—100% offline and free—and query **as-filed Point-in-Time SEC EDGAR & macro signals** with zero restatement leakage.

---

## 30-Second Quickstart (No Account or API Key Required Locally)

### Claude Code
```bash
claude mcp add quant-swarm -- uvx quant-swarm
```

### Cursor / Claude Desktop / Windsurf (`mcp.json`)
```json
{
  "mcpServers": {
    "quant-swarm": {
      "command": "uvx",
      "args": ["quant-swarm"]
    }
  }
}
```

### Remote Streamable-HTTP (`claude.ai`, ChatGPT, Web MCP Connectors)
```text
https://mcp.1.21initiative.com/mcp
```
*(Metadata & `tools/list` require no auth; tool execution uses OAuth 2.1 PKCE or `Authorization: Bearer <SWARM_MCP_ACCESS_TOKEN>` from [1.21initiative.com/mcp/](https://1.21initiative.com/mcp/). Specialist HTTP endpoints: `/mcp` [12 core tools], `/mcp/data` [16 tools], `/mcp/warden` [12 tools], `/mcp/gym` [11 tools] — local-disk `cache.*` and local-filesystem `audit.*` path tools are intentionally omitted on HTTP.)*

---

## First Prompt to Try

> *"Run `audit.backtest` on `./returns.csv` and `./trades.csv` assuming 3 bps spread and 5 bps slippage across 25 parameter trials. Check whether net Sharpe survives 2x cost stress and Newey-West + Benjamini-Hochberg adjustment."*

**Sample Verdict Returned (`0 credits`, `0 bytes` sent off your machine):**
```json
{
  "tool": "audit.backtest",
  "overall_assessment": "PROBLEMS_OBSERVED",
  "metrics": {
    "sharpe_gross_annualized": 2.14,
    "sharpe_net_annualized": 0.31,
    "newey_west_t_stat": 1.18,
    "bh_q_value": 0.42,
    "breakeven_cost_multiplier": 1.12,
    "top5_pnl_share_pct": 68.4
  }
}
```

---

## Default `core` Profile (12 High-Signal Tools, ~2.4k Schema Tokens)

`uvx quant-swarm` loads a lean 12-tool core profile so your coding agent never suffers tool-catalog context bloat:

| Surface | Tools Included in Default `core` Profile | Auth / Metering |
| :--- | :--- | :--- |
| **Free Offline Backtest & Risk Forensics** | `audit.backtest` *(or `audit.backtest_inline` on HTTP)*, `warden.audit_features`, `warden.cost_check`, `warden.validate_order`, `warden.prop_challenge_check`, `warden.defensive_posture`, `warden.explain_sizing`, `gym.probe_fragility` | **Free & 100% Offline** (`stdio` needs zero token and makes zero network calls) |
| **Hosted Point-in-Time (`pit.*`) Data** | `pit.analysis`, `market.fundamentals_pit`, `market.insider_institutional_pit`, `market.regime` | **`SWARM_MCP_ACCESS_TOKEN`** ([free 250 calls/mo + 100 bonus Pro trial credits](https://1.21initiative.com/mcp/); one-time £19 / 10k or £149 / 100k credit packs, no subscription) |

### Specialist Profiles (`--profile`)
Need the full 48-tool local `stdio` specialist suites (step-by-step `audit.preflight`/`quote`/`run`/`compare`, local SQLite cache management, portfolio optimization, symbolic alpha formula evaluation, or optional experimental Shadow Tournament benchmarking)?
```bash
uvx quant-swarm --profile full     # All 48 local stdio tools across Warden, Data & Gym (39 on remote HTTP)
uvx quant-swarm --profile warden   # Equivalent to swarm-warden-mcp (18 stdio tools; 12 on /mcp/warden HTTP)
uvx quant-swarm --profile data     # Equivalent to swarm-data-mcp (19 stdio tools; 16 on /mcp/data HTTP)
uvx quant-swarm --profile gym      # Equivalent to swarm-gym-mcp (11 tools on both stdio and /mcp/gym HTTP)
```

### Point-in-time data, for real
Every hosted dataset (`market.fundamentals_pit`, `market.insider_institutional_pit`, `market.supply_chain_pit`, `market.macro_nowcast`) is indexed strictly by its public filing or release timestamp (`available_at <= as_of`)—never restated fiscal-period archive dates—and returns derived ratios, z-scores, and percentiles rather than raw vendor series.

### PIT significance, explained (`pit.analysis`)
`pit.analysis` evaluates cross-sectional Spearman rank IC per `(dataset | metric | horizon)` family with size/sector neutralization, Newey-West (Bartlett) overlap-adjusted t-statistics, Benjamini-Hochberg `q <= 0.05` multiple-testing control, staleness caps, and a shuffled-date placebo gate (`SIGNIFICANT`, `NOT_SIGNIFICANT`, `NOT_JUDGED`, or `UNVERIFIED`).

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

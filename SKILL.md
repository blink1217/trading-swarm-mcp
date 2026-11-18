# quant-swarm — agent skill

Three MCP servers from the pinned `quant-swarm` release (stdio + hosted remote):

| Server | Use for |
|---|---|
| `swarm-data-mcp` | Point-in-time snapshots (`market.pulse`, `market.sentiment`, `market.climate`, `market.regime`), explained PIT significance (`pit.analysis`), screens/ranks (`market.screen`, `market.rank`), feature building (`features.build`), cache tools |
| `swarm-warden-mcp` | Pre-trade risk gates (`warden.validate_order`, `warden.cost_check`, `warden.explain_sizing`) and backtest audits (`warden.validate_genome`, `warden.audit_features`, `warden.promotion_verdict`) |
| `swarm-gym-mcp` | Regime labels + fragility probes (`gym.label_regimes`, `gym.probe_fragility`, `gym.paired_preview`, `gym.estimate_cloud_run`) and the Shadow Tournament (`tournament.leaderboard`, `tournament.submit`, `tournament.verdict`) |

## Workflow: validate a signal

1. `pit.analysis` with `symbols=["<TICKER>"]` (as_of only on Pro).
2. Report every `(dataset|metric|horizon)` verdict **verbatim** — SIGNIFICANT,
   NOT_SIGNIFICANT, NOT_JUDGED, UNVERIFIED — plus the caveats the tool returns.
3. Honesty rules: never upgrade a raw p-value into "significant"; a
   NOT_SIGNIFICANT row is never "promising"; verdicts are cross-sectional across
   the universe, not a forecast for one name; not investment advice.

## Workflow: audit a backtest

1. `warden.validate_genome` on the genome, then `warden.audit_features` on its
   feature list. Quote findings exactly; a refusal is never softened.
2. Fragility: `gym.label_regimes` (call out UNDERPOWERED labels), then
   `gym.probe_fragility`.
3. Promotion: only `tournament.submit` (hosted, credit-metered) +
   `tournament.verdict` earns evidence — a local backtest never does.

## Workflow: pre-trade check

1. `warden.validate_order` → `warden.cost_check` → `warden.explain_sizing`.
2. Any FAIL ⇒ the summary is REFUSED. These tools are state-only: they never
   place, cancel or route orders.

## Setup

- Registry: `io.github.blink1217/quant-swarm` (unified `quant-swarm` PyPI package + hosted Streamable HTTP remotes).
- Remote: `claude mcp add --transport http quant-swarm https://mcp.1.21initiative.com/mcp --header "Authorization: Bearer <token>"` (specialist subpaths: `/mcp/data` [16 tools], `/mcp/warden` [12 tools], `/mcp/gym` [11 tools]). Supports OAuth 2.1 PKCE or `Authorization: Bearer` header auth.
- stdio: `uvx quant-swarm` (or `uvx --from quant-swarm swarm-data-mcp` / `swarm-warden-mcp` / `swarm-gym-mcp`), with `SWARM_MCP_ACCESS_TOKEN` set for hosted PIT/relay tools.
- Free token at <https://1.21initiative.com/mcp/>. Pro details: `swarm://docs/plans` or the README.

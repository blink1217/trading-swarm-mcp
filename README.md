# trading-swarm-mcp

**The checkers that gate live capital, running locally against your own data.**

Three local [MCP](https://modelcontextprotocol.io) servers extracted from the trading
swarm — the system where every genome promotion is gated by pinned invariant checks,
point-in-time provenance guards, and a pessimistic fill model. Those same checkers are
the two failure modes that end emerging funds: **lookahead leakage in research** and
**an oversized position in production**. Install them in Cursor / Claude Desktop /
Claude Code and audit your own work with the code path that guards real money.

- Local-first, BYO-key: your Alpaca/Finnhub keys are read from env only.
- Your symbols, genomes, and alpha never leave your machine.
- Promotion verdicts are **never** issued locally — statistically undecidable outputs
  say so, name the exact missing inputs, and hand off to the hosted tournament /
  Strategy Validation Audit.
- No tool can place, cancel, or route an order. There is no order-placement code path.

---

## The servers

| Server | Hook | Tools |
| --- | --- | --- |
| `swarm-data-mcp` | History becomes **immutable and free after first fetch**, and every field carries `as_of` provenance | `get_bars`, `enrich_symbol`, `build_features`, `cache_warm`, `cache_stats`, `offline_mode` |
| `swarm-warden-mcp` | Pre-trade invariant + leakage gate — the live-capital floors and the point-in-time guards | `validate_order`, `audit_features`, `cost_check`, `validate_genome`, `explain_sizing`, `request_promotion_verdict` |
| `swarm-gym-mcp` | Regime fragility: *which regime kills it, and is your sample even large enough to have an opinion?* | `label_regimes`, `probe_fragility`, `paired_preview`, `estimate_cloud_run` |

### Point-in-time data, for real

LLM-driven research re-runs cells constantly; each re-run burns Finnhub (60 req/min) and
Alpaca (200 req/min) budgets re-pulling identical history — and then silently builds
features from data that did not exist at decision time. `swarm-data-mcp` fixes both:

- SQLite (WAL) cache at `%LOCALAPPDATA%\1.21-initiative\swarm-mcp\cache.db`
  (`$XDG_CACHE_HOME/1.21-initiative/swarm-mcp` on POSIX).
- **Finalized sessions are never re-fetched** — zero-cost replays forever. The
  in-progress session refreshes at most every 60 s; enrichment every 300 s.
- Earnings/news are append-only on `fetched_at`: a later fetch can never rewrite an
  earlier `as_of`.
- Per-provider token buckets + `429` exponential backoff (`1s·2ⁿ`).
- `build_features` runs the no-lookahead guard on every row (each feature must equal a
  fresh causal recomputation at `as_of`) and the provenance guards on every field.
  Tier-B/C fields without recorded point-in-time evidence come back `UNSCORABLE` —
  never neutral-filled with `0.0`.

Every response carries `coverage` (cache vs API, oldest/newest session per symbol) and
`limits` (your local depth in weeks vs the tape-depth gates: 8 weeks for the tier-A
fast path, 26 weeks for tape eligibility). Below the gates you get an `escalation`
block naming the hosted `bars_1day` panel that satisfies them. Tape-tier replay is
**roadmap, not available** — we will not imply otherwise.

### The warden

`validate_order` is the same function that rejects live orders: per-name 25% of equity,
gross cap 60%. Per-fund overrides are allowed, but the response always reports your
deviation from the house floors. `audit_features` flags banned actuals sources
(`open-meteo.archive` and friends return *actuals*, not what the forecast said on a past
Friday), features predating tape start, and unknown features fail closed to tier C.
`cost_check` converts a claimed gross edge into net-of-cost reality under the pessimistic
fill model (spread + slippage + adverse selection, both legs, always against you).
`explain_sizing` is a step-by-step mirror of the C# live risk engine, and a parity test
pins the floors against terraform and guardrails so the three copies cannot drift.

### The gym

`probe_fragility` replays your genome over the deterministic tier-A simulator and reports
per-regime net bps, the worst regime, turnover, and hard-constraint violations — never a
promotion. Seeds are capped at 8 and `per_regime` at 2: **statistical honesty, not
artificial scarcity**. Tier-B/C mutations against a champion raise the gym's
`TierScoringRefusal` (UNSCORABLE) instead of silently neutral-filling features the price
panel cannot provide. `paired_preview` compares champion vs challenger on identical
market paths with the promotion gate bypassed and every statistic labelled
**UNDERPOWERED**, naming the exact seed count needed to clear `MIN_EPISODES=20`.

---

## Install

Requires Python ≥ 3.11. Keys are BYO: `ALPACA_API_KEY`, `ALPACA_SECRET`, `FINNHUB_API_KEY`.

### Cursor

`Settings → MCP → Add server` (or `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "swarm-data": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/blink1217/trading-swarm-mcp", "swarm-data-mcp"],
      "env": { "ALPACA_API_KEY": "...", "ALPACA_SECRET": "...", "FINNHUB_API_KEY": "..." }
    },
    "swarm-warden": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/blink1217/trading-swarm-mcp", "swarm-warden-mcp"]
    },
    "swarm-gym": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/blink1217/trading-swarm-mcp", "swarm-gym-mcp"]
    }
  }
}
```

Once published to PyPI the args simplify to `["--from", "trading-swarm-mcp", "swarm-data-mcp"]`.

### Claude Desktop

`claude_desktop_config.json` — identical `mcpServers` block as above.

### Claude Code

```bash
claude mcp add swarm-data   --env ALPACA_API_KEY=... --env ALPACA_SECRET=... --env FINNHUB_API_KEY=... -- uvx --from git+https://github.com/blink1217/trading-swarm-mcp swarm-data-mcp
claude mcp add swarm-warden -- uvx --from git+https://github.com/blink1217/trading-swarm-mcp swarm-warden-mcp
claude mcp add swarm-gym    -- uvx --from git+https://github.com/blink1217/trading-swarm-mcp swarm-gym-mcp
```

The warden and gym need no API keys at all — they are pure checkers.

---

## The IP boundary (what ships, what doesn't)

This repository ships **checkers only**: order checks, provenance guards, the
pessimistic fill model, the genome schema, provenance tiers, hard-constraint checking,
gate thresholds, the tier-A gym simulator, and the regime labeller — vendored at pinned
commit SHAs (`.github/pins.json`, verified by CI).

The **selection machinery stays server-side** and is not in this repo: objective scoring,
deflated-Sharpe estimation, and the promotion-gate decision. Any request for a promotion
verdict returns `INDETERMINATE_LOCAL` with the exact missing statistical inputs
(`MIN_EPISODES=20`, PBO ≤ 0.30 via combinatorially-symmetric splits, DSR margin with the
monotonic trials ledger, worst-regime margin across all 5 regimes × seeds), plus:

- an `audit_request` block — genome hash + violation summary, **no proprietary payload**;
- a `cloud_job` spec accepted verbatim by the hosted `POST /tournament/run`.

In v1 the handoff is manual via the **Strategy Validation Audit** booking flow at
[https://1.21initiative.com/](https://1.21initiative.com/) — no public ingress, no new attack
surface. Access requests go through the site, which is how contact details are captured and
how hosted API keys are issued and metered. The human conversation is the product.

## Telemetry

Off by default, opt-in only (`SWARM_MCP_TELEMETRY_OPT_IN=opt-in`), counters only
(tool name, success flag, coarse duration). Never symbols, genomes, prices, or
credentials. Institutional buyers read the source; silent phone-home destroys the wedge.

## Development

```powershell
pip install -e ".[test]"
py -3.11 -m pytest                          # 64 tests
scripts\vendor.ps1                          # re-vendor the pinned checker subset
py -3.11 scripts\check_pin.py               # vendored tree == pins.json
```

Vendoring uses the sibling checkouts of the private repos at the pinned SHAs
(`-FromWorktree` bootstraps before the pin commit exists). CI mirrors the swarm's own
`guardrails-invariants` gate: it checks out the pinned SHAs and verifies the committed
vendored tree byte-for-byte (and the stripped subsets transform-for-transform), then runs
the guardrails' own invariant suite at the pinned SHA.

## License

Wrapper code: MIT (see `LICENSE`). The vendored guardrails checker subset under
`swarm_mcp/vendored/guardrails/` is distributed under the source-available terms in
`swarm_mcp/vendored/guardrails/LICENSE.md`. The excluded selection machinery is not
licensed.

---

*The gym is the tier-A price subset. The tape_replay service does not exist publicly.
Tier-B/C scoring is hosted-only. Anything else would be fabricated evidence — and this
repo refuses to fabricate.*

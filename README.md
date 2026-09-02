# quant-swarm

Risk-first pre-trade checks that gate live capital — point-in-time data, invariant and leakage audits, regime probes.

<!-- mcp-name: io.github.blink1217/quant-swarm -->

## Built by a Principal Quantitative Technologist

Authored by the Principal Quantitative Technologist at [The 1.21 Initiative](https://1.21initiative.com) — the same code path that gates live promotion and execution decisions in the trading swarm. Every tool exists because production trading systems fail in two repeatable ways: **lookahead leakage in research** and **an oversized position in production**. `quant-swarm` ships the checkers that prevent both, so you can audit your own work with the exact logic that guards deployed capital.

## Why risk-first

Risk gates are not post-hoc reports — they are the pre-condition for every action. The package is organized around that constraint:

- **Fail-closed by default.** Unknown features map to tier C, missing provenance returns `UNSCORABLE` (never neutral-filled), and unverifiable tokens are refused on the hosted path. See `warden.audit_features` and `features.build` provenance guards.
- **Pinned, non-drifting invariants.** Order sizing floors (per-name 25%, gross 60%), fill model (spread + slippage + adverse selection, both legs, always against you), and hard-constraint checks are vendored at pinned commit SHAs (`.github/pins.json`, verified byte-for-byte by CI). A parity test pins the three copies of the floors (Python/MCP, C# live engine, Terraform) so they cannot drift.
- **Point-in-time provenance.** SQLite WAL cache at `%LOCALAPPDATA%\1.21-initiative\swarm-mcp\cache.db` (`$XDG_CACHE_HOME/1.21-initiative/swarm-mcp` on POSIX): finalized sessions are never re-fetched, in-progress sessions refresh at most every 60 s, earnings/news are append-only on `fetched_at`, and `features.build` runs the no-lookahead guard on every row (each feature must equal a fresh causal recomputation at `as_of`).
- **Pessimistic execution.** `warden.cost_check` and the simulator model the fill that hurts you; there is no optimistic fill path to inflate backtests.
- **No order-placement code path.** No tool can place, cancel, or route an order — this repository cannot trade, only refuse unsafe trades.
- **Statistically honest refusals.** Promotion verdicts are **never** issued locally. Statistically undecidable outputs return `INDETERMINATE_LOCAL`/`UNDERPOWERED`/`UNSCORABLE`, name the exact missing inputs (`MIN_EPISODES=20`, PBO, DSR, worst-regime margins), and hand off to the hosted tournament / Strategy Validation Audit.

- Local-first, BYO-key: your Alpaca/Finnhub keys are read from env only when operating in BYOK mode; otherwise data flows through the hosted relay.
- Your symbols, genomes, and alpha never leave your machine except as an `audit_request` (genome hash + violation summary, no proprietary payload) when you explicitly escalate.

**One-click install on [Smithery](https://smithery.ai/servers/blink-kt/quant-swarm):**
the listing is [`blink-kt/quant-swarm`](https://smithery.ai/servers/blink-kt/quant-swarm) (package and Smithery slug are both `quant-swarm`), and it carries all three servers. Configuration is optional — without a token the servers still start and list every tool; the first call points you at the free signup. Add a token any time to unlock the relay-backed tools.

---

## The servers

| Server | Hook | Tools |
| --- | --- | --- |
| `swarm-data-mcp` | Derived-only market signals plus point-in-time feature building | `market.pulse`, `market.sentiment`, `features.build`, `cache.warm`, `cache.stats`, `cache.offline` |
| `swarm-warden-mcp` | Pre-trade invariant + leakage gate — the live-capital floors and the point-in-time guards | `warden.validate_order`, `warden.audit_features`, `warden.cost_check`, `warden.validate_genome`, `warden.explain_sizing`, `warden.promotion_verdict` |
| `swarm-gym-mcp` | Regime fragility: *which regime kills it, and is your sample even large enough to have an opinion?* | `gym.label_regimes`, `gym.probe_fragility`, `gym.paired_preview`, `gym.estimate_cloud_run` |

Raw bar/enrichment access (`get_bars` / `enrich_symbol`) is internal only and no longer
registered as tools — those paths echo raw provider values, and the data policy is
derived-only: ratios, percentile ranks, labels, buckets and counts. `market.pulse` and
`market.sentiment` are the general-purpose replacements; both accept an optional `bars`
argument so you can supply your own OHLCV rows for symbols we don'"'"'t carry.

## Plans

Access is tokened through [https://1.21initiative.com/](https://1.21initiative.com/).

- **Free** (instant token at signup): all warden checkers (`warden.validate_order`, `warden.cost_check`,
  `warden.explain_sizing`, `warden.validate_genome`, `warden.audit_features`), the derived snapshots
  `market.pulse` / `market.sentiment` / `market.regime` (shared-relay symbols capped at
  10 per call, ~250 relay data calls/month), plus `cache.stats` and `cache.offline`.
- **Pro** (paid by one-time credit packs, self-serve at the site): everything else — `features.build`,
  `cache.warm` bulk backfill, all gym tools, `warden.promotion_verdict`, and the Pro
  derived tools (`market.microstructure`, `volume.forecast`, `market.screen`,
  `market.rank`), paid by one-time credit packs (10k or 100k relay calls, 90-day validity — no subscription, no unlimited plan) with 50 symbols/call.
- **Institutional** (custom-quoted): Strategy Validation Audit engagements, custom
  universes, hosted evolution runs, SLA.

Plan limits are enforced server-side (the relay, `/api/mcp/verify`, and the hosted
streamable-HTTP endpoint return structured `402` refusals with an `upgrade_url`). This
open-source client'"'"'s plan check is **advisory**: a free token calling a Pro tool gets an
`UPGRADE_REQUIRED` envelope — the tool still lists, and the attempt points at the upgrade
page. No DRM; the free plan is deliberately the zero-cost surface.

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
- Per-provider token buckets + `429` exponential backoff (`1s\u00b72\u207f`).
- `features.build` runs the no-lookahead guard on every row (each feature must equal a
  fresh causal recomputation at `as_of`) and the provenance guards on every field.
  Tier-B/C fields without recorded point-in-time evidence come back `UNSCORABLE` —
  never neutral-filled with `0.0`.

Every response carries `coverage` (cache vs API, oldest/newest session per symbol) and
`limits` (your local depth in weeks vs the tape-depth gates: 8 weeks for the tier-A
fast path, 26 weeks for tape eligibility). Below the gates you get an `escalation`
block naming the hosted `bars_1day` panel that satisfies them. Tape-tier replay is
**roadmap, not available** — we will not imply otherwise.

### The warden

`warden.validate_order` is the same function that rejects live orders: per-name 25% of equity,
gross cap 60%. Per-fund overrides are allowed, but the response always reports your
deviation from the house floors. `warden.audit_features` flags banned actuals sources
(`open-meteo.archive` and friends return *actuals*, not what the forecast said on a past
Friday), features predating tape start, and unknown features fail closed to tier C.
`warden.cost_check` converts a claimed gross edge into net-of-cost reality under the pessimistic
fill model (spread + slippage + adverse selection, both legs, always against you).
`warden.explain_sizing` is a step-by-step mirror of the C# live risk engine, and a parity test
pins the floors against terraform and guardrails so the three copies cannot drift.

### The gym

`gym.probe_fragility` replays your genome over the deterministic tier-A simulator and reports
per-regime net bps, the worst regime, turnover, and hard-constraint violations — never a
promotion. Seeds are capped at 8 and `per_regime` at 2: **statistical honesty, not
artificial scarcity**. Tier-B/C mutations against a champion raise the gym'"'"'s
`TierScoringRefusal` (UNSCORABLE) instead of silently neutral-filling features the price
panel cannot provide. `gym.paired_preview` compares champion vs challenger on identical
market paths with the promotion gate bypassed and every statistic labelled
**UNDERPOWERED**, naming the exact seed count needed to clear `MIN_EPISODES=20`.

---

## Install

Requires Python \u2265 3.11. One kind of credential:

- **Access token (all three servers, required).** Every tool call is gated on a token
  issued by [https://1.21initiative.com/](https://1.21initiative.com/) — request access
  there (that'"'"'s also the Strategy Validation Audit booking flow), then set
  `SWARM_MCP_ACCESS_TOKEN` in the server'"'"'s `env`. Clients still list the tools without a
  token; every call returns an `ACCESS_REQUIRED` envelope pointing back to the site.
  The token is verified against the site, and that verification is the usage meter —
  only the token itself is ever sent, never symbols, genomes, prices, or provider keys.

The same token also feeds the **hosted data relay**: `swarm-data-mcp` serves bars and
enrichment through `https://1.21initiative.com/api/mcp/...`, so **no Alpaca or Finnhub
credentials are required** — the site holds the provider keys behind the relay and caches
historical bars in GCS. The relay is fail-closed: a rejected or unverifiable token means a
refused data fetch, never partial rows. The point-in-time cache semantics on the client
side are unchanged — finalized sessions are immutable in local SQLite regardless of which
data path filled them.

### Smithery

Install [`blink-kt/quant-swarm`](https://smithery.ai/servers/blink-kt/quant-swarm) from
Smithery — one listing, one token, all three servers. Smithery forwards `apiToken` as the
`SWARM_MCP_ACCESS_TOKEN` env var (stdio) or as `?apiToken=` on the hosted endpoints; the
token is optional at connect time.

### Cursor

`Settings \u2192 MCP \u2192 Add server` (or `.cursor/mcp.json` — a checked-in example lives at
[.cursor/mcp.json](.cursor/mcp.json)):

```json
{
  "mcpServers": {
    "swarm-data": {
      "command": "uvx",
      "args": ["--from", "quant-swarm", "swarm-data-mcp"],
      "env": {
        "SWARM_MCP_ACCESS_TOKEN": "<token from https://1.21initiative.com/>"
      }
    },
    "swarm-warden": {
      "command": "uvx",
      "args": ["--from", "quant-swarm", "swarm-warden-mcp"],
      "env": { "SWARM_MCP_ACCESS_TOKEN": "<token from https://1.21initiative.com/>" }
    },
    "swarm-gym": {
      "command": "uvx",
      "args": ["--from", "quant-swarm", "swarm-gym-mcp"],
      "env": { "SWARM_MCP_ACCESS_TOKEN": "<token from https://1.21initiative.com/>" }
    }
  }
}
```

Prefer a deeplink? `scripts/make_deeplinks.py` prints `cursor://\u2026/mcp/install` links for
all three servers (base64 of the stdio config, so they cannot drift from the JSON above);
the generated list is checked in at [.cursor/DEEPLINKS.md](.cursor/DEEPLINKS.md).

### Claude Desktop

`claude_desktop_config.json` \u2014 identical `mcpServers` block as above.

### Claude Code

```bash
claude mcp add swarm-data   --env SWARM_MCP_ACCESS_TOKEN=<token> -- uvx --from quant-swarm swarm-data-mcp
claude mcp add swarm-warden --env SWARM_MCP_ACCESS_TOKEN=<token> -- uvx --from quant-swarm swarm-warden-mcp
claude mcp add swarm-gym    --env SWARM_MCP_ACCESS_TOKEN=<token> -- uvx --from quant-swarm swarm-gym-mcp
```

### Windsurf

`~/.codeium/windsurf/mcp_config.json` \u2014 the same stdio JSON shape as the Cursor block
above. For the hosted endpoints use the `serverUrl` form instead:

```json
{
  "mcpServers": {
    "swarm-data-remote": {
      "serverUrl": "https://swarm-mcp-503318750546.europe-west1.run.app/mcp/data"
    }
  }
}
```

(The hosted endpoints accept the token as `Authorization: Bearer <token>` or as
`?apiToken=<token>`.)

The warden and gym are pure checkers and the data server fetches through the hosted
relay, so **no server needs provider keys** \u2014 only the access token.

**Local development:** operators of this repo can bootstrap offline by setting
`SWARM_MCP_LOCAL_TOKEN` to the same value as `SWARM_MCP_ACCESS_TOKEN` (documented
bypass; token verification against the site is skipped and the token gets the full
Pro entitlement \u2014 it is a local override on your own machine).

**Swarm operators (internal):** to fetch directly from the providers instead of the
relay, set `SWARM_MCP_BYOK=1` plus `ALPACA_API_KEY`/`ALPACA_SECRET`/`FINNHUB_API_KEY`
on `swarm-data-mcp`. Public users should leave `SWARM_MCP_BYOK` unset.

**Hosting the remote endpoint yourself:** `swarm-mcp-http` validates the `Host` header
(DNS-rebinding protection). The defaults allow `1.21initiative.com`, the project'"'"'s Cloud
Run hostname, the Smithery gateway, and localhost; `SWARM_MCP_ALLOWED_HOSTS`
(comma-separated) **extends** the defaults \u2014 it never replaces them, so a custom domain
cannot accidentally lock out the Cloud Run URL. `SWARM_MCP_REMOTE_URL` overrides the
advertised endpoint base, and `SWARM_MCP_TOKEN_VERIFY_URL` overrides the verify endpoint.
See the Dockerfile for the container form.

---

## The IP boundary (what ships, what doesn'"'"'t)

This repository ships **checkers only**: order checks, provenance guards, the
pessimistic fill model, the genome schema, provenance tiers, hard-constraint checking,
gate thresholds, the tier-A gym simulator, and the regime labeller \u2014 vendored at pinned
commit SHAs (`.github/pins.json`, verified by CI).

The **selection machinery stays server-side** and is not in this repo: objective scoring,
deflated-Sharpe estimation, and the promotion-gate decision. Any request for a promotion
verdict returns `INDETERMINATE_LOCAL` with the exact missing statistical inputs
(`MIN_EPISODES=20`, PBO \u2264 0.30 via combinatorially-symmetric splits, DSR margin with the
monotonic trials ledger, worst-regime margin across all 5 regimes \u00d7 seeds), plus:

- an `audit_request` block \u2014 genome hash + violation summary, **no proprietary payload**;
- a `cloud_job` spec accepted verbatim by the hosted `POST /tournament/run`.

In v1 the handoff is manual via the **Strategy Validation Audit** booking flow at
[https://1.21initiative.com/](https://1.21initiative.com/) \u2014 no public ingress, no new attack
surface. Access requests go through the site, which is how contact details are captured and
how hosted API keys are issued and metered. The human conversation is the product.

## Telemetry

Off by default, opt-in only (`SWARM_MCP_TELEMETRY_OPT_IN=opt-in`), counters only
(tool name, success flag, coarse duration). Never symbols, genomes, prices, or
credentials. Institutional buyers read the source; silent phone-home destroys the wedge.

The one exception is by contract: **access-token verification**. When a token is set and
no local bootstrap token matches, the token is POSTed to the verify endpoint (default
`https://1.21initiative.com/api/mcp/verify`, override `SWARM_MCP_TOKEN_VERIFY_URL`).
That call is how usage is metered. Only the token is sent; the gate fails closed if the
endpoint is unreachable or rejects the token.

## Development

```powershell
pip install -e ".[test]"
py -3.11 -m pytest                          # 119 tests
scripts\vendor.ps1                          # re-vendor the pinned checker subset
py -3.11 scripts\check_pin.py               # vendored tree == pins.json
```

Vendoring uses the sibling checkouts of the private repos at the pinned SHAs
(`-FromWorktree` bootstraps before the pin commit exists). CI mirrors the swarm'"'"'s own
`guardrails-invariants` gate: it checks out the pinned SHAs and verifies the committed
vendored tree byte-for-byte (and the stripped subsets transform-for-transform), then runs
the guardrails'"'"' own invariant suite at the pinned SHA.

## License

Wrapper code: MIT (see `LICENSE`). The vendored guardrails checker subset under
`swarm_mcp/vendored/guardrails/` is distributed under the source-available terms in
`swarm_mcp/vendored/guardrails/LICENSE.md`. The excluded selection machinery is not
licensed.

---

*The gym is the tier-A price subset. The tape_replay service does not exist publicly.
Tier-B/C scoring is hosted-only. Anything else would be fabricated evidence \u2014 and this
repo refuses to fabricate.*

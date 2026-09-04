# quant-swarm

Risk-first pre-trade checks that gate live capital — point-in-time data, invariant and leakage audits, regime probes — plus the **Shadow Tournament**: score your strategy genome against a live, self-evolving trading swarm's champion on identical market paths.

<!-- mcp-name: io.github.blink1217/quant-swarm -->

## Built by a Principal Quantitative Technologist

Authored by the Principal Quantitative Technologist at [The 1.21 Initiative](https://1.21initiative.com) — the same code path that gates live promotion and execution decisions in the trading swarm. Every tool exists because production trading systems fail in two repeatable ways: **lookahead leakage in research** and **an oversized position in production**. `quant-swarm` ships the checkers that prevent both, so you can audit your own work with the exact logic that guards deployed capital.

## Your backtest is static. The swarm is not.

Behind this package is a swarm that does not hand-tune strategies. A **champion** genome runs live; every cycle the swarm breeds **challengers** (tier-capped mutations), replays champion and challenger on the *identical* episode-seed matrix across five labelled market regimes (high-vol, drawdown, chop, melt-up, gap-heavy), and records the paired outcome in an **ELO ledger**. A surrogate prior pre-scores candidates so expensive replays are spent where they matter. Nothing is promoted on a single good backtest: promotion requires `MIN_EPISODES=20` paired episodes, a Wilcoxon-significant delta, a positive **worst-regime** margin (maximin, not mean), a **deflated-Sharpe** margin against the monotonic count of every trial ever attempted, and a **PBO** cap. This is the champion/challenger, self-play-style loop popularised by AlphaZero-style systems, applied honestly to markets — with the two things that do *not* transfer stated plainly: the market does not react to us, so there is no true self-play, and there is no learned dynamics model or tree search; selection is done by paired replay, ELO and statistical gates.

Replay is only half of it. Every Sunday the swarm also runs a **forward league**: each league genome — the champion, the top-ELO challengers, contributed genomes, and a *planner* entrant whose weekend moves are chosen by a learned model — seals its positions for the coming week from Friday's causal features, and the SHA-256 commitment is stored before the week happens. The next cycle marks every sealed position against the realised closes (net of each genome's own cost model), ranks the field on the identical real week, and updates ELO on that. Sealed forecasts cannot be overfit to the week that follows; that is the swarm's ground truth, and it feeds a replay buffer where every position is decomposed into its component **moves** (screen, entry, size, weekend) so credit is assigned per move and regime, not to the strategy as a blob. The forward league started on 2026-09-02; the first realised settlements land mid-September, and the swarm runs untouched for eight weeks to collect them.

What that means for you: a local backtest tells you how a strategy did on one path. The Shadow Tournament tells you whether it beats an *evolving* champion on the same paths, in the regime that hurts it most, at a sample size that can actually have an opinion — and your result enters the swarm's ELO ledger. Opt in as a **contributor** and your genome becomes an external challenger in the swarm's next breeding cycle (at half price), scored in replay and, if it earns a league seat, on real weeks. That is the flywheel: every honest submission makes the champion harder to beat, and every user gets a harder benchmark.

## Why risk-first

Risk gates are not post-hoc reports — they are the pre-condition for every action. The package is organized around that constraint:

- **Fail-closed by default.** Unknown features map to tier C, missing provenance returns `UNSCORABLE` (never neutral-filled), and unverifiable tokens are refused on the hosted path. See `warden.audit_features` and `features.build` provenance guards.
- **Pinned, non-drifting invariants.** Order sizing floors (per-name 25%, gross 60%), fill model (spread + slippage + adverse selection, both legs, always against you), and hard-constraint checks are vendored at pinned commit SHAs (`.github/pins.json`, verified byte-for-byte by CI). A parity test pins the three copies of the floors (Python/MCP, C# live engine, Terraform) so they cannot drift.
- **Point-in-time provenance.** SQLite WAL cache at `%LOCALAPPDATA%\1.21-initiative\swarm-mcp\cache.db` (`$XDG_CACHE_HOME/1.21-initiative/swarm-mcp` on POSIX): finalized sessions are never re-fetched, in-progress sessions refresh at most every 60 s, earnings/news are append-only on `fetched_at`, and `features.build` runs the no-lookahead guard on every row (each feature must equal a fresh causal recomputation at `as_of`).
- **Pessimistic execution.** `warden.cost_check` and the simulator model the fill that hurts you; there is no optimistic fill path to inflate backtests.
- **No order-placement code path.** No tool can place, cancel, or route an order — this repository cannot trade, only refuse unsafe trades.
- **Statistically honest refusals.** Promotion verdicts are **never** issued locally. Statistically undecidable outputs return `INDETERMINATE_LOCAL`/`UNDERPOWERED`/`UNSCORABLE`, name the exact missing inputs (`MIN_EPISODES=20`, PBO, DSR, worst-regime margins), and hand off to the hosted Shadow Tournament (`tournament.submit`).

- Local-first, BYO-key: your Alpaca/Finnhub keys are read from env only when operating in BYOK mode; otherwise data flows through the hosted relay.
- Your symbols, bars, features and orders never leave your machine. What can leave is only what you explicitly pass to `tournament.submit`: the genome parameter vector (public schema fields), and — for the **strategy-contributor** tier — an author-written disclosure and/or your strategy code. Submitted code is **never executed**: it is treated as inert text, read statically by the hosted LLM reviewer, reduced to a structured explanation plus a code hash, and the raw code is discarded after review. By default even the vector is deleted from the hosted side once scored.

**One-click install on [Smithery](https://smithery.ai/servers/blink-kt/quant-swarm):**
the listing is [`blink-kt/quant-swarm`](https://smithery.ai/servers/blink-kt/quant-swarm) (package and Smithery slug are both `quant-swarm`); the Smithery stdio launcher starts `swarm-data-mcp`, and the hosted streamable-HTTP endpoints below serve all three servers. Configuration is optional — without a token the servers still start and list every tool; the first call points you at the free signup. Add a token any time to unlock the relay-backed and hosted tools.

---

## The servers

| Server | Hook | Tools |
| --- | --- | --- |
| `swarm-data-mcp` | Derived-only market signals plus point-in-time feature building | `market.pulse`, `market.sentiment`, `market.climate`, `market.regime`, `market.microstructure`, `volume.forecast`, `market.screen`, `market.rank`, `features.build`, `cache.warm`, `cache.stats`, `cache.offline` |
| `swarm-warden-mcp` | Pre-trade invariant + leakage gate — the live-capital floors and the point-in-time guards | `warden.validate_order`, `warden.audit_features`, `warden.cost_check`, `warden.validate_genome`, `warden.explain_sizing`, `warden.promotion_verdict` |
| `swarm-gym-mcp` | Regime fragility locally, then the **Shadow Tournament** against the swarm's live champion | `gym.label_regimes`, `gym.probe_fragility`, `gym.paired_preview`, `gym.estimate_cloud_run`, `tournament.submit`, `tournament.verdict`, `tournament.leaderboard` |

Raw bar/enrichment access (`get_bars` / `enrich_symbol`) is internal only and no longer
registered as tools — those paths echo raw provider values, and the data policy is
derived-only: ratios, percentile ranks, labels, buckets and counts. `market.pulse` and
`market.sentiment` are the general-purpose replacements; both accept an optional `bars`
argument so you can supply your own OHLCV rows for symbols we don'"'"'t carry.

### Operating-area weather (`market.climate`)

`market.climate` is area-specific weather research for weather-exposed underlyings. For
each symbol it fetches the keyless Open-Meteo 14-day forecast **and the same 14 calendar
days one year earlier** for every **operating area** where the product is made and/or sold
(a shipped 10-K-style exposure-weighted registry of energy, agriculture, airline/travel,
retail, utility and homebuilder footprints), then exposure-weights per-area temperature
anomaly, HDD/CDD, precipitation/snow, wind and freeze/heat-day counts into symbol
aggregates — per-area detail is preserved so a heat wave in one harvest region stays
visible. The weather is tied to the underlying's actual geographies, never the listing
exchange or HQ city: a London forecast is not substituted for a product made and sold in
New York. For underlyings outside the shipped registry, pass `areas` (made/sold
geographies) and `market.climate` researches exactly those. Free tool, no provider
credentials; returns derived sums/means/ratios/counts/buckets only, never raw per-day
series. Forecast values are current research context and are never valid for past decision
dates.

### The Shadow Tournament

`gym.paired_preview` is honest about being underpowered: at most 8 seeds × 2 per regime on
your cached window, every statistic labelled `UNDERPOWERED`, never a verdict. `tournament.submit`
is how a genome leaves that ceiling:

1. **Validate locally** — `warden.validate_genome` returns the `genome_hash`; nothing is sent.
2. **Submit** — `tournament.submit(genome, contribute=false|true)`. The site charges credits
   (200, or 100 with `contribute=true`) and dispatches the job to the hosted runner.
   `contribute=true` is the **genome-contributor** tier (vector licensed as an external
   challenger). Add `disclosure` and/or `strategy_code` to enter the **strategy-contributor**
   tier: author-written decision logic and/or code for a static LLM review — the code is
   never executed, and leaderboard attribution plus league-seat eligibility follow review.
3. **Hosted replay on identical paths** — the runner replays *your genome and the swarm's
   current champion* over the same 5 regimes × 4 per regime × 5 seeds = **100 paired episodes**
   on the hosted `bars_1day` panel (above `MIN_EPISODES=20`), computes the Wilcoxon paired
   p-value, bootstrap CI, per-regime deltas and the **worst-regime margin**, counts hard-constraint
   and lookahead violations, and classifies the outcome: `CHALLENGER_BEATS_CHAMPION`,
   `CHALLENGER_LOSES`, or `INCONCLUSIVE`.
4. **ELO** — the outcome updates your genome's rating against the champion (K=16, start 1500) and
   the champion's rating against the field. `tournament.leaderboard` (free) shows the anonymised
   board: 12-char hash prefixes only.
5. **Poll** — `tournament.verdict(job_id)`; no charge to poll. Runs take about two minutes.

The outcome is a *tournament result*, not a promotion. The promotion gate — deflated-Sharpe
margin, PBO cap, the monotonic trials ledger — runs inside the swarm's private registry on
genomes that qualify. That boundary is deliberate (see **The IP boundary** below). Replay
results are the swarm's fast signal; forward-league settlements on real weeks are its slow,
unfakeable one — a contributed genome that beats the champion in replay is what earns a league
seat, and only realised weeks can keep it there.

**What is sent:** the genome parameter vector, its hash, and the `contribute` flag — plus, on the
strategy-contributor tier only, a disclosure and/or `strategy_code` (code is never executed;
the hosted reviewer reads it statically with an LLM and keeps only the structured explanation
and a code hash). Never symbols, bars, features, orders, or credentials. **Retention:** with
`contribute=false` the vector is deleted from the job record once scored — hash and outcome
remain. With `contribute=true` (genome or strategy tier) you license the vector and outcome to
the swarm's evolution loop, where it is registered as an external challenger
(`origin=external_contribution`) and put through the same tier-capped tournament and promotion
gates as the swarm's own mutants. That is why contributors pay half: your genome is the swarm's
proposer diversity, and every accepted contribution makes the next champion harder to beat — for
everyone. The strategy tier's disclosure is what additionally earns leaderboard attribution and
a league seat after review — a contributed genome that beats the champion in replay earns the
seat, and only realised forward-league weeks can keep it there.

## Plans and the credit rate card

Access is tokened through [https://1.21initiative.com/mcp/](https://1.21initiative.com/mcp/).
**One credit is the unit for everything that costs us money** — a relay data call, one simulated
episode of hosted compute, or a tournament run — so a pack buys a bounded amount of *all* of it and
we never run hosted compute at a loss.

| What | Credits | Notes |
| --- | --- | --- |
| Relay data call (`/data/bars`, `/data/enrich`) | 1 | any symbol count within your plan cap counts as one call |
| Hosted single-shot Pro tool (`features.build`, `warden.promotion_verdict`, `gym.label_regimes`, `gym.estimate_cloud_run`) | 1 | charged only on the hosted endpoint |
| Hosted gym episode (`gym.probe_fragility`, `gym.paired_preview`) | 1 per episode | 5 regimes × per_regime × seeds (× 2 genomes for the paired preview); `gym.estimate_cloud_run` prices any geometry before you run it |
| Shadow Tournament (`tournament.submit`) | **200** — **100** with `contribute=true` | fixed price for the full 100-paired-episode geometry vs the champion |
| `tournament.verdict`, `tournament.leaderboard`, all warden checkers, `cache.*`, local stdio runs of any tool | 0 | local execution is never metered — your CPU, your electricity |

- **Free** (instant token at signup): all warden checkers, the derived snapshots
  `market.pulse` / `market.sentiment` / `market.climate` / `market.regime`
  (10 symbols/call, 250 relay calls/month,
  365-day backfill), `cache.stats`, `cache.offline`, and `tournament.leaderboard`. No hosted compute,
  no GCP spend on your behalf beyond the relay allowance.
- **Pro** — one-time credit packs, self-serve: **10,000 credits for £19** or **100,000 for £149**,
  valid 90 days, no subscription, no unlimited plan. Unlocks `features.build`, `cache.warm`, the Pro
  derived tools (`market.microstructure`, `volume.forecast`, `market.screen`, `market.rank`; 50
  symbols/call, full backfill), all gym tools, `warden.promotion_verdict`, and `tournament.submit` /
  `tournament.verdict`. A Starter pack is 50 tournaments — 100 as a contributor.
- **Institutional** (custom-quoted): Strategy Validation Audit engagements, custom universes,
  hosted league runs, SLA.

A Pro token whose pool is empty or past its 90-day window verifies with `status: exhausted` /
`expired` and the **free** feature set — Pro tools refuse until you buy again. Plan limits are
enforced server-side (the relay, `/api/mcp/meter`, `/api/mcp/verify`, and the hosted
streamable-HTTP endpoint return structured `402` refusals with an `upgrade_url`). This open-source
client'"'"'s plan check is **advisory**: a free token calling a Pro tool gets an `UPGRADE_REQUIRED`
envelope — the tool still lists, and the attempt points at the upgrade page. No DRM; the free plan
is deliberately the zero-cost surface.

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
**UNDERPOWERED**, naming the exact seed count needed to clear `MIN_EPISODES=20` — and every
undecidable result ends with a `cloud_job` block that is literally the `tournament.submit` call
that resolves it.

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
(comma-separated) **extends** the defaults — it never replaces them, so a custom domain
cannot accidentally lock out the Cloud Run URL. `SWARM_MCP_REMOTE_URL` overrides the
advertised endpoint base, and `SWARM_MCP_TOKEN_VERIFY_URL` overrides the verify endpoint.
See the Dockerfile for the container form.

The hosted endpoint meters compute (`swarm_mcp/metering.py`): before a Pro compute tool runs it
charges `POST {SWARM_MCP_RELAY_URL}/meter` with the caller'"'"'s token, and refuses (JSON-RPC `-32003`,
HTTP 402/429/503) when the meter refuses or is unreachable — hosted compute is never served
unmetered. The Shadow Tournament runner lives in the same service: the site dispatches jobs to
`POST /internal/tournament/run` guarded by the shared secret `SWARM_MCP_INTERNAL_KEY`, the runner
fills its panel through the relay with `SWARM_MCP_SERVICE_TOKEN` (an institutional token, so
users are never billed for the runner'"'"'s own data), scores against `SWARM_MCP_CHAMPION_GENOME`
(path to the registry'"'"'s current champion; the packaged `swarm_mcp/data/champion_genome.json` is the
fallback), and calls back `POST {relay}/tournament/complete`. `SWARM_MCP_TOURNAMENT_UNIVERSE` and
`SWARM_MCP_TOURNAMENT_LOOKBACK_DAYS` shape the hosted panel. The runner scores in a background task
after answering `202`, so the Cloud Run service must run with **CPU always allocated**
(`--no-cpu-throttling`) and a request timeout of at least 300 s; with request-based CPU the task
starves after the response and the job is refunded as failed.

---

## The IP boundary (what ships, what doesn'"'"'t)

This repository ships **checkers only**: order checks, provenance guards, the
pessimistic fill model, the genome schema, provenance tiers, hard-constraint checking,
gate thresholds, the tier-A gym simulator, and the regime labeller \u2014 vendored at pinned
commit SHAs (`.github/pins.json`, verified by CI).

The **selection machinery stays server-side** and is not in this repo: objective scoring,
deflated-Sharpe estimation, and the promotion-gate decision. Any request for a promotion
verdict returns `INDETERMINATE_LOCAL` with the exact missing statistical inputs
(`MIN_EPISODES=20`, PBO ≤ 0.30 via combinatorially-symmetric splits, DSR margin with the
monotonic trials ledger, worst-regime margin across all 5 regimes × seeds), plus:

- an `audit_request` block — genome hash + violation summary, **no proprietary payload**;
- a `cloud_job` spec that is the `tournament.submit` call which resolves it.

The Shadow Tournament runner in this repo computes **paired statistics and an outcome** on
identical paths; it does not contain, and a CI test (`tests/test_ip_boundary.py`) forbids, the
objective, the DSR estimator, or the promotion decision. Contributed genomes that beat the
champion are handed to the swarm'"'"'s private registry, where those gates run. The **Strategy
Validation Audit** at [https://1.21initiative.com/](https://1.21initiative.com/) remains the
route for multi-genome league runs, custom universes, and NDA'"'"'d live metrics.

## Telemetry

Off by default, opt-in only (`SWARM_MCP_TELEMETRY_OPT_IN=opt-in`), counters only
(tool name, success flag, coarse duration). Never symbols, genomes, prices, or
credentials. Institutional buyers read the source; silent phone-home destroys the wedge.

The exceptions are by contract and explicit:

- **Access-token verification.** When a token is set and no local bootstrap token matches,
  the token is POSTed to the verify endpoint (default `https://1.21initiative.com/api/mcp/verify`,
  override `SWARM_MCP_TOKEN_VERIFY_URL`), at most once per 5 minutes per process. Only the token
  is sent; the gate fails closed if the endpoint is unreachable or rejects the token.
- **Relay data calls** carry the token plus the symbols/days you asked for (that is the request).
  Relay calls are metered server-side per call.
- **`tournament.submit`** sends the genome vector, its hash and the `contribute` flag — the one
  tool that ships a strategy artefact, and it says so in its output every time.

## Development

```powershell
pip install -e ".[test]"
py -3.11 -m pytest                          # 160 tests
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
Tier-B/C scoring is hosted-only. The Shadow Tournament returns paired outcomes, never
promotions. Anything else would be fabricated evidence — and this repo refuses to fabricate.*

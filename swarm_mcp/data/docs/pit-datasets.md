### PIT significance, explained (`pit.analysis`)

`pit.analysis` answers the question every alternative-data buyer should ask first: **is this
signal real, and how do you know?** It reads the release-gated `pit_analysis/v2` store — the
same artifact whose rows carry the significance output for the funnel — and, per underlying,
explains every `(dataset|metric|horizon)` family with no guessing:

- **rolling_ir** (52-week Spearman cross-sectional rank IC on a size/sector-neutralized metric
  vs neutralized forward returns), **Newey-West two-sided p** (`t_hac`, lag `ceil(horizon/5)` —
  1m/6m horizons overlap, so the naive `IR·√n` t is anti-conservative), **n_obs**, the
  **research_only** coverage flag and any staleness caveat.
- An explicit verdict per horizon: **SIGNIFICANT** (an L1 survivor of the *same* run: rolling
  IR > 0 and Benjamini-Hochberg q ≤ 0.05), **NOT_SIGNIFICANT** (with the reason — negative IR,
  or a nominal p that does not survive multiple-testing correction), **NOT_JUDGED** (below the
  26-observation floor, stale release, or diagnostic-only raw level) or **UNVERIFIED** (no gate
  artifact, gate from another run, or a failed placebo — a raw p-value is never upgraded on its
  own).
- Point-in-time: release points after `as_of` are ignored, and raw metric values are never
  returned — derived statistics only.

Free plans get a lite answer (one symbol per call, headline counts and the top families); Pro
unlocks 10 symbols per call, every family, full per-horizon detail and historical `as_of`.
The v2 methodology (neutralization + overlap-corrected inference + scale-free accounting
variants + staleness caps + a shuffled-date placebo) is recorded in every run manifest, and
v1 stores are refused rather than silently reused. Not investment advice.

### Point-in-time data, for real

LLM-driven research re-runs cells constantly; each re-run burns the data relay (60 req/min) and
the bars source (200 req/min) budgets re-pulling identical history — and then silently builds
features from data that did not exist at decision time. `swarm-data-mcp` fixes both:

- SQLite (WAL) cache at `%LOCALAPPDATA%\1.21-initiative\swarm-mcp\cache.db`
  (`$XDG_CACHE_HOME/1.21-initiative/swarm-mcp` on POSIX).
- **Finalized sessions are never re-fetched** — zero-cost replays forever. The
  in-progress session refreshes at most every 60 s; enrichment every 300 s.
- Earnings/news are append-only on `fetched_at`: a later fetch can never rewrite an
  earlier `as_of`.
- Per-provider token buckets + `429` exponential backoff (`1s·2ⁿ`).
- `features.build` runs the no-lookahead guard on every row (each feature must equal a
  fresh causal recomputation at `as_of`) and the provenance guards on every field.
  Tier-B/C fields without recorded point-in-time evidence come back `UNSCORABLE` —
  never neutral-filled with `0.0`.

Every response carries `coverage` (cache vs API, oldest/newest session per symbol) and
`limits` (your local depth in weeks vs the tape-depth gates: 8 weeks for the tier-A
fast path, 26 weeks for tape eligibility). Below the gates you get an `escalation`
block naming the hosted `bars_1day` panel that satisfies them. Tape-tier replay is
**roadmap, not available** — we will not imply otherwise.

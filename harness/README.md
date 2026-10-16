# Tiered MCP test harness

Tier 0 (default, CI-gating, offline) — committed synthetic deterministic
fixtures over the real stdio servers. No network, no credits, no broker. Runs
inside the default `python -m pytest -q` suite via `tests/e2e/`. Local JSONL
run transcripts per scenario (`harness/transcript.py`); optional `LOKI_URL`
push only when that env var is set.

- `tests/e2e/test_contract.py` — golden `tools/list` snapshot for all 26 tools
  (names, input/output schemas, read_only/idempotent annotations) + the
  int-vs-str / null-vs-str bug-class pins.
- `tests/e2e/test_scenario_chain.py` — the architecture scenario chain over the
  stdio driver: `pit.analysis` -> `market.regime` -> `market.climate` ->
  `warden.audit_features` -> `gym.probe_fragility` -> `warden.validate_order`
  (accept + over-limit reject) -> `warden.cost_check` -> `warden.explain_sizing`
  -> accepted order + deterministic in-process simulated fill at the house cost
  constants (`HOUSE_SPREAD_BPS=3.0/SLIPPAGE=3.0/ADVERSE=6.0`).
- `tests/e2e/test_simulator_baseline.py` — golden simulator baselines (fixed
  panel, `seeds=[0, 1]`, `per_regime=2`) to catch silent score drift across
  refactors and vendor re-pins (the pin checks hash vendored files; these check
  behavior).
- `tests/e2e/test_negative.py` — fail-closed negative fixtures: malformed /
  batch / oversized inputs through `http_server._tool_calls` and the metering
  caps, hostile args (`redaction.reject_keylike_args`), bad/missing tokens and
  unreachable verify, `/internal/tournament/run` wrong-secret rejection, tier-B
  UNSCORABLE refusal.

Commands (offline, part of the default suite):

    python -m pytest -q
    python scripts/check_pin.py

Run just the e2e tier:

    python -m pytest tests/e2e -q

Regenerate goldens ONLY after an intentional contract/simulator change:

    python -m harness.goldens contract
    python -m harness.goldens simulator

## Tier 1 — agent loop (opt-in, nightly; never merge-gating)

    set HARNESS_AGENT=1
    python -m pytest tests/e2e/test_tier1_agent.py -q

Default mode is scripted tool-trace oracles over the committed cassettes in
`harness/goldens/cassettes/` (offline). The live LLM loop additionally needs
`HARNESS_LLM_API_KEY` (plus optional `HARNESS_LLM_BASE_URL`,
`HARNESS_LLM_MODEL`) AND a hard spend cap (`HARNESS_SPEND_CAP_USD`, default
$1.00; `HARNESS_MAX_LLM_CALLS` caps turns). Scoring rubric:
correct tool selected / verdict quoted verbatim / refusal respected.

## Tier 2 — hosted round-trip (opt-in; spends credits)

    set HARNESS_HOSTED=1
    set SWARM_MCP_ACCESS_TOKEN=<hosted token>
    set HARNESS_MAX_CREDITS=300
    python -m pytest tests/e2e/test_tier2_hosted.py -q
    python -m harness.hosted --max-credits 300            (add --submit for tournament.submit)

Relay `data.bars` -> hosted `gym.estimate_cloud_run` / `label_regimes` /
`probe_fragility` -> `tournament.submit` / `verdict` / `leaderboard`, all under
one hard credit budget cap; a step that would exceed the cap is refused
fail-closed.

## Notes

- Verdict strings are asserted VERBATIM from the tools' actual outputs. Where
  the architecture doc drifted from the code, the code wins: `gym.probe_fragility`
  has no drawdown/recovery/factor-decay scorecard fields (asserted absent), and
  `warden.validate_order`'s accept verdict is `APPROVED`.
- `market.climate` runs fully offline via the committed Open-Meteo
  MockTransport installed by `harness/serve.py --fixture-weather` (server code
  untouched).
- Version alignment: `alerts.json:113`, `smithery.yaml`, and `npm/bin/quant-swarm-mcp.js` are pinned to `0.5.0`.
- Execution boundary: the chain terminates at the warden's accept verdict plus
  an in-process simulated fill. No harness code constructs a broker client or
  contains an order-routing code path (asserted in the chain's final checks).

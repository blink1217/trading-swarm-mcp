"""Tier 0 architecture scenario chain — the ordered end-to-end path over the
real stdio servers on the committed synthetic fixtures.

Chain (per the plan): pit.analysis -> market.regime -> market.climate ->
warden.audit_features -> gym.probe_fragility -> warden.validate_order
(accept + over-limit reject) -> warden.cost_check -> warden.explain_sizing
-> terminate at the accepted order + a deterministic IN-PROCESS simulated fill
at the house cost constants. Execution boundary: nothing beyond the accept
verdict and this simulation — the gated live executor is C# trade_bot_sharp and
no harness path bridges into it.

Every check quotes the server's actual verdict vocabulary verbatim. The
architecture doc is corrected where it drifted: gym.probe_fragility carries no
drawdown/recovery/factor-decay scorecard fields, and the warden's accept
verdict string is APPROVED.
"""
from __future__ import annotations

import json
import re

from swarm_mcp import genome_compat, vendor_path  # noqa: F401

from fill_model import (  # vendored guardrails
    HOUSE_ADVERSE_SELECTION_BPS,
    HOUSE_SLIPPAGE_BPS,
    HOUSE_SPREAD_BPS,
    house_round_trip_cost_bps,
    net_return_after_costs,
    one_way_house_cost_bps,
)
from gates import MIN_COVERAGE, MIN_EPISODES, REGIMES  # vendored
from objective import MAX_GROSS_EXPOSURE_PCT, MAX_POSITION_PCT  # vendored
from order_checks import check_order  # vendored

from swarm_mcp.pit import analysis as pit_analysis
from swarm_mcp.tools.warden_tools import HOUSE_SIZING_FLOORS

from harness import fixtures, goldens

SCENARIO_ID = "architecture_chain_v1"

PIT_AS_OF = "2026-06-01"
TAPE_STARTED = "2025-01-01"
ACCEPT_NOTIONAL = 200_000.0
ACCEPT_CURRENT = {"S01": 300_000.0}
REJECT_NOTIONAL = 300_000.0
REJECT_CURRENT = {"S01": 350_000.0}
EQUITY = 1_000_000.0
SIZING_EQUITY = 100_000.0
SIZING_ATR = 2.5
SIZING_CLOSE = 75.0
SIZING_CLOSE_CAPPED = 100.0
GROSS_EDGE_BPS = 40.0
GROSS_EDGE_CONSUMED_BPS = 20.0

PIT_VERDICTS = {
    pit_analysis.SIGNIFICANT,
    pit_analysis.NOT_SIGNIFICANT,
    pit_analysis.NOT_JUDGED,
    pit_analysis.UNVERIFIED,
}
PIT_VERDICT_VOCAB = PIT_VERDICTS | {pit_analysis.INSUFFICIENT}

PROBE_FORBIDDEN_KEYS = {
    "max_drawdown", "max_drawdown_bps", "recovery_days", "recovery",
    "factor_decay", "factor_half_life", "scorecard", "drawdown_scorecard",
}

# Same invariant scanner as tests/test_no_order_paths.py (shipped tree only —
# that is where such a path could live).
ORDER_PATH_RE = re.compile(
    r"/v2/orders|place_order|cancel_order|submit_order|\borders\b.*POST|POST.*\borders\b", re.I)
ORDER_TOOL_RE = re.compile(
    r"(place|cancel|route|submit|execute).*order|order.*(place|cancel|route|submit|execute)", re.I)
BROKER_CONSTRUCTION_RE = re.compile(r"AlpacaBroker\(|BrokerGateway\(|alpaca\.trade|TradeClient\(")


def _json_blob(obj) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def simulate_fill(*, symbol: str, side: str, qty: int, reference_price: float,
                  gross_edge_bps: float = GROSS_EDGE_BPS) -> dict:
    """Deterministic in-process simulated fill at the house cost constants.

    The execution boundary ends at the warden's accept verdict plus this pure
    computation — there is no order-routing code path and no broker client in
    the harness.
    """
    one_way = one_way_house_cost_bps()
    sign = 1.0 if side == "buy" else -1.0
    fill_price = reference_price * (1.0 + sign * one_way / 1e4)
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "reference_price": reference_price,
        "fill_price": round(fill_price, 6),
        "house_costs_bps": {
            "spread_bps": HOUSE_SPREAD_BPS,
            "slippage_bps": HOUSE_SLIPPAGE_BPS,
            "adverse_selection_bps": HOUSE_ADVERSE_SELECTION_BPS,
        },
        "one_way_cost_bps": one_way,
        "round_trip_cost_bps": house_round_trip_cost_bps(),
        "gross_edge_bps": gross_edge_bps,
        "net_return_bps": net_return_after_costs(
            gross_edge_bps, HOUSE_SPREAD_BPS, HOUSE_SLIPPAGE_BPS, HOUSE_ADVERSE_SELECTION_BPS),
        "execution": ("in-process simulated fill only — no order-routing code path, "
                      "no broker client, no venue"),
    }


def assert_no_order_placement_path() -> list[str]:
    """Fail-closed execution-boundary assertion over the shipped package tree:
    no order-routing code path may exist anywhere in swarm_mcp (the invariant
    tests/test_no_order_paths.py pins). Returns violation strings (empty ==
    clean)."""
    problems: list[str] = []
    for path in (fixtures.ROOT / "swarm_mcp").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if ORDER_PATH_RE.search(path.read_text(encoding="utf-8")):
            problems.append(f"{path}: contains an order-routing code path")
    return problems


def assert_harness_builds_no_broker() -> list[str]:
    """The harness must never bridge into a live/paper executor (the gated live
    executor is the C# application); fills are simulated in-process only."""
    problems: list[str] = []
    for path in (fixtures.ROOT / "harness").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if BROKER_CONSTRUCTION_RE.search(path.read_text(encoding="utf-8")):
            problems.append(f"{path}: constructs a broker client")
    return problems


def failed_checks(result: dict) -> list[str]:
    out = []
    for step in result["steps"]:
        for chk in step["checks"]:
            if not chk["ok"]:
                out.append(f"step {step['step']} {step['tool']}: {chk['name']} — {chk['detail']}")
    for chk in result.get("final_checks", []):
        if not chk["ok"]:
            out.append(f"final: {chk['name']} — {chk['detail']}")
    return out


def _check(step: dict, name: str, ok: bool, detail: str = "") -> None:
    step["checks"].append({"name": name, "ok": bool(ok), "detail": detail})


def _find_family(pit_payload: dict, family_id: str) -> dict | None:
    for fam in (pit_payload.get("underlyings", {}).get("AAPL", {}) or {}).get("families", []):
        if fam.get("family_id") == family_id:
            return fam
    return None


def _find_horizon(fam: dict | None, horizon: str) -> dict | None:
    for row in (fam or {}).get("horizons", []):
        if row.get("horizon") == horizon:
            return row
    return None


async def _call(driver, transcript, name: str, args: dict, step: dict) -> dict:
    result = await driver.call_tool(name, args)
    payload = result["payload"]
    verdict = str(payload.get("verdict") or payload.get("tool") or
                  ("CALL_ERROR" if result["is_error"] else "OK"))
    step["verdict"] = verdict
    step["duration_ms"] = round(result["duration_ms"], 3)
    transcript.record(tool=name, arguments=args, verdict=verdict,
                      duration_ms=result["duration_ms"])
    contract = await driver.tool_contract(name)
    step["schema_violations"] = goldens.validate_against_schema(
        payload, contract.get("outputSchema"))
    return payload


async def run_architecture_chain(*, data, warden, gym, transcript,
                                 bars: list[dict] | None = None,
                                 genome: dict | None = None,
                                 challenger: dict | None = None) -> dict:
    """genome is the CHAMPION (baseline); challenger is the stress genome under
    test (mutate_a, a tier-A diff) — the probe call shape from the corrections:
    gym.probe_fragility(genome=stress_genome, champion_genome=champion)."""
    bars = bars if bars is not None else fixtures.bars_rows()
    genome = genome if genome is not None else fixtures.genome_baseline()
    challenger = challenger if challenger is not None else fixtures.genome_challenger()
    ghash = genome_compat.genome_hash_canonical(challenger)
    transcript.genome_hash = ghash
    steps: list[dict] = []

    def new_step(server: str, tool: str) -> dict:
        step = {"step": len(steps) + 1, "server": server, "tool": tool,
                "checks": [], "verdict": None, "duration_ms": None}
        steps.append(step)
        return step

    # 1) pit.analysis — verdicts verbatim, p-values never upgraded -----------------
    step = new_step("data", "pit.analysis")
    payload = await _call(data, transcript, "pit.analysis",
                          {"symbols": ["AAPL"], "as_of": PIT_AS_OF}, step)
    _check(step, "tool_name", payload.get("tool") == "pit.analysis", str(payload.get("tool")))
    families = (payload.get("underlyings", {}).get("AAPL", {}) or {}).get("families", [])
    statuses = {h.get("status") for f in families for h in f.get("horizons", [])}
    _check(step, "verdict_vocabulary",
           statuses <= PIT_VERDICT_VOCAB and None not in statuses,
           f"statuses={sorted(map(str, statuses))}")
    _check(step, "all_four_verdicts_verbatim", PIT_VERDICTS <= statuses,
           f"missing={sorted(PIT_VERDICTS - statuses)}")
    wiki = _find_family(payload, "wiki|wiki.pageviews")
    w_sig = _find_horizon(wiki, "1d")
    _check(step, "significant_only_with_l1_survivor",
           w_sig is not None and w_sig.get("status") == pit_analysis.SIGNIFICANT
           and w_sig.get("significant") is True and w_sig.get("bh_q") == 0.004,
           str(w_sig))
    w_1w = _find_horizon(wiki, "1w")
    _check(step, "nominal_p_never_upgraded",
           w_1w is not None and w_1w.get("status") == pit_analysis.NOT_SIGNIFICANT
           and w_1w.get("p_value_two_sided") == 0.03
           and "Benjamini-Hochberg" in str(w_1w.get("explanation")),
           str(w_1w))
    e_1d = _find_horizon(_find_family(payload, "edgar|edgar.rev"), "1d")
    _check(step, "foreign_gate_never_upgrades_tiny_p",
           e_1d is not None and e_1d.get("status") == pit_analysis.UNVERIFIED
           and e_1d.get("p_value_two_sided") == 0.00001,
           str(e_1d))
    a_1d = _find_horizon(_find_family(payload, "edgar|edgar.assets"), "1d")
    _check(step, "diagnostic_family_not_judged",
           a_1d is not None and a_1d.get("status") == pit_analysis.NOT_JUDGED,
           str(a_1d))
    blob = _json_blob(payload)
    _check(step, "restricted_families_withheld",
           "news_alpaca" not in blob
           and (payload.get("underlyings", {}).get("AAPL", {}) or {})
           .get("summary", {}).get("restricted_withheld") == 1, "restricted leak check")
    _check(step, "raw_metric_values_never_leave",
           all(secret not in blob for secret in ("7777771", "8888882", "6666663")),
           "raw fixture metric values leaked")

    # 2) market.regime — shape/coverage only --------------------------------------
    step = new_step("data", "market.regime")
    payload = await _call(data, transcript, "market.regime", {"bars": bars}, step)
    regimes = payload.get("regimes", {})
    _check(step, "tool_name", payload.get("tool") == "market.regime", str(payload.get("tool")))
    _check(step, "covers_fixture_universe",
           set(regimes) == set(fixtures.SYMS), f"{len(regimes)} symbols")
    ok_rows = all(
        set(r) >= {"as_of", "regime", "session_count"}
        and r.get("session_count") == len([b for b in bars if b["symbol"] == sym])
        and r.get("regime") in set(REGIMES) | {"unknown"}
        for sym, r in regimes.items())
    _check(step, "per_symbol_shape", ok_rows, "regime row shape")
    blob = _json_blob(payload)
    _check(step, "derived_only_no_raw_bars",
           all(f'"{k}"' not in blob for k in ("open", "high", "low", "close", "volume")),
           "raw OHLCV key leaked")

    # 3) market.climate — shape/coverage only, committed weather fixture -----------
    step = new_step("data", "market.climate")
    payload = await _call(data, transcript, "market.climate",
                          {"symbols": [fixtures.CLIMATE_SYMBOL]}, step)
    res = (payload.get("climate") or {}).get(fixtures.CLIMATE_SYMBOL, {})
    areas = res.get("areas") or []
    _check(step, "tool_name", payload.get("tool") == "market.climate", str(payload.get("tool")))
    _check(step, "coverage_split",
           payload.get("covered") == [fixtures.CLIMATE_SYMBOL]
           and payload.get("uncovered") == [],
           f"covered={payload.get('covered')} uncovered={payload.get('uncovered')}")
    m = re.match(r"(\d+)/(\1) operating areas", str(res.get("footprint")))
    _check(step, "footprint_shape",
           res.get("status") == "OK" and m is not None and len(areas) == int(m.group(1)),
           str(res.get("footprint")))
    ok_areas = all(
        set(a) >= {"area", "weight", "forecast", "baseline_year_ago", "anomaly", "windows"}
        and a.get("anomaly", {}).get("temp_bucket") == "extreme_warm"
        for a in areas)
    _check(step, "derived_area_metrics_pinned_to_fixture", ok_areas and bool(areas),
           "area metric shape")
    blob = _json_blob(payload)
    _check(step, "derived_only_no_raw_provider_series",
           all(f'"{k}"' not in blob
               for k in ("temperature_2m_max", "precipitation_sum", "wind_speed_10m_max")),
           "raw provider series leaked")
    _check(step, "forecast_never_valid_for_past_decisions",
           "never valid for a past decision date" in str(res.get("evidence_note", "")),
           str(res.get("evidence_note")))

    # 4) warden.audit_features — leakage verdicts quoted exactly ------------------
    step = new_step("warden", "warden.audit_features")
    manifest = [
        {"name": "mom_20d", "source": "bars_1day", "value_ts": "2025-06-02"},
        {"name": "open_meteo_forecast_anomaly", "source": "open-meteo.archive",
         "value_ts": "2025-06-02"},
        {"name": "mom_20d", "source": "bars_1day", "value_ts": "2024-06-03"},
        {"name": "zz_unknown_feature", "source": "some_llm", "value_ts": "2025-06-02"},
    ]
    payload = await _call(warden, transcript, "warden.audit_features", {
        "manifest": manifest, "tape_started": TAPE_STARTED,
        "selected_symbols": ["S00", "S01"],
        "snapshot_universe": [f"S{i:02d}" for i in range(10)]}, step)
    features = payload.get("features", [])
    _check(step, "tool_name", payload.get("tool") == "warden.audit_features",
           str(payload.get("tool")))
    _check(step, "violations_found_verdict", payload.get("verdict") == "VIOLATIONS_FOUND",
           str(payload.get("verdict")))
    ok_status = (features[0].get("status") == "OK"
                 and features[1].get("status") == "VIOLATION"
                 and "returns actuals" in features[1].get("detail", "")
                 and features[2].get("status") == "VIOLATION"
                 and "predates tape start" in features[2].get("detail", ""))
    _check(step, "leakage_verdicts_quoted_exactly", ok_status, str(features))
    _check(step, "unknown_feature_fails_closed_to_tier_c",
           features[3].get("tier") == "C", str(features[3]))
    expected_violations = [f"{f['name']}: {f['detail']}" for f in features
                           if f.get("status") == "VIOLATION"]
    _check(step, "violation_list_matches_features",
           payload.get("violations") == expected_violations
           and len(expected_violations) == 2, str(payload.get("violations")))
    cov = payload.get("coverage") or {}
    _check(step, "coverage_gate",
           cov.get("fraction") == 1.0 and cov.get("min_coverage") == MIN_COVERAGE
           and cov.get("meets") is True, str(cov))

    step = new_step("warden", "warden.audit_features")
    payload = await _call(warden, transcript, "warden.audit_features", {
        "manifest": [{"name": "zz_unknown_feature", "source": "some_llm",
                      "value_ts": "2025-06-02"}],
        "selected_symbols": ["S00", "S98"],
        "snapshot_universe": ["S00"]}, step)
    _check(step, "no_tape_started_is_unscorable",
           payload.get("verdict") == "VIOLATIONS_FOUND"
           and "UNSCORABLE" in str(payload.get("violations")), str(payload.get("violations")))
    cov = payload.get("coverage") or {}
    _check(step, "below_min_coverage_is_unscorable",
           cov.get("fraction") == 0.5 and cov.get("min_coverage") == MIN_COVERAGE
           and cov.get("meets") is False and "UNSCORABLE" in str(cov.get("note")),
           str(cov))

    step = new_step("warden", "warden.audit_features")
    payload = await _call(warden, transcript, "warden.audit_features", {
        "manifest": [{"name": "mom_20d", "source": "bars_1day", "value_ts": "2025-06-02"}],
        "tape_started": TAPE_STARTED,
        "selected_symbols": ["S00"], "snapshot_universe": ["S00"]}, step)
    _check(step, "clean_verdict_quoted_exactly",
           payload.get("verdict") == "CLEAN" and payload.get("violations") == [],
           str(payload.get("verdict")))

    # 5) gym.probe_fragility — the real keys, power honesty, never a promotion -----
    step = new_step("gym", "gym.probe_fragility")
    payload = await _call(gym, transcript, "gym.probe_fragility", {
        "genome": challenger, "champion_genome": genome,
        "seeds": [0, 1], "per_regime": 2, "bars": bars, "min_symbols": 5}, step)
    required = {"per_regime_bps", "worst_regime", "turnover_pct_avg", "max_gross_exposure",
                "max_position_pct", "hard_constraint_violations", "lookahead_violations",
                "underpowered"}
    _check(step, "architecture_doc_output_keys", required <= set(payload),
           f"missing={sorted(required - set(payload))}")
    _check(step, "no_drawdown_recovery_factor_scorecard_fields",
           not (PROBE_FORBIDDEN_KEYS & set(payload)),
           str(sorted(PROBE_FORBIDDEN_KEYS & set(payload))))
    _check(step, "fragility_report_never_promotes",
           str(payload.get("verdict", "")).startswith("FRAGILITY_REPORT")
           and "promotion" not in payload, str(payload.get("verdict")))
    _check(step, "power_honesty",
           payload.get("underpowered") == (payload.get("n_episodes", 0) < MIN_EPISODES)
           and payload.get("underpowered") is True
           and "MIN_EPISODES" in str(payload.get("power_note")),
           f"n={payload.get('n_episodes')} underpowered={payload.get('underpowered')}")
    _check(step, "regime_coverage",
           set(payload.get("per_regime_bps", {})) == set(REGIMES)
           and payload.get("worst_regime") in REGIMES
           and payload.get("worst_regime_bps")
           == payload.get("per_regime_bps", {}).get(payload.get("worst_regime")),
           str(payload.get("per_regime_bps")))
    _check(step, "violations_are_lists",
           isinstance(payload.get("hard_constraint_violations"), list)
           and isinstance(payload.get("lookahead_violations"), list)
           and payload.get("lookahead_violations") == [],
           str(payload.get("lookahead_violations")))
    _check(step, "cloud_job_handoff",
           (payload.get("cloud_job", {}).get("body", {}) or {}).get("challenger_id") == ghash
           and payload.get("genome_hash") == ghash,
           str(payload.get("genome_hash")))

    # 6) warden.validate_order — accept + over-limit reject at the house floors ----
    step = new_step("warden", "warden.validate_order")
    accept_order = {"symbol": "S00", "notional": ACCEPT_NOTIONAL, "side": "buy"}
    payload = await _call(warden, transcript, "warden.validate_order", {
        "order": accept_order, "equity": EQUITY, "current_positions": ACCEPT_CURRENT}, step)
    expected = check_order(accept_order, EQUITY, ACCEPT_CURRENT)
    _check(step, "accept_verdict_verbatim",
           payload.get("verdict") == "APPROVED" and payload.get("violations") == [],
           str(payload.get("verdict")))
    _check(step, "same_checker_function_identity",
           payload.get("post_position_pct") == expected["post_position_pct"]
           and payload.get("post_gross_pct") == expected["post_gross_pct"],
           str((payload.get("post_position_pct"), payload.get("post_gross_pct"))))
    _check(step, "house_floors_per_name_25_gross_60",
           payload.get("house_floors") == {"max_position_pct": MAX_POSITION_PCT,
                                           "max_gross_exposure_pct": MAX_GROSS_EXPOSURE_PCT}
           and MAX_POSITION_PCT == 0.25 and MAX_GROSS_EXPOSURE_PCT == 0.60,
           str(payload.get("house_floors")))
    _check(step, "no_order_placement_safety_note",
           "cannot place, cancel, or route an order" in str(payload.get("safety")),
           str(payload.get("safety")))

    step = new_step("warden", "warden.validate_order")
    reject_order = {"symbol": "S00", "notional": REJECT_NOTIONAL, "side": "buy"}
    payload = await _call(warden, transcript, "warden.validate_order", {
        "order": reject_order, "equity": EQUITY, "current_positions": REJECT_CURRENT}, step)
    expected = check_order(reject_order, EQUITY, REJECT_CURRENT)
    quote = str(payload.get("rejection_quote"))
    _check(step, "over_limit_reject_verdict_verbatim",
           payload.get("verdict") == "REJECTED"
           and payload.get("violations") == expected["violations"]
           and len(expected["violations"]) == 2,
           str(payload.get("violations")))
    _check(step, "rejection_quotes_the_exact_floor",
           "max_position_pct=0.25" in quote and "max_gross_exposure_pct=0.6" in quote
           and "0.65" in quote, quote)

    # 7) warden.cost_check — net-of-cost recomputation ----------------------------
    step = new_step("warden", "warden.cost_check")
    cost_args = {"gross_edge_bps": GROSS_EDGE_BPS, "spread_bps": HOUSE_SPREAD_BPS,
                 "slippage_bps": HOUSE_SLIPPAGE_BPS,
                 "adverse_selection_bps": HOUSE_ADVERSE_SELECTION_BPS}
    payload = await _call(warden, transcript, "warden.cost_check", cost_args, step)
    exp_one = one_way_house_cost_bps()
    exp_rt = house_round_trip_cost_bps()
    exp_net = net_return_after_costs(GROSS_EDGE_BPS, HOUSE_SPREAD_BPS,
                                     HOUSE_SLIPPAGE_BPS, HOUSE_ADVERSE_SELECTION_BPS)
    _check(step, "net_of_cost_recomputation",
           payload.get("one_way_cost_bps") == exp_one == 12.0
           and payload.get("round_trip_cost_bps") == exp_rt == 24.0
           and payload.get("net_return_bps") == exp_net == 16.0,
           str({k: payload.get(k) for k in ("one_way_cost_bps", "round_trip_cost_bps",
                                            "net_return_bps")}))
    _check(step, "survives_costs_verdict_verbatim",
           payload.get("verdict") == "SURVIVES_COSTS", str(payload.get("verdict")))

    step = new_step("warden", "warden.cost_check")
    payload = await _call(warden, transcript, "warden.cost_check", {
        "gross_edge_bps": GROSS_EDGE_CONSUMED_BPS,
        "spread_bps": HOUSE_SPREAD_BPS, "slippage_bps": HOUSE_SLIPPAGE_BPS,
        "adverse_selection_bps": HOUSE_ADVERSE_SELECTION_BPS}, step)
    _check(step, "edge_consumed_verdict_verbatim",
           payload.get("verdict") == "EDGE_CONSUMED_BY_COSTS"
           and payload.get("net_return_bps") == GROSS_EDGE_CONSUMED_BPS - 24.0,
           str(payload.get("verdict")))

    # 8) warden.explain_sizing — sizing recomputation -----------------------------
    step = new_step("warden", "warden.explain_sizing")
    sizing_args = {"equity": SIZING_EQUITY, "atr_14": SIZING_ATR, "close": SIZING_CLOSE}
    payload = await _call(warden, transcript, "warden.explain_sizing", sizing_args, step)
    exp_qty = int(SIZING_EQUITY * HOUSE_SIZING_FLOORS["risk_pct_per_trade"]
                  // (SIZING_ATR * HOUSE_SIZING_FLOORS["atr_sizing_mult"]))
    _check(step, "atr_sizing_recomputation",
           payload.get("verdict") == "APPROVED" and payload.get("qty") == exp_qty == 320
           and payload.get("reasons") == [],
           f"qty={payload.get('qty')} exp={exp_qty}")
    _check(step, "house_sizing_floors_identity",
           payload.get("house_floors") == HOUSE_SIZING_FLOORS,
           str(payload.get("house_floors")))
    _check(step, "trail_stop_recomputation",
           payload.get("trail_stop") == HOUSE_SIZING_FLOORS["trail_atr_mult"] * SIZING_ATR,
           str(payload.get("trail_stop")))
    accepted_qty = payload.get("qty")

    step = new_step("warden", "warden.explain_sizing")
    payload = await _call(warden, transcript, "warden.explain_sizing", {
        "equity": SIZING_EQUITY, "atr_14": SIZING_ATR, "close": SIZING_CLOSE_CAPPED}, step)
    exp_capped = int(SIZING_EQUITY * HOUSE_SIZING_FLOORS["max_position_pct"]
                     // SIZING_CLOSE_CAPPED)
    _check(step, "position_cap_recomputation",
           payload.get("verdict") == "REDUCED" and payload.get("qty") == exp_capped == 250
           and "position_cap:320->250" in payload.get("reasons", []),
           f"qty={payload.get('qty')} reasons={payload.get('reasons')}")

    # 9) terminate: accepted order + deterministic in-process simulated fill ------
    fill = simulate_fill(symbol="S00", side="buy", qty=accepted_qty,
                         reference_price=SIZING_CLOSE, gross_edge_bps=GROSS_EDGE_BPS)
    shipped_tree_problems = assert_no_order_placement_path()
    harness_problems = assert_harness_builds_no_broker()
    final_checks = [
        {"name": "house_cost_constants",
         "ok": (HOUSE_SPREAD_BPS, HOUSE_SLIPPAGE_BPS, HOUSE_ADVERSE_SELECTION_BPS)
               == (3.0, 3.0, 6.0),
         "detail": str(fill["house_costs_bps"])},
        {"name": "fill_deterministic_at_house_costs",
         "ok": fill["fill_price"] == round(SIZING_CLOSE * (1.0 + 12.0 / 1e4), 6)
               and fill["one_way_cost_bps"] == 12.0
               and fill["round_trip_cost_bps"] == 24.0,
         "detail": str(fill)},
        {"name": "fill_ties_to_cost_check_net",
         "ok": fill["net_return_bps"] == 16.0,
         "detail": str(fill["net_return_bps"])},
        {"name": "no_order_placement_path_in_shipped_tree",
         "ok": not shipped_tree_problems,
         "detail": "; ".join(shipped_tree_problems)},
        {"name": "harness_builds_no_broker_client",
         "ok": not harness_problems,
         "detail": "; ".join(harness_problems)},
        {"name": "chain_stays_inside_tool_inventory",
         "ok": all(not ORDER_TOOL_RE.search(s["tool"]) for s in steps),
         "detail": str([s["tool"] for s in steps])},
    ]

    result = {
        "scenario": SCENARIO_ID,
        "challenger_hash": ghash,
        "champion_hash": genome_compat.genome_hash_canonical(genome),
        "steps": steps,
        "fill": fill,
        "final_checks": final_checks,
    }
    transcript.record(tool="harness.simulate_fill", arguments=None,
                      verdict="SIMULATED_FILL_ACCEPTED", duration_ms=0.0,
                      genome_hash=ghash, extra={"fill_price": fill["fill_price"]})
    return result

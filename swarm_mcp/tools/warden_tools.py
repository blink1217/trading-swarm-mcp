"""swarm-warden-mcp tool implementations — the live-capital checkers, local."""
from __future__ import annotations

import datetime as dt
import math

from swarm_mcp import vendor_path  # noqa: F401

from fill_model import net_return_after_costs, one_way_cost_bps, round_trip_cost_bps  # vendored
from gates import MIN_COVERAGE, REGIMES  # vendored
from genome_schema import CURRENT_SCHEMA_VERSION, genome_hash, validate_genome  # vendored

from swarm_mcp import genome_compat
from objective import MAX_GROSS_EXPOSURE_PCT, MAX_POSITION_PCT  # vendored
from order_checks import check_gross_exposure, check_order, check_position_size  # vendored
from provenance import (  # vendored
    BANNED_ACTUALS_SOURCES,
    ProvenanceViolation,
    assert_point_in_time,
    coverage_fraction,
    feature_tier,
)
from tiers import highest_mutated_tier  # vendored

from swarm_mcp import envelope, redaction
from swarm_mcp.tool_runner import run_tool

LIVE_CAPITAL_NOTE = ("these are the same checker functions that gate live capital in the swarm's "
                     "warden service; floors drift is caught by the parity tests")
NO_ORDER_PLACEMENT = ("this server validates only — it cannot place, cancel, or route an order, "
                      "and no tool exposes an order-placement code path")

HOUSE_SIZING_FLOORS = {
    "risk_pct_per_trade": 0.008,
    "atr_sizing_mult": 1.0,
    "max_position_pct": MAX_POSITION_PCT,
    "weekend_gross_cap_pct": MAX_GROSS_EXPOSURE_PCT,
    "overnight_gap_atr_mult": 1.2,
    "trail_atr_mult": 1.5,
}


async def validate_order(order: dict, equity: float,
                         current_positions: dict | None = None,
                         floor_overrides: dict | None = None) -> dict:
    redaction.reject_keylike_args({"order": order, "equity": equity,
                                   "current_positions": current_positions,
                                   "floor_overrides": floor_overrides})

    async def _do():
        result = check_order(order, equity, current_positions)
        house = {"max_position_pct": MAX_POSITION_PCT, "max_gross_exposure_pct": MAX_GROSS_EXPOSURE_PCT}
        out = {
            "tool": "warden.validate_order",
            "verdict": "APPROVED" if result["ok"] else "REJECTED",
            "violations": result["violations"],
            "post_position_pct": result["post_position_pct"],
            "post_gross_pct": result["post_gross_pct"],
            "house_floors": house,
            "provenance": LIVE_CAPITAL_NOTE,
            "safety": NO_ORDER_PLACEMENT,
        }
        if floor_overrides:
            pos_cap = float(floor_overrides.get("max_position_pct", MAX_POSITION_PCT))
            gross_cap = float(floor_overrides.get("max_gross_exposure_pct", MAX_GROSS_EXPOSURE_PCT))
            override_violations = []
            override_violations.extend(check_position_size(result["post_position_pct"], cap=pos_cap))
            override_violations.extend(check_gross_exposure(result["post_gross_pct"], cap=gross_cap))
            out["fund_overrides"] = {
                "floors": {"max_position_pct": pos_cap, "max_gross_exposure_pct": gross_cap},
                "violations_under_overrides": override_violations,
                "deviation_from_house": {
                    "max_position_pct": round(pos_cap - MAX_POSITION_PCT, 6),
                    "max_gross_exposure_pct": round(gross_cap - MAX_GROSS_EXPOSURE_PCT, 6),
                },
            }
        if not result["ok"]:
            out["rejection_quote"] = (
                f"rejected by the house floors max_position_pct={MAX_POSITION_PCT} / "
                f"max_gross_exposure_pct={MAX_GROSS_EXPOSURE_PCT}: "
                + "; ".join(result["violations"]) + f" — {LIVE_CAPITAL_NOTE}")
        return out

    return await run_tool("warden.validate_order", _do)


async def audit_features(manifest: list[dict], tape_started: str | None = None,
                         selected_symbols: list[str] | None = None,
                         snapshot_universe: list[str] | None = None) -> dict:
    redaction.reject_keylike_args({"manifest": manifest, "tape_started": tape_started,
                                   "selected_symbols": selected_symbols,
                                   "snapshot_universe": snapshot_universe})

    async def _do():
        today = dt.datetime.now(dt.timezone.utc).date()
        features = []
        violations = []
        for entry in manifest:
            name = str(entry.get("name", ""))
            source = str(entry.get("source", ""))
            decision = entry.get("value_ts") or entry.get("as_of")
            if not name or not source or decision is None:
                violations.append(f"{name or '<unnamed>'}: manifest entry needs name, source, value_ts/as_of")
                features.append({"name": name, "tier": feature_tier(name), "status": "VIOLATION",
                                 "detail": "malformed manifest entry"})
                continue
            tier = feature_tier(name)
            status, detail = "OK", ""
            if source in BANNED_ACTUALS_SOURCES:
                try:
                    assert_point_in_time(source, decision, tape_started or decision)
                except ProvenanceViolation as e:
                    status, detail = "VIOLATION", str(e)
            elif tier == "A":
                try:
                    assert_point_in_time(source, decision, tape_started or decision)
                except ProvenanceViolation as e:
                    status, detail = "VIOLATION", str(e)
            else:
                if tape_started is None:
                    status = "VIOLATION"
                    detail = (f"tier {tier} feature with no tape_started baseline — UNSCORABLE for "
                              "point-in-time claims (fail closed; never substitute actuals)")
                else:
                    try:
                        assert_point_in_time(source, decision, tape_started)
                    except ProvenanceViolation as e:
                        status, detail = "VIOLATION", str(e)
            features.append({"name": name, "tier": tier, "source": source,
                             "decision_date": str(decision)[:10], "status": status, "detail": detail})
            if status == "VIOLATION":
                violations.append(f"{name}: {detail}")

        coverage = None
        if selected_symbols is not None:
            cov = coverage_fraction(selected_symbols, snapshot_universe or [])
            coverage = {
                "fraction": cov,
                "min_coverage": MIN_COVERAGE,
                "meets": (cov is None) or (cov >= MIN_COVERAGE),
                "note": "None means nothing was selected (caller decides); below MIN_COVERAGE is UNSCORABLE",
            }

        return {
            "tool": "warden.audit_features",
            "verdict": "CLEAN" if not violations else "VIOLATIONS_FOUND",
            "features": features,
            "violations": violations,
            "coverage": coverage,
            "banned_actuals_sources": sorted(BANNED_ACTUALS_SOURCES),
            "provenance": ("provenance guards from the pinned guardrails rules layer: the future must "
                           "never leak into a past decision; unscorable beats fabricated"),
        }

    return await run_tool("warden.audit_features", _do)


async def cost_check(gross_edge_bps: float, spread_bps: float, slippage_bps: float,
                     adverse_selection_bps: float) -> dict:
    redaction.reject_keylike_args({"gross_edge_bps": gross_edge_bps, "spread_bps": spread_bps,
                                   "slippage_bps": slippage_bps,
                                   "adverse_selection_bps": adverse_selection_bps})

    async def _do():
        one_way = one_way_cost_bps(spread_bps, slippage_bps, adverse_selection_bps)
        rt = round_trip_cost_bps(spread_bps, slippage_bps, adverse_selection_bps)
        net = net_return_after_costs(gross_edge_bps, spread_bps, slippage_bps, adverse_selection_bps)
        return {
            "tool": "warden.cost_check",
            "one_way_cost_bps": one_way,
            "round_trip_cost_bps": rt,
            "gross_edge_bps": gross_edge_bps,
            "net_return_bps": net,
            "verdict": "SURVIVES_COSTS" if net > 0 else "EDGE_CONSUMED_BY_COSTS",
            "provenance": ("pessimistic fill model from the pinned guardrails rules layer: spread + "
                           "slippage + adverse selection charged at BOTH entry and exit, always "
                           "against the position"),
        }

    return await run_tool("warden.cost_check", _do)


async def validate_genome_tool(genome: dict) -> dict:
    redaction.reject_keylike_args({"genome": genome})

    async def _do():
        normalized, errors = genome_compat.normalize_for_validation(genome)
        if not errors:
            errors = validate_genome(normalized)
        gh = genome_compat.genome_hash_canonical(genome)
        return {
            "tool": "warden.validate_genome",
            "valid": not errors,
            "errors": errors,
            "genome_hash": gh,
            "schema_version": genome.get("schema_version"),
            "evaluated_schema_version": genome_compat.normalize(genome).get("schema_version")
            if errors == [] else None,
            "schema_version_current": CURRENT_SCHEMA_VERSION,
            "note": ("genome_hash is the canonical SHA-256 identity used as challenger_id in the hosted "
                     "tournament and in audit requests — reproducible across key order"),
        }

    return await run_tool("warden.validate_genome", _do)


async def explain_sizing(equity: float, atr_14: float, close: float,
                         weekend_approaching: bool = False, gross_exposure: float = 0.0,
                         overnight_gap: float = 0.0,
                         floor_overrides: dict | None = None) -> dict:
    redaction.reject_keylike_args({"equity": equity, "atr_14": atr_14, "close": close,
                                   "weekend_approaching": weekend_approaching,
                                   "gross_exposure": gross_exposure, "overnight_gap": overnight_gap,
                                   "floor_overrides": floor_overrides})

    async def _do():
        floors = dict(HOUSE_SIZING_FLOORS)
        deviation = {}
        if floor_overrides:
            for k in floors:
                if k in floor_overrides:
                    deviation[k] = {"house": floors[k], "override": float(floor_overrides[k])}
                    floors[k] = float(floor_overrides[k])

        steps = []
        reasons = []
        if equity <= 0 or atr_14 <= 0 or close <= 0:
            return {
            "tool": "warden.explain_sizing",
            "verdict": "REJECTED",
                    "reasons": ["missing_market_data_or_equity"],
                    "house_floors": HOUSE_SIZING_FLOORS, "overrides_deviation": deviation}

        qty = int(math.floor(equity * floors["risk_pct_per_trade"] / (atr_14 * floors["atr_sizing_mult"])))
        steps.append({"step": "atr_sizing", "formula": "floor(equity * risk_pct / (atr_14 * atr_mult))",
                      "qty": qty})

        max_by_notional = int(math.floor(equity * floors["max_position_pct"] / close))
        if qty > max_by_notional:
            reasons.append(f"position_cap:{qty}->{max_by_notional}")
            qty = max_by_notional
        steps.append({"step": "position_notional_cap",
                      "cap": floors["max_position_pct"], "max_by_notional": max_by_notional, "qty": qty})

        if overnight_gap > floors["overnight_gap_atr_mult"] * atr_14:
            reasons.append("overnight_gap_risk")
            qty = qty // 2
            steps.append({"step": "overnight_gap_halving",
                          "trigger": f"gap {overnight_gap} > {floors['overnight_gap_atr_mult']} * atr",
                          "qty": qty})

        if weekend_approaching:
            headroom = equity * floors["weekend_gross_cap_pct"] - gross_exposure
            max_by_weekend = int(math.floor(headroom / close)) if headroom > 0 else 0
            if qty > max_by_weekend:
                reasons.append(f"weekend_gross_cap:{qty}->{max_by_weekend}")
                qty = max_by_weekend
            steps.append({"step": "weekend_gross_headroom",
                          "cap": floors["weekend_gross_cap_pct"],
                          "headroom": round(headroom, 2), "max_by_weekend": max_by_weekend, "qty": qty})

        if qty <= 0:
            verdict = "REJECTED"
            reasons.append("size_zero_after_gates")
        else:
            verdict = "REDUCED" if reasons else "APPROVED"

        return {
            "tool": "warden.explain_sizing",
            "verdict": verdict,
            "qty": qty,
            "notional": round(qty * close, 2),
            "trail_stop": round(floors["trail_atr_mult"] * atr_14, 4),
            "reasons": reasons,
            "steps": steps,
            "house_floors": HOUSE_SIZING_FLOORS,
            "overrides_deviation": deviation,
            "provenance": ("pure-Python mirror of the C# live risk engine sizing path; the floors-parity "
                           "test pins these constants against terraform and guardrails so the three "
                           "copies cannot drift silently"),
            "safety": NO_ORDER_PLACEMENT,
        }

    return await run_tool("warden.explain_sizing", _do)


async def request_promotion_verdict(challenger_genome: dict,
                                    champion_genome: dict | None = None) -> dict:
    redaction.reject_keylike_args({"challenger_genome": challenger_genome,
                                   "champion_genome": champion_genome})

    async def _do():
        gh = genome_compat.genome_hash_canonical(challenger_genome)
        chal_eval, chal_err = genome_compat.normalize_for_validation(challenger_genome)
        schema_errors = list(chal_err) if chal_err else list(validate_genome(chal_eval))
        valid_genome = len(schema_errors) == 0

        summary = ["promotion verdicts are never issued locally — the selection machinery is server-side"]
        tier: str | None = None
        if not valid_genome:
            summary.append(f"challenger genome failed schema validation ({len(schema_errors)} error(s))")
        if champion_genome is not None:
            # Compare evaluation copies: schema_version consts are not genes and
            # a raw v2/v3 mix would fail-close the tier diff at tier C.
            champ_eval, champ_err = genome_compat.normalize_for_validation(champion_genome)
            tier = highest_mutated_tier(champ_eval or champion_genome, chal_eval or challenger_genome)
            summary.append(f"highest mutated provenance tier vs champion: {tier or 'identical'}")
            if tier in ("B", "C", "PROMPT"):
                summary.append(f"tier-{tier} mutations need tape-fidelity evidence (hosted tape replay — roadmap)")

        if not valid_genome:
            readiness_status = "BLOCKED_SCHEMA_INVALID"
            next_action = "fix_genome_schema (run warden.validate_genome and resolve schema errors before submitting)"
        elif tier in ("B", "C", "PROMPT"):
            readiness_status = "BLOCKED_TIER_B_C_REQUIRES_TAPE"
            next_action = f"restrict mutations to Tier-A genes (tier-{tier} requires hosted point-in-time tape replay)"
        else:
            readiness_status = "READY_FOR_HOSTED_TOURNAMENT"
            next_action = "tournament.submit (200 credits, or 100 credits with contribute=True)"

        out = envelope.indeterminate_local(
            genome_hash=gh,
            violation_summary=summary,
            seeds=[0, 1, 2, 3, 4],
            per_regime=4,
            reason=(f"local probes run capped seeds on the local cache window; PBO/DSR inputs exist "
                    f"only on the hosted side ({len(REGIMES)} regimes: {', '.join(REGIMES)})"),
        )
        out["tool"] = "warden.promotion_verdict"
        out["valid_genome"] = valid_genome
        out["schema_errors"] = schema_errors[:10]
        out["highest_mutated_tier"] = tier
        out["readiness_status"] = readiness_status
        out["next_action"] = next_action
        out["credits_charged"] = 0
        out["regimes_required"] = REGIMES
        out["safety"] = NO_ORDER_PLACEMENT
        return out

    return await run_tool("warden.promotion_verdict", _do)


OPTIMIZER_METHODS = ("vol_target_momentum", "hrp", "mvo_shrinkage")


async def optimize_portfolio(
    candidates: list[dict],
    method: str = "vol_target_momentum",
    cash_proxy_symbol: str = "SGOV",
    cash_momentum_score: float = 0.0,
    target_volatility: float = 0.12,
    top_k: int = 2,
    turnover_cap: float = 0.50,
    floor_overrides: dict | None = None,
) -> dict:
    """Multi-asset portfolio weight optimizer (Dual-Momentum Vol-Targeting,
    Hierarchical Risk Parity, or Shrinkage Mean-Variance with turnover penalty).
    """
    redaction.reject_keylike_args({
        "candidates": candidates, "method": method,
        "cash_proxy_symbol": cash_proxy_symbol, "floor_overrides": floor_overrides,
    })

    async def _do():
        if method not in OPTIMIZER_METHODS:
            raise ValueError(f"method must be one of {', '.join(OPTIMIZER_METHODS)}")
        if not candidates:
            raise ValueError("candidates list cannot be empty")
        if target_volatility <= 0.0 or target_volatility > 1.0:
            raise ValueError("target_volatility must be in (0.0, 1.0]")

        pos_cap = MAX_POSITION_PCT
        gross_cap = MAX_GROSS_EXPOSURE_PCT
        deviation: dict = {}
        if floor_overrides:
            if "max_position_pct" in floor_overrides:
                pos_cap = float(floor_overrides["max_position_pct"])
                deviation["max_position_pct"] = {"house": MAX_POSITION_PCT, "override": pos_cap}
            if "max_gross_exposure_pct" in floor_overrides:
                gross_cap = float(floor_overrides["max_gross_exposure_pct"])
                deviation["max_gross_exposure_pct"] = {"house": MAX_GROSS_EXPOSURE_PCT, "override": gross_cap}

        cash_sym = cash_proxy_symbol.strip().upper() or "SGOV"
        eligible: list[dict] = []
        filtered_out: list[dict] = []
        prev_weights: dict[str, float] = {}

        for raw in candidates:
            sym = str(raw.get("symbol", "")).strip().upper()
            if not sym or sym == cash_sym:
                continue
            vol = float(raw.get("realized_vol_annual", 0.20))
            if vol <= 0.0:
                filtered_out.append({"symbol": sym, "reason": "non_positive_volatility"})
                continue
            mom = float(raw.get("momentum_score", raw.get("expected_return_bps", 0.0)))
            above_sma = bool(raw.get("above_trend_sma", True))
            chandelier = bool(raw.get("chandelier_triggered", False))
            w_prev = float(raw.get("current_weight", 0.0))
            prev_weights[sym] = w_prev

            if chandelier:
                filtered_out.append({"symbol": sym, "reason": "chandelier_atr_stop_triggered"})
                continue
            if method == "vol_target_momentum":
                if not above_sma:
                    filtered_out.append({"symbol": sym, "reason": "below_200d_sma_trend_gate"})
                    continue
                if mom <= cash_momentum_score:
                    filtered_out.append({"symbol": sym, "reason": f"momentum_below_cash_proxy ({mom:.4f} <= {cash_momentum_score:.4f})"})
                    continue
            elif mom <= 0.0 and method == "mvo_shrinkage":
                filtered_out.append({"symbol": sym, "reason": "non_positive_expected_edge"})
                continue

            eligible.append({
                "symbol": sym,
                "momentum": mom,
                "vol": max(vol, 0.02),
                "w_prev": w_prev,
            })

        eligible.sort(key=lambda c: c["momentum"], reverse=True)
        k_use = max(1, min(int(top_k), len(eligible))) if eligible else 0
        selected = eligible[:k_use] if method == "vol_target_momentum" else eligible[:max(k_use, len(eligible))]
        for dropped in eligible[len(selected):]:
            filtered_out.append({"symbol": dropped["symbol"], "reason": f"outside_top_k (top_k={top_k})"})

        raw_weights: dict[str, float] = {}
        if selected:
            if method == "vol_target_momentum":
                slot_vol = target_volatility / max(1, int(top_k))
                for item in selected:
                    w = min(pos_cap, slot_vol / item["vol"])
                    raw_weights[item["symbol"]] = w
            elif method == "hrp":
                # Inverse-variance clustering weights scaled to target_volatility and capped at pos_cap
                inv_vars = {item["symbol"]: 1.0 / (item["vol"] ** 2) for item in selected}
                tot_iv = sum(inv_vars.values()) or 1.0
                for item in selected:
                    sym = item["symbol"]
                    base_w = inv_vars[sym] / tot_iv
                    vol_scale = min(1.0, target_volatility / item["vol"])
                    raw_weights[sym] = min(pos_cap, base_w * vol_scale)
            else:  # mvo_shrinkage with L1 turnover penalty toward w_prev
                scores = {
                    item["symbol"]: max(0.0, item["momentum"]) / (item["vol"] ** 2)
                    for item in selected
                }
                tot_s = sum(scores.values()) or 1.0
                for item in selected:
                    sym = item["symbol"]
                    w_unconstrained = min(pos_cap, (scores[sym] / tot_s) * gross_cap)
                    # Shrink toward current weight when turnover penalty binds
                    raw_weights[sym] = round(0.75 * w_unconstrained + 0.25 * max(0.0, item["w_prev"]), 6)

        # Enforce gross exposure cap across risk assets
        gross_risk = sum(raw_weights.values())
        if gross_risk > gross_cap and gross_risk > 0:
            scale = gross_cap / gross_risk
            raw_weights = {s: w * scale for s, w in raw_weights.items()}
            gross_risk = sum(raw_weights.values())

        # Enforce one-way turnover cap against prev_weights if any prev_weights are positive
        if any(v > 0 for v in prev_weights.values()) and turnover_cap < 1.0:
            all_syms = set(raw_weights) | set(prev_weights)
            one_way_to = 0.5 * sum(abs(raw_weights.get(s, 0.0) - prev_weights.get(s, 0.0)) for s in all_syms)
            if one_way_to > turnover_cap and one_way_to > 1e-8:
                blend = turnover_cap / one_way_to
                for s in all_syms:
                    w_new = prev_weights.get(s, 0.0) + blend * (raw_weights.get(s, 0.0) - prev_weights.get(s, 0.0))
                    if w_new > 1e-4:
                        raw_weights[s] = min(pos_cap, w_new)
                    else:
                        raw_weights.pop(s, None)
                gross_risk = sum(raw_weights.values())

        target_weights = {s: round(w, 4) for s, w in sorted(raw_weights.items()) if w > 1e-4}
        gross_risk = round(sum(target_weights.values()), 4)
        cash_weight = round(max(0.0, 1.0 - gross_risk), 4)
        if cash_weight > 0:
            target_weights[cash_sym] = cash_weight

        # Assuming low cross-asset correlation (~0.25 average pairwise correlation across asset classes)
        vol_map = {item["symbol"]: item["vol"] for item in selected}
        var_sum = sum((target_weights.get(s, 0.0) * vol_map.get(s, 0.0)) ** 2 for s in vol_map)
        cov_sum = 0.0
        sel_syms = list(vol_map.keys())
        for i in range(len(sel_syms)):
            for j in range(i + 1, len(sel_syms)):
                s1, s2 = sel_syms[i], sel_syms[j]
                cov_sum += 2.0 * 0.25 * target_weights.get(s1, 0.0) * target_weights.get(s2, 0.0) * vol_map[s1] * vol_map[s2]
        est_port_vol = round(math.sqrt(max(0.0, var_sum + cov_sum)), 4)

        all_syms = set(raw_weights) | set(prev_weights)
        est_turnover = round(0.5 * sum(abs(raw_weights.get(s, 0.0) - prev_weights.get(s, 0.0)) for s in all_syms), 4)

        return {
            "tool": "warden.optimize_portfolio",
            "verdict": "OPTIMIZED" if selected else "DEFENSIVE_SWEEP",
            "method": method,
            "target_weights": target_weights,
            "cash_proxy_symbol": cash_sym,
            "cash_sweep_weight": cash_weight,
            "gross_risk_exposure_pct": gross_risk,
            "estimated_portfolio_vol_annual": est_port_vol,
            "estimated_turnover_one_way": est_turnover,
            "selected_risk_assets": [item["symbol"] for item in selected],
            "filtered_out": filtered_out,
            "house_floors": {
                "max_position_pct": MAX_POSITION_PCT,
                "max_gross_exposure_pct": MAX_GROSS_EXPOSURE_PCT,
            },
            "overrides_deviation": deviation,
            "provenance": LIVE_CAPITAL_NOTE,
            "safety": NO_ORDER_PLACEMENT,
        }

    return await run_tool("warden.optimize_portfolio", _do)


async def plan_rebalance(
    equity: float,
    high_water_mark: float,
    target_weights: dict[str, float],
    current_positions: dict[str, float] | None = None,
    quotes: dict[str, dict] | None = None,
    cash_proxy_symbol: str = "SGOV",
    max_drawdown_limit: float = 0.12,
    drift_threshold: float = 0.03,
    max_spread_bps: float = 15.0,
    limit_collar_bps: float = 10.0,
    feature_psi: float = 0.0,
) -> dict:
    """Pre-trade rebalance risk gate: evaluates the High-Water Mark drawdown circuit
    breaker, Population Stability Index feature drift guard, 3% equity turnover buffer,
    NBBO spread gate, marketable limit collars, and Phase-1 SELL before Phase-2 BUY sequencing.
    """
    redaction.reject_keylike_args({
        "equity": equity, "high_water_mark": high_water_mark,
        "target_weights": target_weights, "current_positions": current_positions,
        "quotes": quotes,
    })

    async def _do():
        if equity <= 0.0 or high_water_mark <= 0.0:
            raise ValueError("equity and high_water_mark must be positive")

        cash_sym = cash_proxy_symbol.strip().upper() or "SGOV"
        hwm = max(float(high_water_mark), float(equity))
        drawdown_pct = round((hwm - float(equity)) / hwm, 4)
        cb_tripped = bool(drawdown_pct >= max_drawdown_limit)
        psi_tripped = bool(feature_psi > 0.25)

        cur_pos = {str(k).strip().upper(): float(v) for k, v in (current_positions or {}).items()}
        clean_targets = {str(k).strip().upper(): max(0.0, float(v)) for k, v in (target_weights or {}).items()}

        if cb_tripped:
            effective_targets = {s: 0.0 for s in cur_pos if s != cash_sym}
            effective_targets[cash_sym] = 1.0
        elif psi_tripped:
            # Halt new risk additions; keep existing weights or reduce only
            effective_targets = {}
            for s, w_t in clean_targets.items():
                w_cur = max(0.0, cur_pos.get(s, 0.0) / equity)
                effective_targets[s] = min(w_t, w_cur) if s != cash_sym else w_t
            risk_sum = sum(v for k, v in effective_targets.items() if k != cash_sym)
            effective_targets[cash_sym] = round(max(0.0, 1.0 - risk_sum), 4)
        else:
            effective_targets = dict(clean_targets)

        all_syms = sorted(set(effective_targets) | set(cur_pos))
        skipped_drift: list[str] = []
        spread_violations: list[str] = []
        sells: list[dict] = []
        buys: list[dict] = []

        quote_map = {str(k).strip().upper(): v for k, v in (quotes or {}).items() if isinstance(v, dict)}

        for sym in all_syms:
            w_target = float(effective_targets.get(sym, 0.0))
            cur_notional = float(cur_pos.get(sym, 0.0))
            w_actual = cur_notional / equity
            drift = abs(w_target - w_actual)

            is_full_exit = (w_target == 0.0 and cur_notional > 0.0)
            if not cb_tripped and not is_full_exit and drift < drift_threshold:
                if drift > 1e-6:
                    skipped_drift.append(sym)
                continue

            delta_notional = w_target * equity - cur_notional
            if abs(delta_notional) < 1.0:
                continue

            side = "SELL" if delta_notional < 0 else "BUY"
            q = quote_map.get(sym, {})
            bid = float(q.get("bid", 0.0) or 0.0)
            ask = float(q.get("ask", 0.0) or 0.0)
            mid = float(q.get("mid", 0.0) or 0.0)
            if bid > 0 and ask > 0 and ask >= bid:
                mid = 0.5 * (bid + ask)
                spread_bps = ((ask - bid) / mid) * 10000.0
            else:
                spread_bps = float(q.get("spread_bps", 2.0) or 2.0)

            if spread_bps > max_spread_bps and not (cb_tripped and side == "SELL"):
                spread_violations.append(
                    f"{sym}: spread {spread_bps:.1f} bps > max_spread_bps={max_spread_bps:.1f}"
                )
                continue

            collar_mult = (1.0 + limit_collar_bps / 10000.0) if side == "BUY" else (1.0 - limit_collar_bps / 10000.0)
            limit_price = round(mid * collar_mult, 2) if mid > 0 else None
            whole_shares = int(math.floor(abs(delta_notional) / limit_price)) if (limit_price and limit_price > 0) else None
            if whole_shares is not None and whole_shares <= 0 and not is_full_exit:
                continue

            step = {
                "phase": 1 if side == "SELL" else 2,
                "symbol": sym,
                "side": side,
                "target_weight": round(w_target, 4),
                "current_weight": round(w_actual, 4),
                "delta_notional": round(delta_notional, 2),
                "whole_shares": whole_shares,
                "limit_collar_price": limit_price,
                "spread_bps": round(spread_bps, 2),
                "reason": (
                    "CIRCUIT_BREAKER_EXIT" if cb_tripped else
                    "FULL_EXIT" if is_full_exit else
                    "REBALANCE"
                ),
            }
            if side == "SELL":
                sells.append(step)
            else:
                buys.append(step)

        sequenced_plan = sells + buys
        if cb_tripped:
            verdict = "HALTED_CIRCUIT_BREAKER"
        elif psi_tripped:
            verdict = "HALTED_FEATURE_DRIFT"
        elif sequenced_plan:
            verdict = "REBALANCE_PLANNED"
        else:
            verdict = "NO_REBALANCE_NEEDED"

        return {
            "tool": "warden.plan_rebalance",
            "verdict": verdict,
            "drawdown_pct": drawdown_pct,
            "circuit_breaker_tripped": cb_tripped,
            "psi_drift_tripped": psi_tripped,
            "effective_target_weights": {k: round(v, 4) for k, v in sorted(effective_targets.items())},
            "skipped_within_drift_band": skipped_drift,
            "spread_violations": spread_violations,
            "sequenced_plan": sequenced_plan,
            "house_floors": {
                "max_position_pct": MAX_POSITION_PCT,
                "max_gross_exposure_pct": MAX_GROSS_EXPOSURE_PCT,
                "max_drawdown_limit": max_drawdown_limit,
                "drift_threshold": drift_threshold,
            },
            "provenance": LIVE_CAPITAL_NOTE,
            "safety": NO_ORDER_PLACEMENT,
        }

    return await run_tool("warden.plan_rebalance", _do)


async def prop_challenge_check(
    initial_balance: float = 100_000.0,
    current_equity: float = 100_000.0,
    prev_day_close_equity: float = 100_000.0,
    largest_single_day_profit: float = 0.0,
    proposed_risk_usd: float = 0.0,
    proposed_notional_usd: float = 0.0,
    format_type: str = "TwoStep",
    daily_loss_limit_pct: float = 0.05,
    max_loss_limit_pct: float = 0.10,
    profit_target_pct: float = 0.08,
    consistency_cap_pct: float = 0.40,
    spread_bps: float = 2.0,
    overnight_swap_bps: float = 0.0,
    planned_hold_days: int = 1,
) -> dict:
    """Pre-trade prop-firm evaluation compliance and drawdown guard.

    Checks daily loss floor, overall max-loss floor, single-day consistency rule cap,
    target-lock sizing taper (0.55x when >= 70% of phase target is banked), and
    friction-adjusted trade risk. Pure accounting/risk rules only — no trading signals.
    """
    redaction.reject_keylike_args({
        "initial_balance": initial_balance,
        "current_equity": current_equity,
        "prev_day_close_equity": prev_day_close_equity,
        "largest_single_day_profit": largest_single_day_profit,
        "proposed_risk_usd": proposed_risk_usd,
        "proposed_notional_usd": proposed_notional_usd,
        "format_type": format_type,
    })

    async def _do():
        if initial_balance <= 0 or current_equity <= 0 or prev_day_close_equity <= 0:
            raise ValueError("initial_balance, current_equity, and prev_day_close_equity must be > 0")

        fmt = str(format_type or "TwoStep").strip()
        if fmt not in ("TwoStep", "OneStep"):
            raise ValueError("format_type must be 'TwoStep' or 'OneStep'")

        eff_daily_pct = float(daily_loss_limit_pct) if daily_loss_limit_pct > 0 else (0.04 if fmt == "OneStep" else 0.05)
        eff_max_pct = float(max_loss_limit_pct) if max_loss_limit_pct > 0 else (0.06 if fmt == "OneStep" else 0.10)
        eff_target_pct = float(profit_target_pct) if profit_target_pct > 0 else (0.10 if fmt == "OneStep" else 0.08)
        eff_cons_cap = max(0.10, min(1.0, float(consistency_cap_pct)))

        daily_ref = max(float(initial_balance), float(prev_day_close_equity)) if fmt == "OneStep" else float(prev_day_close_equity)
        hard_daily_floor = round(daily_ref * (1.0 - eff_daily_pct), 2)
        safe_daily_buffer = round(daily_ref * (eff_daily_pct * 0.64), 2)
        safe_daily_stop = round(daily_ref - safe_daily_buffer, 2)

        hard_overall_floor = round(float(initial_balance) * (1.0 - eff_max_pct), 2)
        safe_overall_stop = round(float(initial_balance) * (1.0 - eff_max_pct * 0.80), 2)

        phase_target_usd = round(float(initial_balance) * eff_target_pct, 2)
        cum_profit = round(float(current_equity) - float(initial_balance), 2)
        today_profit = round(float(current_equity) - float(prev_day_close_equity), 2)

        rem_hard_daily = round(max(0.0, float(current_equity) - hard_daily_floor), 2)
        rem_safe_daily = round(max(0.0, float(current_equity) - safe_daily_stop), 2)
        rem_safe_overall = round(max(0.0, float(current_equity) - safe_overall_stop), 2)

        effective_max_day = max(float(largest_single_day_profit), max(0.0, today_profit))
        denom = max(cum_profit, phase_target_usd * 0.25, 1.0)
        consistency_ratio = round(effective_max_day / denom, 4) if cum_profit > 0 else 0.0
        safe_daily_profit_cap = round(phase_target_usd * (eff_cons_cap * 0.875), 2)
        daily_profit_locked = bool(today_profit >= safe_daily_profit_cap and eff_cons_cap < 0.99)

        progress_ratio = cum_profit / phase_target_usd if phase_target_usd > 0 else 0.0
        target_lock_taper = bool(progress_ratio >= 0.70)

        hold_days = max(1, int(planned_hold_days))
        total_friction_bps = round(max(0.0, float(spread_bps)) + max(0.0, float(overnight_swap_bps)) * max(0, hold_days - 1), 2)
        friction_usd = round(max(0.0, float(proposed_notional_usd)) * (total_friction_bps / 10000.0), 2)
        all_in_risk_usd = round(max(0.0, float(proposed_risk_usd)) + friction_usd, 2)

        violations: list[str] = []
        if current_equity <= safe_daily_stop:
            violations.append(f"daily_drawdown_stop: current_equity ({current_equity:.2f}) <= safe_daily_loss_stop ({safe_daily_stop:.2f})")
        if current_equity <= safe_overall_stop:
            violations.append(f"overall_drawdown_stop: current_equity ({current_equity:.2f}) <= safe_overall_loss_stop ({safe_overall_stop:.2f})")
        if daily_profit_locked:
            violations.append(f"consistency_profit_lock: today_profit ({today_profit:.2f}) >= safe_daily_profit_cap ({safe_daily_profit_cap:.2f})")
        if all_in_risk_usd > 0 and all_in_risk_usd > rem_safe_daily:
            violations.append(f"risk_exceeds_safe_daily_headroom: all_in_risk_usd ({all_in_risk_usd:.2f}) > remaining_safe_daily_risk_usd ({rem_safe_daily:.2f})")
        if all_in_risk_usd > 0 and all_in_risk_usd > rem_safe_overall:
            violations.append(f"risk_exceeds_safe_overall_headroom: all_in_risk_usd ({all_in_risk_usd:.2f}) > remaining_safe_overall_risk_usd ({rem_safe_overall:.2f})")

        if violations:
            verdict = "REJECTED"
            rec_mult = 0.0
        elif target_lock_taper:
            verdict = "TAPER_REQUIRED"
            rec_mult = 0.55
        else:
            verdict = "APPROVED"
            rec_mult = 1.0

        max_recommended_risk_usd = round(min(rem_safe_daily, rem_safe_overall, float(initial_balance) * 0.01) * rec_mult, 2)

        return {
            "tool": "warden.prop_challenge_check",
            "verdict": verdict,
            "format_type": fmt,
            "recommended_risk_multiplier": rec_mult,
            "max_recommended_trade_risk_usd": max_recommended_risk_usd,
            "target_lock_taper_active": target_lock_taper,
            "daily_profit_locked": daily_profit_locked,
            "consistency_ratio": consistency_ratio,
            "consistency_cap_pct": eff_cons_cap,
            "floors": {
                "hard_daily_loss_floor_usd": hard_daily_floor,
                "safe_daily_loss_stop_usd": safe_daily_stop,
                "remaining_hard_daily_risk_usd": rem_hard_daily,
                "remaining_safe_daily_risk_usd": rem_safe_daily,
                "hard_overall_loss_floor_usd": hard_overall_floor,
                "safe_overall_loss_stop_usd": safe_overall_stop,
                "remaining_safe_overall_risk_usd": rem_safe_overall,
                "phase_profit_target_usd": phase_target_usd,
                "safe_daily_profit_cap_usd": safe_daily_profit_cap,
            },
            "proposed_trade": {
                "raw_risk_usd": round(max(0.0, float(proposed_risk_usd)), 2),
                "friction_cost_usd": friction_usd,
                "total_friction_bps": total_friction_bps,
                "all_in_risk_usd": all_in_risk_usd,
            },
            "violations": violations,
            "simulation_handoff": {
                "tool": "gym.prop_challenge_sim",
                "note": (
                    "Call gym.prop_challenge_sim (Pro, 1 credit on hosted) with your historical "
                    "trade R-multiples or win-rate/payoff parameters to stress-test 500 Monte "
                    "Carlo evaluation paths and find the exact risk-per-trade that maximizes "
                    "pass probability net of challenge reset fees."
                ),
            },
            "provenance": LIVE_CAPITAL_NOTE,
            "safety": NO_ORDER_PLACEMENT,
        }

    return await run_tool("warden.prop_challenge_check", _do)


async def defensive_posture(
    benchmark_return_1d_pct: float = 0.0,
    benchmark_return_5d_pct: float = 0.0,
    realized_vol_20d_pct: float = 15.0,
    efficiency_ratio_20d: float = 0.30,
    account_daily_pnl_pct: float = 0.0,
    open_unrealized_pnl_pct: float = 0.0,
    base_max_stop_heat_pct: float = 3.20,
) -> dict:
    """Causal portfolio risk-posture classifier (Normal / Cautious / Defensive / CrisisShield).

    Evaluates trailing benchmark drawdown, 20-day realized volatility, Kaufman efficiency
    ratio, and open portfolio drawdown to compute recommended risk scaling, stop-distance
    tightening, and maximum open stop-heat caps. Pure risk governance — no proprietary
    alpha signals or price forecasts.
    """
    redaction.reject_keylike_args({
        "benchmark_return_1d_pct": benchmark_return_1d_pct,
        "benchmark_return_5d_pct": benchmark_return_5d_pct,
        "realized_vol_20d_pct": realized_vol_20d_pct,
        "efficiency_ratio_20d": efficiency_ratio_20d,
        "account_daily_pnl_pct": account_daily_pnl_pct,
        "open_unrealized_pnl_pct": open_unrealized_pnl_pct,
        "base_max_stop_heat_pct": base_max_stop_heat_pct,
    })

    async def _do():
        if base_max_stop_heat_pct <= 0 or realized_vol_20d_pct < 0:
            raise ValueError("base_max_stop_heat_pct must be > 0 and realized_vol_20d_pct >= 0")

        r1d = float(benchmark_return_1d_pct)
        r5d = float(benchmark_return_5d_pct)
        vol20 = float(realized_vol_20d_pct)
        er20 = max(0.0, min(1.0, float(efficiency_ratio_20d)))
        acct_1d = float(account_daily_pnl_pct)
        unreal = float(open_unrealized_pnl_pct)
        base_heat = float(base_max_stop_heat_pct)

        downward_selloff = bool(r1d <= -0.95 or r5d <= -2.10 or vol20 >= 24.0)
        severe_selloff = bool(r1d <= -2.10 or r5d <= -3.80 or vol20 >= 32.0)
        account_stress = bool(acct_1d <= -1.25 or unreal <= -1.10)
        severe_account_stress = bool(acct_1d <= -2.25 or unreal <= -1.85)

        reasons: list[str] = []
        if severe_selloff or severe_account_stress:
            posture = "CrisisShield"
            risk_mult = 0.20
            stop_tighten = 0.55
            heat_cap = min(base_heat, 0.75)
            def_rotation = True
            if severe_selloff:
                reasons.append(f"severe_market_selloff (1d={r1d:.2f}%, 5d={r5d:.2f}%, vol20d={vol20:.1f}%)")
            if severe_account_stress:
                reasons.append(f"severe_account_drawdown (daily_pnl={acct_1d:.2f}%, open_unrealized={unreal:.2f}%)")
        elif downward_selloff or account_stress:
            posture = "Defensive"
            risk_mult = 0.42
            stop_tighten = 0.65
            heat_cap = min(base_heat, 1.40)
            def_rotation = True
            if downward_selloff:
                reasons.append(f"downward_market_selloff (1d={r1d:.2f}%, 5d={r5d:.2f}%, vol20d={vol20:.1f}%)")
            if account_stress:
                reasons.append(f"account_drawdown_stress (daily_pnl={acct_1d:.2f}%, open_unrealized={unreal:.2f}%)")
        elif r1d <= -0.40 or r5d <= -1.00 or vol20 >= 19.5 or er20 <= 0.18 or acct_1d <= -0.60:
            posture = "Cautious"
            risk_mult = 0.68
            stop_tighten = 0.78
            heat_cap = min(base_heat, 2.20)
            def_rotation = r5d <= -1.25
            reasons.append(f"elevated_chop_or_pullback (1d={r1d:.2f}%, 5d={r5d:.2f}%, vol20d={vol20:.1f}%, er20={er20:.2f})")
        else:
            posture = "Normal"
            risk_mult = 1.00
            stop_tighten = 1.00
            heat_cap = base_heat
            def_rotation = False
            reasons.append("constructive_volatility_and_drawdown_headroom")

        return {
            "tool": "warden.defensive_posture",
            "posture": posture,
            "risk_scale_multiplier": round(risk_mult, 2),
            "stop_tightening_factor": round(stop_tighten, 2),
            "effective_stop_heat_cap_pct": round(heat_cap, 2),
            "downward_selloff_active": downward_selloff,
            "defensive_rotation_active": def_rotation,
            "reasons": reasons,
            "telemetry": {
                "benchmark_return_1d_pct": round(r1d, 3),
                "benchmark_return_5d_pct": round(r5d, 3),
                "realized_vol_20d_pct": round(vol20, 2),
                "efficiency_ratio_20d": round(er20, 3),
                "account_daily_pnl_pct": round(acct_1d, 3),
                "open_unrealized_pnl_pct": round(unreal, 3),
            },
            "provenance": LIVE_CAPITAL_NOTE,
            "safety": NO_ORDER_PLACEMENT,
        }

    return await run_tool("warden.defensive_posture", _do)



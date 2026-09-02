"""swarm-warden-mcp tool implementations — the live-capital checkers, local."""
from __future__ import annotations

import datetime as dt
import math

from swarm_mcp import vendor_path  # noqa: F401

from fill_model import net_return_after_costs, one_way_cost_bps, round_trip_cost_bps  # vendored
from gates import MIN_COVERAGE, REGIMES  # vendored
from genome_schema import CURRENT_SCHEMA_VERSION, genome_hash, validate_genome  # vendored
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
        errors = validate_genome(genome)
        if genome.get("schema_version") != CURRENT_SCHEMA_VERSION:
            errors.append(f"unsupported schema_version {genome.get('schema_version')} "
                          f"(current {CURRENT_SCHEMA_VERSION})")
        gh = genome_hash(genome)
        return {
            "tool": "warden.validate_genome",
            "valid": not errors,
            "errors": errors,
            "genome_hash": gh,
            "schema_version": genome.get("schema_version"),
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
        gh = genome_hash(challenger_genome)
        summary = ["promotion verdicts are never issued locally — the selection machinery is server-side"]
        if champion_genome is not None:
            tier = highest_mutated_tier(champion_genome, challenger_genome)
            summary.append(f"highest mutated provenance tier vs champion: {tier or 'identical'}")
            if tier in ("B", "C"):
                summary.append(f"tier-{tier} mutations need tape-fidelity evidence (hosted tape replay — roadmap)")
        out = envelope.indeterminate_local(
            genome_hash=gh,
            violation_summary=summary,
            seeds=[0, 1, 2, 3, 4],
            per_regime=4,
            reason=(f"local probes run capped seeds on the local cache window; PBO/DSR inputs exist "
                    f"only on the hosted side ({len(REGIMES)} regimes: {', '.join(REGIMES)})"),
        )
        out["tool"] = "warden.promotion_verdict"
        out["regimes_required"] = REGIMES
        out["safety"] = NO_ORDER_PLACEMENT
        return out

    return await run_tool("warden.promotion_verdict", _do)

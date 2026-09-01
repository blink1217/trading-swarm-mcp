"""Pessimistic execution-cost model for the gym simulator.

Prior work in this workspace (5-min backtests) showed that fill/spread assumptions
dominate results: Corwin-Schultz half-spread overcharged SPY ~6x vs quoted spreads, and
optimistic limit-entry assumptions failed under adverse selection. The gym therefore
charges costs at the PESSIMISTIC end: spread + slippage + adverse selection at both MOO
entry and MOC exit, always against the position.
"""
from __future__ import annotations


def one_way_cost_bps(spread_bps: float, slippage_bps: float, adverse_selection_bps: float) -> float:
    """Pessimistic one-way execution cost in bps (always paid against the position)."""
    return spread_bps + slippage_bps + adverse_selection_bps


def round_trip_cost_bps(spread_bps: float, slippage_bps: float, adverse_selection_bps: float) -> float:
    """Total pessimistic round-trip cost in bps (entry + exit)."""
    return 2.0 * one_way_cost_bps(spread_bps, slippage_bps, adverse_selection_bps)


def net_return_after_costs(gross_return_bps: float, spread_bps: float, slippage_bps: float, adverse_selection_bps: float) -> float:
    """Gross 5-day return in bps minus pessimistic round-trip costs in bps."""
    return gross_return_bps - round_trip_cost_bps(spread_bps, slippage_bps, adverse_selection_bps)


def gross_income_if_flat(notional_bps: float, spread_bps: float, slippage_bps: float, adverse_selection_bps: float) -> float:
    """Cost of churning notional (gross traded value) once, in bps of equity."""
    return notional_bps * one_way_cost_bps(spread_bps, slippage_bps, adverse_selection_bps) / 1e4

"""Deterministic weekend-gate policy head.

The genome's policy over `{HOLD, TRIM_50_PCT, FLATTEN}` for weekend holds. This is the
NO-LLM head used inside the simulator (OpenRouter is too slow/costly/non-deterministic
for rollouts), and it doubles as the coordinator's fail-closed fallback when the LLM
weekend decision is unavailable (OpenRouter outage / circuit breaker / bad JSON).

Feature inputs are all causal values available at Friday close:
- earnings proximity (days until next earnings, or -1 if unknown)
- atr_pct (daily ATR / price)
- gap_risk_pct (abs of the most recent overnight gap)
- energy_bias in [-1, 1] (advisory alt-data bias; -1 bearish .. +1 bullish)
"""
from __future__ import annotations


def weekend_conviction(gate: dict, *, earnings_days: float | None, atr_pct: float | None,
                       gap_risk_pct: float | None, energy_bias: float | None,
                       fail_closed: bool = False) -> float:
    """Risk-off-adjusted conviction in [0, 1]. 1 = strong hold, 0 = risk-off.

    With `fail_closed=True` (the coordinator's fallback path), every UNKNOWN
    feature applies a 0.55 risk-off multiplier instead of being treated as
    benign — missing data must never be an argument to hold over the weekend.
    """
    conviction = 1.0
    if earnings_days is not None and earnings_days >= 0:
        if earnings_days <= gate["max_earnings_proximity_days"]:
            conviction *= 0.35
    if atr_pct is not None and atr_pct > gate["max_atr_pct"]:
        conviction *= 0.55
    if gap_risk_pct is not None and gap_risk_pct > gate["max_gap_risk_pct"]:
        conviction *= 0.55
    if energy_bias is not None and energy_bias < gate["min_energy_bias"]:
        conviction *= 0.70
    if fail_closed:
        for v in (earnings_days, atr_pct, gap_risk_pct, energy_bias):
            if v is None:
                conviction *= 0.55
    # Smooth the product so tiny variations don't flip actions.
    smooth = gate["conviction_smooth"]
    return 1.0 / (1.0 + ((1.0 - conviction) / (max(conviction, 1e-9))) ** (1.0 / smooth))


def weekend_action(gate: dict, *, earnings_days: float | None, atr_pct: float | None,
                   gap_risk_pct: float | None, energy_bias: float | None,
                   fail_closed: bool = False) -> tuple[str, float]:
    """Return (action, target_weight_fraction) for a single open position.

    action in {HOLD, TRIM_50_PCT, FLATTEN}; target_weight_fraction is the multiple of
    the pre-weekend weight to carry into Monday (1.0 / 0.5 / 0.0).
    """
    c = weekend_conviction(gate, earnings_days=earnings_days, atr_pct=atr_pct,
                           gap_risk_pct=gap_risk_pct, energy_bias=energy_bias,
                           fail_closed=fail_closed)
    if c >= gate["trim_threshold"]:
        return "HOLD", 1.0
    if c >= gate["flatten_threshold"]:
        return "TRIM_50_PCT", 0.5
    return "FLATTEN", 0.0

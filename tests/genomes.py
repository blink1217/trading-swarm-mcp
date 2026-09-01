"""Genome fixtures (schema v2) — same shapes the swarm's gym tests use."""
from __future__ import annotations

import copy

BASELINE_V2 = {
    "schema_version": 2,
    "screen": {
        "data_tier": "A",
        "enable_momentum": True,
        "enable_mean_reversion": True,
        "enable_episodic_pivot": True,
        "momentum_quantile": 0.60,
        "reversion_quantile": 0.70,
        "vol_spike_ratio": 1.5,
        "breakout_dist_min": -0.02,
        "min_cross_section": 5,
    },
    "analyst": {"data_tier": "A", "lgbm_threshold": 0.45, "max_candidates": 50},
    "risk": {"data_tier": "A", "atr_period": 14, "atr_sizing_mult": 1.0,
             "risk_pct_per_trade": 0.008, "hold_days": 5},
    "execution": {"data_tier": "A", "exit_on_moc": True, "slippage_bps": 3.0,
                  "spread_bps": 1.0, "adverse_selection_bps": 8.0},
    "weekend_gate": {
        "data_tier": "C",
        "max_earnings_proximity_days": 3.0,
        "max_atr_pct": 0.05,
        "max_gap_risk_pct": 0.03,
        "min_energy_bias": -1.0,
        "conviction_smooth": 1.0,
        "trim_threshold": 0.50,
        "flatten_threshold": 0.25,
    },
    "prompt_variant_id": "default_v1",
}


def baseline() -> dict:
    return copy.deepcopy(BASELINE_V2)


def mutate_a(base: dict | None = None) -> dict:
    g = copy.deepcopy(base) if base is not None else baseline()
    g["screen"]["momentum_quantile"] = 0.80
    g["risk"]["atr_sizing_mult"] = 1.2
    return g


def mutate_b(base: dict | None = None) -> dict:
    g = copy.deepcopy(base) if base is not None else baseline()
    g["weekend_gate"]["max_earnings_proximity_days"] = 5.0
    return g


def mutate_c(base: dict | None = None) -> dict:
    g = copy.deepcopy(base) if base is not None else baseline()
    g["weekend_gate"]["min_energy_bias"] = 0.2
    return g


def mutate_prompt(base: dict | None = None) -> dict:
    g = copy.deepcopy(base) if base is not None else baseline()
    g["prompt_variant_id"] = "experiment_v2"
    return g

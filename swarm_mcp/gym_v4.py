"""House v4 Episode Simulator (`house_v4`, `evaluated_schema_version = 4`).

Upgrades the gym and Shadow Tournament replay from `house_v3` (fixed 5-day hold,
hard-wired `HOLD` weekend gate) so every Tier-A `risk.hold_days` (`1..10`) and
`weekend_gate.*` gene actively governs simulated outcomes — without mutating
any pinned file inside `swarm_mcp/vendored/` (`vendored-pin-check` invariant).

Also defines the exhaustive gene-coverage partition over `genome_schema.json`
and refuses `NO_SCORED_CHANGE` genomes that differ from the champion solely in
unscored metadata / fixed-cost keys (`execution.*`, `risk.atr_period`,
`schema_version`, `*.data_tier`).
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from swarm_mcp import vendor_path  # noqa: F401

from fill_model import house_round_trip_cost_bps  # vendored guardrails
from gym.panel import assert_no_lookahead, decision_features, prepare_panel  # vendored alpha
from gym.policy_head import weekend_action  # vendored alpha
from gym.simulator import (  # vendored alpha
    FIDELITY_LABEL,
    STATE_FEATURES,
    TierScoringRefusal,
    _analyst_score,
    _episode_summary,
    _screen_mask,
    _size_position,
    assert_tier_a_scortable,
)
from objective import MAX_GROSS_EXPOSURE_PCT  # vendored guardrails

HOUSE_COST_MODEL = "house_v4"
EVALUATED_SCHEMA_VERSION = 4

SCORED_TIER_A_GENES: frozenset[str] = frozenset({
    "screen.enable_momentum",
    "screen.enable_mean_reversion",
    "screen.enable_episodic_pivot",
    "screen.momentum_quantile",
    "screen.reversion_quantile",
    "screen.vol_spike_ratio",
    "screen.breakout_dist_min",
    "screen.min_cross_section",
    "analyst.lgbm_threshold",
    "analyst.max_candidates",
    "risk.atr_sizing_mult",
    "risk.risk_pct_per_trade",
    "risk.hold_days",
    "weekend_gate.max_atr_pct",
    "weekend_gate.max_gap_risk_pct",
    "weekend_gate.conviction_smooth",
    "weekend_gate.trim_threshold",
    "weekend_gate.flatten_threshold",
})

TIER_GATED_GENES: frozenset[str] = frozenset({
    "weekend_gate.max_earnings_proximity_days",
    "weekend_gate.min_energy_bias",
    "prompt_variant_id",
})

UNSCORED_GENES: frozenset[str] = frozenset({
    "schema_version",
    "screen.data_tier",
    "analyst.data_tier",
    "risk.data_tier",
    "risk.atr_period",
    "execution.data_tier",
    "execution.exit_on_moc",
    "execution.slippage_bps",
    "execution.spread_bps",
    "execution.adverse_selection_bps",
    "weekend_gate.data_tier",
})


class NoScoredChangeError(ValueError):
    """Challenger differs from the champion solely in unscored settings."""


def _flatten_scalars(obj: dict, prefix: str = "", out: dict[str, object] | None = None) -> dict[str, object]:
    if out is None:
        out = {}
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            _flatten_scalars(v, path, out)
        else:
            out[path] = v
    return out


def changed_gene_paths(challenger: dict, champion: dict) -> list[str]:
    a = _flatten_scalars(challenger)
    b = _flatten_scalars(champion)
    all_keys = sorted(set(a) | set(b))
    return [k for k in all_keys if a.get(k) != b.get(k)]


def assert_has_scored_change(challenger: dict, champion: dict) -> None:
    diffs = changed_gene_paths(challenger, champion)
    if diffs and all(d in UNSCORED_GENES for d in diffs):
        raise NoScoredChangeError(
            f"NO_SCORED_CHANGE: genome differs from the champion only in unscored settings {diffs}"
        )


def schema_leaf_gene_paths() -> set[str]:
    schema_path = os.path.join(
        os.path.dirname(__file__), "vendored", "guardrails", "genome_schema.json"
    )
    with open(schema_path, encoding="utf-8") as fh:
        schema = json.load(fh)
    leaves: set[str] = set()

    def _walk(props: dict, prefix: str = "") -> None:
        for name, spec in props.items():
            path = f"{prefix}.{name}" if prefix else name
            if isinstance(spec, dict) and spec.get("type") == "object" and isinstance(spec.get("properties"), dict):
                _walk(spec["properties"], path)
            else:
                leaves.add(path)

    _walk(schema.get("properties", {}))
    return leaves


def prepare_panel_v4(bars: pd.DataFrame) -> pd.DataFrame:
    """Prepare causal decision features + evaluation-only `fwd_ret_{1..10}d` columns."""
    panel = prepare_panel(bars)
    if panel.empty:
        return panel
    panel = panel.sort_values(["symbol", "ts"]).reset_index(drop=True)
    grouped = panel.groupby("symbol", sort=False)
    for h in range(1, 11):
        col = f"fwd_ret_{h}d"
        if col not in panel.columns:
            panel[col] = grouped["close"].shift(-h) / panel["entry_open_next"] - 1.0
    return panel


def _eval_forward_return_next_open(panel: pd.DataFrame, symbol: str, decision_date, hold_days: int) -> float:
    """Evaluation-only next-open-to-close[t + hold_days] return (never exposed to decision_features)."""
    col = f"fwd_ret_{hold_days}d"
    t = pd.Timestamp(decision_date)
    sym_rows = panel[panel["symbol"] == symbol].sort_values("ts").reset_index(drop=True)
    matches = sym_rows.index[sym_rows["ts"] == t].tolist()
    if not matches:
        return 0.0
    idx = matches[-1]
    if col in sym_rows.columns:
        val = sym_rows.at[idx, col]
        return float(val) if val is not None and not np.isnan(float(val)) else 0.0
    if idx + 1 >= len(sym_rows) or idx + hold_days >= len(sym_rows):
        return 0.0
    entry_open = float(sym_rows.at[idx + 1, "open"])
    exit_close = float(sym_rows.at[idx + hold_days, "close"])
    if entry_open <= 0.0 or np.isnan(entry_open) or np.isnan(exit_close):
        return 0.0
    return exit_close / entry_open - 1.0


def replay_episode_v4(panel: pd.DataFrame, episode: dict, genome: dict) -> dict:
    cost_bps = house_round_trip_cost_bps()
    hold_days = int(np.clip(int(genome["risk"].get("hold_days", 5)), 1, 10))

    mask = _screen_mask(panel, episode, genome)
    candidates = list(mask[mask].index) if len(mask) else []
    if not candidates:
        return _episode_summary(episode, [], 0.0, 0.0, 0.0, 0.0, [], [])

    scored = []
    for s in candidates:
        f = decision_features(panel, s, episode["date"])
        if f is None:
            continue
        proba, passed = _analyst_score(dict(f), genome)
        if passed:
            scored.append((s, proba, f))

    scored.sort(key=lambda t: -t[1])
    scored = scored[: genome["analyst"]["max_candidates"]]

    positions = []
    lookahead = []
    gross = 0.0
    for s, proba, f in scored:
        v = assert_no_lookahead(panel, s, episode["date"], f)
        if v:
            lookahead.extend(v)
            continue
        size_pct = _size_position(genome, float(f["atr_14"]), float(f["close"]))
        if size_pct <= 0:
            continue
        if gross >= MAX_GROSS_EXPOSURE_PCT:
            break
        capped_size = min(size_pct, MAX_GROSS_EXPOSURE_PCT - gross)

        atr_pct_val = None if np.isnan(float(f["atr_pct"])) else float(f["atr_pct"])
        gap_risk_val = None if np.isnan(float(f["gap_open"])) else abs(float(f["gap_open"]))
        action, target_frac = weekend_action(
            genome["weekend_gate"],
            earnings_days=None,
            atr_pct=atr_pct_val,
            gap_risk_pct=gap_risk_val,
            energy_bias=None,
            fail_closed=False,
        )
        final_pct = capped_size * target_frac
        gross += final_pct

        fwd = _eval_forward_return_next_open(panel, s, episode["date"], hold_days)
        net_ret = (fwd * 1e4 - cost_bps) if final_pct > 0.0 else 0.0
        positions.append({
            "symbol": s,
            "size_pct": capped_size,
            "target_frac": target_frac,
            "final_pct": final_pct,
            "hold_days": hold_days,
            "gross_ret_bps": fwd * 1e4 if final_pct > 0.0 else 0.0,
            "net_ret_bps": net_ret,
            "proba": proba,
            "action": action,
            "moves": {
                "screen": "PASS",
                "entry": "ENTER" if final_pct > 0.0 else "SKIPPED_WEEKEND",
                "size": "CAPPED" if capped_size < size_pct else "FULL",
                "weekend": action,
            },
            "state": {k: (None if np.isnan(float(f[k])) else float(f[k])) for k in STATE_FEATURES if k in f},
        })

    gross_total = sum(p["final_pct"] for p in positions)
    max_pos = max((p["final_pct"] for p in positions), default=0.0)
    turnover = sum(p["final_pct"] for p in positions)
    weekly_net_bps = sum(p["final_pct"] * p["net_ret_bps"] for p in positions)
    return _episode_summary(episode, positions, weekly_net_bps, turnover, gross_total, max_pos, lookahead, [])


def evaluate_genome_v4(
    panel: pd.DataFrame,
    episodes_by_seed: dict[int, list[dict]],
    genome: dict,
    seeds: list[int] | None = None,
    champion_genome: dict | None = None,
) -> dict:
    assert_tier_a_scortable(genome, champion_genome)
    seeds = seeds or list(episodes_by_seed.keys())
    all_eps: list[dict] = []
    per_seed = {}
    for s in seeds:
        eps = [replay_episode_v4(panel, e, genome) for e in episodes_by_seed[s]]
        for e in eps:
            e["seed"] = int(s)
        per_seed[s] = eps
        all_eps.extend(eps)
    weekly = [e["weekly_net_bps"] for e in all_eps if e["lookahead_violations"] == []]
    lookahead = [v for e in all_eps for v in e["lookahead_violations"]]
    return {
        "fidelity": FIDELITY_LABEL,
        "cost_model": HOUSE_COST_MODEL,
        "evaluated_schema_version": EVALUATED_SCHEMA_VERSION,
        "n_episodes": len(weekly),
        "weekly_net_bps": weekly,
        "per_seed": per_seed,
        "mean_weekly_bps": float(np.mean(weekly)) if weekly else None,
        "turnover_pct_avg": float(np.mean([e["turnover_pct"] for e in all_eps])) if all_eps else None,
        "max_gross_exposure": max((e["max_gross_exposure_pct"] for e in all_eps), default=0.0),
        "max_position_pct": max((e["max_position_pct"] for e in all_eps), default=0.0),
        "lookahead_violations": lookahead[:20],
        "episodes": all_eps,
    }

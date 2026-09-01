"""swarm-gym-mcp tool implementations — regime fragility probing.

Structural honesty: seed caps, tier refusal, underpowered labels, and a hard
rule that promotion verdicts are never issued locally.
"""
from __future__ import annotations

import math
import time

import numpy as np
import pandas as pd

from swarm_mcp import vendor_path  # noqa: F401

from gates import MAX_PBO, MIN_DSR_MARGIN, MIN_EPISODES, REGIMES  # vendored
from genome_schema import genome_hash, validate_genome  # vendored
from gym.regime import (  # vendored alpha
    episode_pool,
    episode_seed_matrix,
    label_all_regimes,
)
from gym.simulator import TierScoringRefusal, evaluate_genome  # vendored alpha
from objective import hard_constraint_violations  # vendored guardrails

from swarm_mcp import envelope, redaction, telemetry
from swarm_mcp.cache.db import get_db

MAX_LOCAL_SEEDS = 8
MAX_LOCAL_PER_REGIME = 2
SECONDS_PER_EPISODE_EST = 0.5

LOCAL_POWER_NOTE = ("local compute is capped for statistical honesty — the hosted tournament runs "
                    "uncapped seeds on the full bars_1day panel with the monotonic trial ledger")


async def _run(tool: str, fn):
    t0 = time.perf_counter()
    try:
        out = await fn()
        telemetry.record(tool, True, (time.perf_counter() - t0) * 1000.0)
        return redaction.redact(out)
    except Exception as e:
        telemetry.record(tool, False, (time.perf_counter() - t0) * 1000.0)
        return {"tool": tool, "error": redaction.redact_text(f"{type(e).__name__}: {e}")}


def _panel_from_bars(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).dt.normalize()
    df = df.drop_duplicates(subset=["symbol", "ts"], keep="last")
    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
    from gym.panel import prepare_panel  # vendored alpha

    return prepare_panel(df)


def _load_panel(symbols: list[str] | None, bars: list[dict] | None) -> pd.DataFrame:
    if bars:
        panel = _panel_from_bars(bars)
    else:
        if not symbols:
            raise ValueError("provide symbols (served from the shared cache) or inline bars rows")
        db = get_db()
        symbols = sorted({s.strip().upper() for s in symbols if s and s.strip()})
        rows = db.get_bars("alpaca", symbols)
        if not rows:
            raise ValueError(
                f"no cached bars for {symbols} — warm the cache with swarm-data-mcp cache_warm first "
                "(the gym reads the shared cache and never places API calls itself)")
        panel = _panel_from_bars(rows)
    if panel.empty:
        raise ValueError("panel is empty after preparation (each symbol needs >= 30 sessions)")
    return panel


def _check_caps(seeds: list[int], per_regime: int) -> None:
    if len(seeds) > MAX_LOCAL_SEEDS:
        raise ValueError(
            f"seed cap: local probes accept at most {MAX_LOCAL_SEEDS} seeds (statistical honesty, not "
            f"scarcity — more seeds would imply power this machine cannot deliver); request the full "
            "run via the cloud_job block")
    if per_regime > MAX_LOCAL_PER_REGIME or per_regime < 1:
        raise ValueError(f"per_regime must be in [1, {MAX_LOCAL_PER_REGIME}] locally")


def _regime_scores(episodes: list[dict]) -> dict[str, float]:
    by_regime: dict[str, list[float]] = {r: [] for r in REGIMES}
    for ep in episodes:
        by_regime.setdefault(ep.get("regime", "chop"), []).append(ep["weekly_net_bps"])
    return {r: (float(np.mean(v)) if v else 0.0) for r, v in by_regime.items()}


def _wilcoxon_paired(deltas: list[float]) -> float | None:
    if len(deltas) < 5:
        return None
    arr = np.array(deltas)
    arr = arr[np.isfinite(arr)]
    if len(arr) < 5 or np.all(arr == 0):
        return 1.0
    try:
        from scipy.stats import wilcoxon

        return float(wilcoxon(arr).pvalue)
    except Exception:
        return None


def _bootstrap_ci(deltas: list[float], n_boot: int = 1000, ci: float = 0.95) -> tuple[float, float]:
    if not deltas:
        return (0.0, 0.0)
    arr = np.array(deltas)
    rng = np.random.default_rng(42)
    means = [float(np.mean(rng.choice(arr, size=len(arr), replace=True))) for _ in range(n_boot)]
    lo = float(np.percentile(means, (1 - ci) / 2 * 100))
    hi = float(np.percentile(means, (1 + ci) / 2 * 100))
    return lo, hi


def _depth_meta(panel: pd.DataFrame) -> dict:
    lo = panel["ts"].min().date()
    hi = panel["ts"].max().date()
    weeks = envelope.depth_weeks(lo, hi)
    return {"oldest_session": str(lo), "newest_session": str(hi), **envelope.limits_block(weeks)}


async def label_regimes(symbols: list[str] | None = None, bars: list[dict] | None = None,
                        min_symbols: int = 8) -> dict:
    redaction.reject_keylike_args({"symbols": symbols, "bars": bars, "min_symbols": min_symbols})

    async def _do():
        panel = _load_panel(symbols, bars)
        labels = label_all_regimes(panel)
        pool = episode_pool(panel, labels, min_symbols=min_symbols)
        counts = {r: 0 for r in REGIMES}
        for ep in pool:
            counts[ep["regime"]] = counts.get(ep["regime"], 0) + 1
        thin = sorted(r for r, n in counts.items() if n < 3)
        out = {
            "tool": "label_regimes",
            "regimes": REGIMES,
            "regime_counts": counts,
            "pool_size": len(pool),
            "thin_pools": thin,
            "thin_pool_warning": (f"regimes {thin} have < 3 episodes — any per-regime statistic on them "
                                  "is anecdotal" if thin else None),
            "labels_are_causal": "regime labels use trailing data only (no lookahead)",
            "depth": _depth_meta(panel),
        }
        if not out["depth"]["meets_tape_eligible"]:
            out["escalation"] = envelope.escalation_block(
                [f"panel depth {out['depth']['local_depth_weeks']} weeks is below the tape-eligible gate"])
        return out

    return await _run("label_regimes", _do)


async def probe_fragility(genome: dict, champion_genome: dict | None = None,
                          seeds: list[int] | None = None, per_regime: int = 2,
                          symbols: list[str] | None = None, bars: list[dict] | None = None,
                          min_symbols: int = 8) -> dict:
    redaction.reject_keylike_args({"genome": genome, "champion_genome": champion_genome,
                                   "seeds": seeds, "per_regime": per_regime,
                                   "symbols": symbols, "bars": bars, "min_symbols": min_symbols})

    async def _do():
        errors = validate_genome(genome)
        gh = genome_hash(genome)
        if errors:
            return {"tool": "probe_fragility", "valid_genome": False, "errors": errors,
                    "genome_hash": gh}
        use_seeds = list(seeds) if seeds else [0, 1]
        _check_caps(use_seeds, per_regime)

        panel = _load_panel(symbols, bars)
        labels = label_all_regimes(panel)
        pool = episode_pool(panel, labels, min_symbols=min_symbols)
        matrix = episode_seed_matrix(pool, use_seeds, per_regime)

        try:
            run = evaluate_genome(panel, matrix, genome, use_seeds, champion_genome=champion_genome)
        except TierScoringRefusal as e:
            out = envelope.indeterminate_local(
                genome_hash=gh,
                violation_summary=[str(e)],
                seeds=use_seeds,
                per_regime=per_regime,
                reason="tier-B/C mutations need hosted tape-fidelity replay (roadmap); never neutral-filled",
            )
            out["tool"] = "probe_fragility"
            out["verdict"] = "UNSCORABLE"
            out["fidelity_note"] = "the gym is the tier-A price subset; refusal is the correct verdict"
            return out

        weekly = run["weekly_net_bps"]
        per_ep = [{"max_gross_exposure_pct": e["max_gross_exposure_pct"],
                   "max_position_pct": e["max_position_pct"]} for e in run["episodes"]]
        violations = hard_constraint_violations(weekly, per_ep)
        regime_means = _regime_scores(run["episodes"])
        worst = min(regime_means, key=regime_means.get) if any(regime_means.values()) else None

        n = run["n_episodes"]
        out = {
            "tool": "probe_fragility",
            "verdict": "FRAGILITY_REPORT — local tools never issue promotion verdicts",
            "genome_hash": gh,
            "fidelity": run["fidelity"],
            "n_episodes": n,
            "seeds": use_seeds,
            "per_regime": per_regime,
            "mean_weekly_bps": run["mean_weekly_bps"],
            "per_regime_bps": regime_means,
            "worst_regime": worst,
            "worst_regime_bps": regime_means.get(worst) if worst else None,
            "turnover_pct_avg": run["turnover_pct_avg"],
            "max_gross_exposure": run["max_gross_exposure"],
            "max_position_pct": run["max_position_pct"],
            "hard_constraint_violations": violations,
            "lookahead_violations": run["lookahead_violations"],
            "underpowered": n < MIN_EPISODES,
            "power_note": (f"n={n} < MIN_EPISODES={MIN_EPISODES}: no conclusion about promotion is "
                           "possible from this sample — by design" if n < MIN_EPISODES else
                           "episode count meets MIN_EPISODES, but PBO/DSR inputs still require the hosted side"),
            "depth": _depth_meta(panel),
            "cloud_job": envelope.cloud_job_block(
                gh, use_seeds, per_regime,
                reason=LOCAL_POWER_NOTE),
        }
        return out

    return await _run("probe_fragility", _do)


async def paired_preview(champion_genome: dict, challenger_genome: dict,
                         seeds: list[int] | None = None, per_regime: int = 2,
                         symbols: list[str] | None = None, bars: list[dict] | None = None,
                         min_symbols: int = 8) -> dict:
    redaction.reject_keylike_args({"champion_genome": champion_genome,
                                   "challenger_genome": challenger_genome,
                                   "seeds": seeds, "per_regime": per_regime,
                                   "symbols": symbols, "bars": bars, "min_symbols": min_symbols})

    async def _do():
        errors = validate_genome(champion_genome) + validate_genome(challenger_genome)
        champ_h = genome_hash(champion_genome)
        chal_h = genome_hash(challenger_genome)
        if errors:
            return {"tool": "paired_preview", "valid_genomes": False, "errors": errors,
                    "champion_hash": champ_h, "challenger_hash": chal_h}
        use_seeds = list(seeds) if seeds else [0, 1]
        _check_caps(use_seeds, per_regime)

        panel = _load_panel(symbols, bars)
        labels = label_all_regimes(panel)
        pool = episode_pool(panel, labels, min_symbols=min_symbols)
        matrix = episode_seed_matrix(pool, use_seeds, per_regime)

        try:
            champ_run = evaluate_genome(panel, matrix, champion_genome, use_seeds)
            chal_run = evaluate_genome(panel, matrix, challenger_genome, use_seeds,
                                       champion_genome=champion_genome)
        except TierScoringRefusal as e:
            out = envelope.indeterminate_local(
                genome_hash=chal_h,
                violation_summary=[str(e)],
                seeds=use_seeds,
                per_regime=per_regime,
                reason="tier-B/C challenger mutations need hosted tape-fidelity replay (roadmap)",
            )
            out["tool"] = "paired_preview"
            out["verdict"] = "UNSCORABLE"
            return out

        champ_eps = {e["date"]: e for e in champ_run["episodes"]}
        chal_eps = {e["date"]: e for e in chal_run["episodes"]}
        common = sorted(set(champ_eps) & set(chal_eps))
        deltas = [chal_eps[d]["weekly_net_bps"] - champ_eps[d]["weekly_net_bps"] for d in common]
        p_value = _wilcoxon_paired(deltas)
        ci_lo, ci_hi = _bootstrap_ci(deltas)
        champ_regime = _regime_scores(champ_run["episodes"])
        chal_regime = _regime_scores(chal_run["episodes"])

        episodes_per_seed = per_regime * len(REGIMES)
        seeds_needed = math.ceil(MIN_EPISODES / episodes_per_seed) if episodes_per_seed else None
        underpowered_label = (f"UNDERPOWERED — n={len(common)} < MIN_EPISODES={MIN_EPISODES}; "
                              f"needs >= {seeds_needed} seeds x {per_regime}/regime on identical paths")

        return {
            "tool": "paired_preview",
            "verdict": "UNDERPOWERED_PREVIEW — promotion is NEVER issued locally",
            "promotion": None,
            "champion_hash": champ_h,
            "challenger_hash": chal_h,
            "n_paired_episodes": len(common),
            "seeds": use_seeds,
            "per_regime": per_regime,
            "statistics": {
                "mean_delta_bps": {"value": float(np.mean(deltas)) if deltas else 0.0,
                                   "power": underpowered_label},
                "paired_p_value": {"value": p_value, "power": underpowered_label},
                "delta_ci_95": {"value": [ci_lo, ci_hi], "power": underpowered_label},
                "worst_regime_champion": {"value": min(champ_regime.values()), "power": underpowered_label},
                "worst_regime_challenger": {"value": min(chal_regime.values()), "power": underpowered_label},
                "per_regime_champion": champ_regime,
                "per_regime_challenger": chal_regime,
            },
            "gate_requirements": {
                "min_episodes": f"MIN_EPISODES={MIN_EPISODES}; reach with >= {seeds_needed} local seeds "
                                "(above the seed cap) or on the hosted side",
                "pbo": f"MAX_PBO={MAX_PBO} requires combinatorially-symmetric CV splits (CSCV) over the "
                       "full history — hosted compute only",
                "dsr_margin": f"MIN_DSR_MARGIN={MIN_DSR_MARGIN} requires the deflated-Sharpe estimator "
                              "with the monotonic n_trials ledger — server-side by design",
            },
            "identical_paths": "both genomes replayed the same fixed episode-seed matrix (paired by construction)",
            "depth": _depth_meta(panel),
            "cloud_job": envelope.cloud_job_block(
                chal_h, list(range(seeds_needed)) if seeds_needed else use_seeds, per_regime,
                reason=LOCAL_POWER_NOTE),
        }

    return await _run("paired_preview", _do)


async def estimate_cloud_run(seeds: list[int] | None = None, per_regime: int = 4,
                             n_genomes: int = 1) -> dict:
    redaction.reject_keylike_args({"seeds": seeds, "per_regime": per_regime, "n_genomes": n_genomes})

    async def _do():
        use_seeds = list(seeds) if seeds else [0, 1, 2, 3, 4]
        episodes = len(REGIMES) * per_regime * len(use_seeds) * max(1, n_genomes)
        est_wall_s = episodes * SECONDS_PER_EPISODE_EST
        return {
            "tool": "estimate_cloud_run",
            "episodes_required": episodes,
            "formula": "len(REGIMES) x per_regime x seeds x genomes",
            "regimes": REGIMES,
            "wall_clock_estimate_s": round(est_wall_s, 1),
            "wall_clock_note": "order-of-magnitude only; hosted runners parallelize per genome",
            "budget": envelope.budget_reference(),
            "submit_bodies": {
                "POST /tournament/run": {
                    "challenger_id": "<genome_hash from validate_genome>",
                    "seeds": use_seeds,
                    "per_regime": per_regime,
                    "panel": "bars_1day",
                },
                "POST /cycle/run": {
                    "note": "league cycle — book via the Strategy Validation Audit for multi-genome runs",
                },
            },
            "handoff": envelope.indeterminate_local()["why"],
        }

    return await _run("estimate_cloud_run", _do)

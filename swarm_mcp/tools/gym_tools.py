"""swarm-gym-mcp tool implementations — regime fragility probing.

Structural honesty: seed caps, tier refusal, underpowered labels, and a hard
rule that promotion verdicts are never issued locally.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from swarm_mcp import vendor_path  # noqa: F401

from gates import MAX_PBO, MIN_DSR_MARGIN, MIN_EPISODES, REGIMES  # vendored
from genome_schema import genome_hash, validate_genome  # vendored

from swarm_mcp import genome_compat
from gym.regime import (  # vendored alpha
    episode_pool,
    episode_seed_matrix,
    label_all_regimes,
)
from gym.simulator import TierScoringRefusal, evaluate_genome  # vendored alpha
from objective import hard_constraint_violations  # vendored guardrails

from swarm_mcp import envelope, redaction
from swarm_mcp.cache.db import get_db
from swarm_mcp.tool_runner import run_tool

MAX_LOCAL_SEEDS = 8
MAX_LOCAL_PER_REGIME = 2
SECONDS_PER_EPISODE_EST = 0.5

LOCAL_POWER_NOTE = ("local compute is capped for statistical honesty — the hosted tournament runs "
                    "uncapped seeds on the full bars_1day panel with the monotonic trial ledger")


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
                f"no cached bars for {symbols} — warm the cache with swarm-data-mcp cache.warm first "
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


def paired_stats(champ_run: dict, chal_run: dict) -> dict:
    """Paired champion-vs-challenger statistics on the common episode dates.

    Shared by the local ``gym.paired_preview`` (capped, UNDERPOWERED) and the
    hosted Shadow Tournament runner (full geometry). Pure statistics only — no
    promotion decision lives here (that machinery is server-side by design).
    """
    champ_eps = {e["date"]: e for e in champ_run["episodes"]}
    chal_eps = {e["date"]: e for e in chal_run["episodes"]}
    common = sorted(set(champ_eps) & set(chal_eps))
    deltas = [chal_eps[d]["weekly_net_bps"] - champ_eps[d]["weekly_net_bps"] for d in common]
    champ_regime = _regime_scores(champ_run["episodes"])
    chal_regime = _regime_scores(chal_run["episodes"])
    per_regime_delta = {r: chal_regime.get(r, 0.0) - champ_regime.get(r, 0.0) for r in REGIMES}
    ci_lo, ci_hi = _bootstrap_ci(deltas)
    return {
        "n_paired_episodes": len(common),
        "deltas": deltas,
        "mean_delta_bps": float(np.mean(deltas)) if deltas else 0.0,
        "paired_p_value": _wilcoxon_paired(deltas),
        "delta_ci_95": [ci_lo, ci_hi],
        "per_regime_champion": champ_regime,
        "per_regime_challenger": chal_regime,
        "per_regime_delta_bps": per_regime_delta,
        # maximin: the challenger's margin in its WORST regime relative to the champion
        "worst_regime_margin_bps": float(min(per_regime_delta.values())) if per_regime_delta else 0.0,
    }


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
            "tool": "gym.label_regimes",
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

    return await run_tool("gym.label_regimes", _do)


async def probe_fragility(genome: dict, champion_genome: dict | None = None,
                          seeds: list[int] | None = None, per_regime: int = 2,
                          symbols: list[str] | None = None, bars: list[dict] | None = None,
                          min_symbols: int = 8) -> dict:
    redaction.reject_keylike_args({"genome": genome, "champion_genome": champion_genome,
                                   "seeds": seeds, "per_regime": per_regime,
                                   "symbols": symbols, "bars": bars, "min_symbols": min_symbols})

    async def _do():
        normalized, errors = genome_compat.normalize_for_validation(genome)
        # Tier diffs must compare EVALUATION copies: a raw-v2 challenger vs a
        # normalized-v3 champion would diff on schema_version, an undeclared
        # gene path that the tier resolver fail-closes to tier C.
        champ_eval = None
        if champion_genome is not None:
            champ_norm_genome, champ_norm_errors = genome_compat.normalize_for_validation(champion_genome)
            champ_eval = champ_norm_genome if not champ_norm_errors else champion_genome
        gh = genome_compat.genome_hash_canonical(genome)
        if not errors:
            errors = validate_genome(normalized)
        if errors:
            return {"tool": "gym.probe_fragility", "valid_genome": False, "errors": errors,
                    "genome_hash": gh}
        use_seeds = list(seeds) if seeds else [0, 1]
        _check_caps(use_seeds, per_regime)

        panel = _load_panel(symbols, bars)
        labels = label_all_regimes(panel)
        pool = episode_pool(panel, labels, min_symbols=min_symbols)
        matrix = episode_seed_matrix(pool, use_seeds, per_regime)

        try:
            run = evaluate_genome(panel, matrix, normalized, use_seeds, champion_genome=champ_eval)
        except TierScoringRefusal as e:
            out = envelope.indeterminate_local(
                genome_hash=gh,
                violation_summary=[str(e)],
                seeds=use_seeds,
                per_regime=per_regime,
                reason="tier-B/C mutations need hosted tape-fidelity replay (roadmap); never neutral-filled",
            )
            out["tool"] = "gym.probe_fragility"
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
            "tool": "gym.probe_fragility",
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

    return await run_tool("gym.probe_fragility", _do)


async def paired_preview(champion_genome: dict, challenger_genome: dict,
                         seeds: list[int] | None = None, per_regime: int = 2,
                         symbols: list[str] | None = None, bars: list[dict] | None = None,
                         min_symbols: int = 8) -> dict:
    redaction.reject_keylike_args({"champion_genome": champion_genome,
                                   "challenger_genome": challenger_genome,
                                   "seeds": seeds, "per_regime": per_regime,
                                   "symbols": symbols, "bars": bars, "min_symbols": min_symbols})

    async def _do():
        champ_norm, champ_errors = genome_compat.normalize_for_validation(champion_genome)
        chal_norm, chal_errors = genome_compat.normalize_for_validation(challenger_genome)
        errors = champ_errors + chal_errors
        champ_h = genome_compat.genome_hash_canonical(champion_genome)
        chal_h = genome_compat.genome_hash_canonical(challenger_genome)
        if not errors:
            errors = validate_genome(champ_norm) + validate_genome(chal_norm)
        if errors:
            return {"tool": "gym.paired_preview", "valid_genomes": False, "errors": errors,
                    "champion_hash": champ_h, "challenger_hash": chal_h}
        use_seeds = list(seeds) if seeds else [0, 1]
        _check_caps(use_seeds, per_regime)

        panel = _load_panel(symbols, bars)
        labels = label_all_regimes(panel)
        pool = episode_pool(panel, labels, min_symbols=min_symbols)
        matrix = episode_seed_matrix(pool, use_seeds, per_regime)

        try:
            champ_run = evaluate_genome(panel, matrix, champ_norm, use_seeds)
            chal_run = evaluate_genome(panel, matrix, chal_norm, use_seeds,
                                       champion_genome=champ_norm)
        except TierScoringRefusal as e:
            out = envelope.indeterminate_local(
                genome_hash=chal_h,
                violation_summary=[str(e)],
                seeds=use_seeds,
                per_regime=per_regime,
                reason="tier-B/C challenger mutations need hosted tape-fidelity replay (roadmap)",
            )
            out["tool"] = "gym.paired_preview"
            out["verdict"] = "UNSCORABLE"
            return out

        stats = paired_stats(champ_run, chal_run)
        n_common = stats["n_paired_episodes"]
        champ_regime = stats["per_regime_champion"]
        chal_regime = stats["per_regime_challenger"]

        episodes_per_seed = per_regime * len(REGIMES)
        seeds_needed = math.ceil(MIN_EPISODES / episodes_per_seed) if episodes_per_seed else None
        underpowered_label = (f"UNDERPOWERED — n={n_common} < MIN_EPISODES={MIN_EPISODES}; "
                              f"needs >= {seeds_needed} seeds x {per_regime}/regime on identical paths")

        return {
            "tool": "gym.paired_preview",
            "verdict": "UNDERPOWERED_PREVIEW — promotion is NEVER issued locally",
            "promotion": None,
            "champion_hash": champ_h,
            "challenger_hash": chal_h,
            "n_paired_episodes": n_common,
            "seeds": use_seeds,
            "per_regime": per_regime,
            "statistics": {
                "mean_delta_bps": {"value": stats["mean_delta_bps"], "power": underpowered_label},
                "paired_p_value": {"value": stats["paired_p_value"], "power": underpowered_label},
                "delta_ci_95": {"value": stats["delta_ci_95"], "power": underpowered_label},
                "worst_regime_champion": {"value": min(champ_regime.values()), "power": underpowered_label},
                "worst_regime_challenger": {"value": min(chal_regime.values()), "power": underpowered_label},
                "worst_regime_margin_bps": {"value": stats["worst_regime_margin_bps"], "power": underpowered_label},
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

    return await run_tool("gym.paired_preview", _do)


async def estimate_cloud_run(seeds: list[int] | None = None, per_regime: int = 4,
                             n_genomes: int = 1) -> dict:
    redaction.reject_keylike_args({"seeds": seeds, "per_regime": per_regime, "n_genomes": n_genomes})

    async def _do():
        from swarm_mcp import plans

        use_seeds = list(seeds) if seeds else list(plans.TOURNAMENT["seeds"])
        episodes = len(REGIMES) * per_regime * len(use_seeds) * max(1, n_genomes)
        est_wall_s = episodes * SECONDS_PER_EPISODE_EST
        return {
            "tool": "gym.estimate_cloud_run",
            "episodes_required": episodes,
            "formula": "len(REGIMES) x per_regime x seeds x genomes",
            "regimes": REGIMES,
            "wall_clock_estimate_s": round(est_wall_s, 1),
            "wall_clock_note": "order-of-magnitude only; hosted runners parallelize per genome",
            "credits": {
                "hosted_gym_episode": plans.COMPUTE_RATES["hosted.episode"],
                "this_geometry_on_hosted_gym": episodes * plans.COMPUTE_RATES["hosted.episode"],
                "shadow_tournament_full": plans.TOURNAMENT["credits_full"],
                "shadow_tournament_contribute": plans.TOURNAMENT["credits_contribute"],
                "note": ("hosted gym.probe_fragility / gym.paired_preview charge 1 credit per simulated "
                         "episode; tournament.submit is a fixed price for the full paired geometry "
                         f"({plans.TOURNAMENT['episodes']} episodes vs the live champion); local stdio "
                         "runs are never metered"),
            },
            "budget": envelope.budget_reference(),
            "submit_bodies": {
                "tournament.submit": {
                    "genome": "<the genome dict — validated locally first with warden.validate_genome>",
                    "contribute": "<true halves the price and licenses the vector to the swarm>",
                    "hosted_geometry": {
                        "seeds": plans.TOURNAMENT["seeds"],
                        "per_regime": plans.TOURNAMENT["per_regime"],
                        "panel": "bars_1day",
                    },
                },
                "league cycle": {
                    "note": "multi-genome league runs are booked via the Strategy Validation Audit",
                },
            },
            "handoff": envelope.indeterminate_local()["why"],
        }

    return await run_tool("gym.estimate_cloud_run", _do)


# ---------------------------------------------------------------------------
# Phase 3: ExpressionGraph DAG Strategy Evaluation (strategy-spec-v3)
# ---------------------------------------------------------------------------

GRAPH_OPERATOR_ABI = "graph-ops-v2"
GRAPH_MAX_NODES = 12
GRAPH_ALLOWED_METRICS = {
    "mom_20d": "mom_20d",
    "mom_5d": "mom_5d",
    "rsi_14": "rsi_14",
    "atr_pct": "atr_pct",
    "vol_ratio_20d": "vol_ratio_20",
    "vol_ratio_20": "vol_ratio_20",
    "breakout_dist_20d": "breakout_dist_20d",
}
GRAPH_WINDOW_OPS = ("mean", "std", "min", "max", "sum", "ema", "rank")
GRAPH_UNARY_OPS = ("neg", "abs", "sigmoid", "tanh", "clip01", "lag")
GRAPH_BINARY_OPS = ("add", "sub", "mul", "div", "min", "max")
GRAPH_CS_OPS = ("rank", "zscore")
GRAPH_CROWD_OPS = ("crowd_fade", "minority_game")
GRAPH_READOUT_OPS = ("weighted_sum_then_clip01", "weighted_sum_then_rank", "weighted_sum_then_sigmoid")


def _canonical_graph_hash(graph: dict) -> str:
    import hashlib
    import json

    blob = json.dumps(graph, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _validate_and_toposort_graph(graph: dict) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    if not isinstance(graph, dict):
        return [], ["graph must be a dict"]
    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return [], ["graph.nodes must be a non-empty list"]
    if len(nodes) > GRAPH_MAX_NODES:
        errors.append(f"graph has {len(nodes)} nodes; max is {GRAPH_MAX_NODES} (GRAPH_MAX_NODES)")

    by_id: dict[str, dict] = {}
    readout_count = 0
    for n in nodes:
        if not isinstance(n, dict) or not n.get("id") or not n.get("kind"):
            errors.append("every node must be a dict with 'id' and 'kind'")
            continue
        nid = str(n["id"])
        if nid in by_id:
            errors.append(f"duplicate node id: {nid}")
        by_id[nid] = n
        kind = str(n["kind"])
        if kind == "input":
            m = str(n.get("metric_id", ""))
            if m not in GRAPH_ALLOWED_METRICS:
                errors.append(f"node {nid}: unknown metric_id {m!r}; allowed: {sorted(GRAPH_ALLOWED_METRICS)}")
        elif kind == "window":
            if n.get("op") not in GRAPH_WINDOW_OPS:
                errors.append(f"node {nid}: unknown window op {n.get('op')!r}")
            w = int(n.get("window", 0) or 0)
            if w < 2 or w > 252:
                errors.append(f"node {nid}: window must be in [2, 252]")
        elif kind == "unary":
            if n.get("op") not in GRAPH_UNARY_OPS:
                errors.append(f"node {nid}: unknown unary op {n.get('op')!r}")
        elif kind == "binary":
            if n.get("op") not in GRAPH_BINARY_OPS:
                errors.append(f"node {nid}: unknown binary op {n.get('op')!r}")
            ins = n.get("inputs")
            if not isinstance(ins, list) or len(ins) != 2:
                errors.append(f"node {nid}: binary node requires exactly 2 inputs")
        elif kind == "weighted":
            ins = n.get("inputs") or []
            wts = n.get("weights") or []
            if not ins or len(ins) != len(wts):
                errors.append(f"node {nid}: weighted node requires matching non-empty inputs and weights")
            elif any(float(w) <= 0 for w in wts):
                errors.append(f"node {nid}: weighted node weights must be > 0")
        elif kind == "cross_sectional":
            if n.get("op") not in GRAPH_CS_OPS:
                errors.append(f"node {nid}: unknown cross_sectional op {n.get('op')!r}")
        elif kind == "crowd":
            if n.get("op") not in GRAPH_CROWD_OPS:
                errors.append(f"node {nid}: unknown crowd op {n.get('op')!r}")
        elif kind == "readout":
            readout_count += 1
            if n.get("op") not in GRAPH_READOUT_OPS:
                errors.append(f"node {nid}: unknown readout op {n.get('op')!r}")
            ins = n.get("inputs") or []
            wts = n.get("weights") or []
            if not ins or len(ins) != len(wts):
                errors.append(f"node {nid}: readout requires matching non-empty inputs and weights")
        else:
            errors.append(f"node {nid}: unsupported kind {kind!r}")

    if readout_count != 1:
        errors.append(f"graph must contain exactly 1 readout node (found {readout_count})")
    if errors:
        return [], errors

    def _deps(node: dict) -> list[str]:
        k = node["kind"]
        if k == "input":
            return []
        if k in ("window", "unary", "cross_sectional", "crowd"):
            return [str(node.get("input", ""))]
        return [str(x) for x in (node.get("inputs") or [])]

    for nid, node in by_id.items():
        for dep in _deps(node):
            if dep not in by_id:
                errors.append(f"node {nid} references missing input node {dep!r}")
    if errors:
        return [], errors

    visited: dict[str, int] = {}
    ordered: list[dict] = []

    def _dfs(nid: str) -> bool:
        state = visited.get(nid, 0)
        if state == 1:
            return False  # cycle
        if state == 2:
            return True
        visited[nid] = 1
        for dep in _deps(by_id[nid]):
            if not _dfs(dep):
                return False
        visited[nid] = 2
        ordered.append(by_id[nid])
        return True

    for nid in by_id:
        if visited.get(nid, 0) == 0:
            if not _dfs(nid):
                return [], ["graph contains a dependency cycle (must be a DAG)"]

    return ordered, []


def _eval_graph_on_panel(panel: pd.DataFrame, ordered_nodes: list[dict]) -> pd.DataFrame:
    """Evaluate a validated ExpressionGraph DAG causally across all (ts, symbol) rows."""
    df = panel.sort_values(["symbol", "ts"]).copy()
    node_values: dict[str, pd.Series] = {}

    for node in ordered_nodes:
        nid = str(node["id"])
        kind = str(node["kind"])
        if kind == "input":
            col = GRAPH_ALLOWED_METRICS[str(node["metric_id"])]
            direction = float(node.get("direction", 1))
            s = pd.to_numeric(df.get(col, pd.Series(0.0, index=df.index)), errors="coerce").fillna(0.0) * direction
            if col == "rsi_14":
                s = (s - 50.0) / 50.0
            node_values[nid] = s
        elif kind == "window":
            src = node_values[str(node["input"])]
            w = int(node["window"])
            op = str(node["op"])
            grp = src.groupby(df["symbol"], group_keys=False)
            if op == "mean":
                out = grp.transform(lambda x: x.rolling(w, min_periods=1).mean())
            elif op == "std":
                out = grp.transform(lambda x: x.rolling(w, min_periods=2).std().fillna(0.0))
            elif op == "min":
                out = grp.transform(lambda x: x.rolling(w, min_periods=1).min())
            elif op == "max":
                out = grp.transform(lambda x: x.rolling(w, min_periods=1).max())
            elif op == "sum":
                out = grp.transform(lambda x: x.rolling(w, min_periods=1).sum())
            elif op == "ema":
                out = grp.transform(lambda x: x.ewm(span=w, adjust=False).mean())
            else:  # rank
                out = grp.transform(lambda x: x.rolling(w, min_periods=2).apply(
                    lambda a: float(np.mean(a <= a[-1])), raw=True).fillna(0.5))
            node_values[nid] = out.fillna(0.0)
        elif kind == "unary":
            src = node_values[str(node["input"])]
            op = str(node["op"])
            if op == "neg":
                node_values[nid] = -src
            elif op == "abs":
                node_values[nid] = src.abs()
            elif op == "sigmoid":
                node_values[nid] = 1.0 / (1.0 + np.exp(-np.clip(src, -20.0, 20.0)))
            elif op == "tanh":
                node_values[nid] = np.tanh(np.clip(src, -20.0, 20.0))
            elif op == "clip01":
                node_values[nid] = np.clip(src, 0.0, 1.0)
            else:  # lag
                lag_n = max(1, int((node.get("params") or {}).get("periods", 1)))
                node_values[nid] = src.groupby(df["symbol"], group_keys=False).shift(lag_n).fillna(0.0)
        elif kind == "binary":
            a = node_values[str(node["inputs"][0])]
            b = node_values[str(node["inputs"][1])]
            op = str(node["op"])
            if op == "add":
                node_values[nid] = a + b
            elif op == "sub":
                node_values[nid] = a - b
            elif op == "mul":
                node_values[nid] = a * b
            elif op == "div":
                node_values[nid] = a / np.where(np.abs(b) < 1e-6, 1e-6, b)
            elif op == "min":
                node_values[nid] = np.minimum(a, b)
            else:
                node_values[nid] = np.maximum(a, b)
        elif kind == "weighted":
            ins = [node_values[str(i)] for i in node["inputs"]]
            wts = [float(w) for w in node["weights"]]
            tot_w = sum(abs(w) for w in wts) or 1.0
            acc = pd.Series(0.0, index=df.index)
            for s_in, w in zip(ins, wts):
                acc = acc + s_in * (w / tot_w)
            node_values[nid] = acc
        elif kind == "cross_sectional":
            src = node_values[str(node["input"])]
            op = str(node["op"])
            grp = src.groupby(df["ts"], group_keys=False)
            if op == "rank":
                node_values[nid] = grp.rank(pct=True).fillna(0.5)
            else:
                node_values[nid] = grp.transform(
                    lambda x: (x - x.mean()) / (x.std() if x.std() and x.std() > 1e-8 else 1.0)
                ).fillna(0.0)
        elif kind == "crowd":
            src = node_values[str(node["input"])]
            op = str(node["op"])
            # Counter-crowd dampening when signal exceeds 90th percentile saturation
            if op == "crowd_fade":
                node_values[nid] = src * np.exp(-0.5 * np.square(src))
            else:  # minority_game
                node_values[nid] = np.tanh(src) - 0.5 * np.power(np.tanh(src), 3)
        elif kind == "readout":
            ins = [node_values[str(i)] for i in node["inputs"]]
            wts = [float(w) for w in node["weights"]]
            tot_w = sum(abs(w) for w in wts) or 1.0
            raw = pd.Series(0.0, index=df.index)
            for s_in, w in zip(ins, wts):
                raw = raw + s_in * (w / tot_w)
            op = str(node["op"])
            if op == "weighted_sum_then_clip01":
                readout = np.clip(0.5 + 0.5 * np.tanh(raw), 0.0, 1.0)
            elif op == "weighted_sum_then_rank":
                readout = raw.groupby(df["ts"], group_keys=False).rank(pct=True).fillna(0.5)
            else:
                readout = 1.0 / (1.0 + np.exp(-np.clip(raw, -20.0, 20.0)))
            df["graph_readout"] = readout

    return df


async def evaluate_graph(
    graph: dict,
    seeds: list[int] | None = None,
    per_regime: int = 2,
    symbols: list[str] | None = None,
    bars: list[dict] | None = None,
    min_symbols: int = 8,
) -> dict:
    """Replay a declarative ExpressionGraph DAG (strategy-spec-v3) over the capped
    episode-seed matrix across the 5 causal market regimes.
    """
    redaction.reject_keylike_args({
        "graph": graph, "seeds": seeds, "per_regime": per_regime,
        "symbols": symbols, "bars": bars, "min_symbols": min_symbols,
    })

    async def _do():
        ordered_nodes, errors = _validate_and_toposort_graph(graph)
        gh = _canonical_graph_hash(graph) if isinstance(graph, dict) else ""
        if errors:
            return {
                "tool": "gym.evaluate_graph",
                "valid_graph": False,
                "errors": errors,
                "graph_hash": gh,
                "operator_abi": GRAPH_OPERATOR_ABI,
            }

        use_seeds = list(seeds) if seeds else [0, 1]
        _check_caps(use_seeds, per_regime)

        panel = _load_panel(symbols, bars)
        labels = label_all_regimes(panel)
        pool = episode_pool(panel, labels, min_symbols=min_symbols)
        matrix = episode_seed_matrix(pool, use_seeds, per_regime)

        scored_df = _eval_graph_on_panel(panel, ordered_nodes)
        top_k = max(1, min(int(graph.get("top_k", 2) or 2), 6))
        min_conviction = float(graph.get("min_readout", 0.52) or 0.52)
        round_trip_cost_bps = 24.0  # house_v3 pessimistic fill model

        # Precompute 5-session forward gross return per (symbol, ts) for episode scoring
        scored_df["fwd_5d_close"] = scored_df.groupby("symbol")["close"].shift(-5)
        scored_df["fwd_5d_bps"] = ((scored_df["fwd_5d_close"] - scored_df["close"]) / scored_df["close"]) * 10000.0

        episodes_out: list[dict] = []
        prev_selected: set[str] = set()
        turnovers: list[float] = []

        for seed_val, ep_list in matrix.items():
            for ep in ep_list:
                ep_ts = pd.Timestamp(ep["date"])
                slice_df = scored_df[scored_df["ts"] == ep_ts].dropna(subset=["graph_readout", "fwd_5d_bps"])
                if slice_df.empty:
                    continue
                # Deterministic seed tie-breaker jitter (1e-6) + sub-universe sampling
                rng = np.random.default_rng(int(seed_val) + 1000)
                keep_n = max(top_k, int(math.ceil(0.85 * len(slice_df))))
                chosen_idx = rng.choice(slice_df.index.to_numpy(), size=min(len(slice_df), keep_n), replace=False)
                sub = slice_df.loc[chosen_idx].sort_values("graph_readout", ascending=False)
                active = sub[sub["graph_readout"] >= min_conviction].head(top_k)

                cur_syms = set(active["symbol"].tolist())
                if not prev_selected and not cur_syms:
                    turnover = 0.0
                else:
                    union_n = max(1, len(prev_selected | cur_syms))
                    turnover = len(prev_selected ^ cur_syms) / union_n * 100.0
                turnovers.append(turnover)
                prev_selected = cur_syms

                if active.empty:
                    net_bps = 0.0
                else:
                    gross_bps = float(active["fwd_5d_bps"].mean())
                    net_bps = gross_bps - round_trip_cost_bps

                episodes_out.append({
                    "date": str(ep_ts.date()),
                    "regime": ep["regime"],
                    "seed": int(seed_val),
                    "weekly_net_bps": round(net_bps, 2),
                    "selected_count": int(len(active)),
                })

        regime_means = _regime_scores(episodes_out)
        worst = min(regime_means, key=regime_means.get) if any(regime_means.values()) else None
        n = len(episodes_out)
        mean_bps = round(float(np.mean([e["weekly_net_bps"] for e in episodes_out])), 2) if episodes_out else 0.0
        avg_to = round(float(np.mean(turnovers)), 2) if turnovers else 0.0

        return {
            "tool": "gym.evaluate_graph",
            "verdict": "GRAPH_FRAGILITY_REPORT — local tools never issue promotion verdicts",
            "valid_graph": True,
            "errors": [],
            "graph_hash": gh,
            "operator_abi": GRAPH_OPERATOR_ABI,
            "node_count": len(ordered_nodes),
            "n_episodes": n,
            "seeds": use_seeds,
            "per_regime": per_regime,
            "mean_weekly_bps": mean_bps,
            "per_regime_bps": {k: round(v, 2) for k, v in regime_means.items()},
            "worst_regime": worst,
            "worst_regime_bps": round(regime_means[worst], 2) if worst else None,
            "turnover_pct_avg": avg_to,
            "underpowered": n < MIN_EPISODES,
            "power_note": (
                f"n={n} < MIN_EPISODES={MIN_EPISODES}: no conclusion about promotion is "
                "possible from this sample — by design"
                if n < MIN_EPISODES else
                "episode count meets MIN_EPISODES, but PBO/DSR inputs still require the hosted side"
            ),
            "depth": _depth_meta(panel),
            "cloud_job": envelope.cloud_job_block(gh, use_seeds, per_regime, reason=LOCAL_POWER_NOTE),
        }

    return await run_tool("gym.evaluate_graph", _do)


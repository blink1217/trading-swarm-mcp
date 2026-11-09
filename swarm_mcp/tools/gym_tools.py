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


async def prop_challenge_sim(
    trades_r: list[float] | None = None,
    win_rate: float = 0.52,
    avg_win_r: float = 1.85,
    avg_loss_r: float = 1.00,
    trades_per_day: int = 2,
    risk_pct_per_trade: float = 0.0075,
    initial_balance: float = 100_000.0,
    format_type: str = "TwoStep",
    daily_loss_limit_pct: float = 0.05,
    max_loss_limit_pct: float = 0.10,
    profit_target_pct: float = 0.08,
    consistency_cap_pct: float = 0.40,
    max_trading_days: int = 30,
    challenge_fee_usd: float = 500.0,
    payout_split_pct: float = 0.80,
    n_paths: int = 250,
    seed: int = 42,
) -> dict:
    """Monte Carlo prop-firm challenge survival and optimal risk-sizing simulator.

    Replays caller-supplied trade R-multiples (`trades_r`) or parametric win/loss
    payoffs across `n_paths` multi-day prop evaluations enforcing intraday/daily
    loss limits, overall drawdown floors, and single-day consistency rules.
    Pure statistical simulation on user-supplied trade distributions — no proprietary
    signals or alpha equations.
    """
    redaction.reject_keylike_args({
        "trades_r": trades_r,
        "win_rate": win_rate,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "trades_per_day": trades_per_day,
        "risk_pct_per_trade": risk_pct_per_trade,
        "initial_balance": initial_balance,
        "format_type": format_type,
    })

    async def _do():
        if initial_balance <= 0:
            raise ValueError("initial_balance must be > 0")
        fmt = str(format_type or "TwoStep").strip()
        if fmt not in ("TwoStep", "OneStep"):
            raise ValueError("format_type must be 'TwoStep' or 'OneStep'")
        if not (0.0005 <= risk_pct_per_trade <= 0.05):
            raise ValueError("risk_pct_per_trade must be in [0.0005, 0.05]")
        if not (1 <= n_paths <= 500):
            raise ValueError("n_paths must be in [1, 500]")
        if not (1 <= trades_per_day <= 20):
            raise ValueError("trades_per_day must be in [1, 20]")
        if not (1 <= max_trading_days <= 90):
            raise ValueError("max_trading_days must be in [1, 90]")

        eff_daily = float(daily_loss_limit_pct) if daily_loss_limit_pct > 0 else (0.04 if fmt == "OneStep" else 0.05)
        eff_max = float(max_loss_limit_pct) if max_loss_limit_pct > 0 else (0.06 if fmt == "OneStep" else 0.10)
        eff_target = float(profit_target_pct) if profit_target_pct > 0 else (0.10 if fmt == "OneStep" else 0.08)
        eff_cons = max(0.15, min(1.0, float(consistency_cap_pct)))

        clean_r: np.ndarray | None = None
        if trades_r is not None:
            if len(trades_r) < 5:
                raise ValueError("trades_r must contain at least 5 trade R-multiples when supplied")
            arr = np.array([float(x) for x in trades_r], dtype=float)
            arr = arr[np.isfinite(arr)]
            if len(arr) < 5:
                raise ValueError("trades_r must contain at least 5 finite R-multiples")
            clean_r = np.clip(arr, -5.0, 15.0)
        else:
            if not (0.01 <= win_rate <= 0.99):
                raise ValueError("win_rate must be in [0.01, 0.99]")
            if avg_win_r <= 0 or avg_loss_r <= 0:
                raise ValueError("avg_win_r and avg_loss_r must be > 0")

        def _sim_risk_level(r_pct: float, rng_seed: int) -> dict:
            rng = np.random.default_rng(rng_seed)
            n_total = n_paths * max_trading_days * trades_per_day
            if clean_r is not None:
                draws = rng.choice(clean_r, size=(n_paths, max_trading_days, trades_per_day), replace=True)
            else:
                wins = rng.random((n_paths, max_trading_days, trades_per_day)) < float(win_rate)
                # Add modest payoff dispersion around avg_win_r / avg_loss_r
                noise = rng.normal(1.0, 0.15, size=(n_paths, max_trading_days, trades_per_day))
                noise = np.clip(noise, 0.5, 1.8)
                draws = np.where(wins, float(avg_win_r) * noise, -float(avg_loss_r) * noise)

            passed = 0
            daily_breach = 0
            overall_breach = 0
            consistency_locked = 0
            days_to_pass: list[int] = []

            target_profit = initial_balance * eff_target
            overall_floor = initial_balance * (1.0 - eff_max)

            for p in range(n_paths):
                eq = float(initial_balance)
                max_day_pnl = 0.0
                outcome = "timeout"
                for d in range(max_trading_days):
                    day_start_eq = eq
                    daily_ref = max(float(initial_balance), day_start_eq) if fmt == "OneStep" else day_start_eq
                    daily_floor = daily_ref * (1.0 - eff_daily)
                    # Apply 0.55x target-lock taper when >= 70% of target is reached
                    prog = (eq - initial_balance) / target_profit if target_profit > 0 else 0.0
                    eff_r_pct = r_pct * 0.55 if prog >= 0.70 else r_pct

                    for t in range(trades_per_day):
                        trade_pnl = day_start_eq * eff_r_pct * float(draws[p, d, t])
                        eq += trade_pnl
                        if eq <= daily_floor:
                            outcome = "daily_breach"
                            break
                        if eq <= overall_floor:
                            outcome = "overall_breach"
                            break
                    if outcome in ("daily_breach", "overall_breach"):
                        break

                    day_pnl = eq - day_start_eq
                    if day_pnl > max_day_pnl:
                        max_day_pnl = day_pnl

                    cum_pnl = eq - initial_balance
                    if cum_pnl >= target_profit:
                        if eff_cons < 0.99 and max_day_pnl > cum_pnl * eff_cons:
                            outcome = "consistency_locked"
                        else:
                            outcome = "passed"
                            days_to_pass.append(d + 1)
                            break

                if outcome == "passed":
                    passed += 1
                elif outcome == "daily_breach":
                    daily_breach += 1
                elif outcome == "overall_breach":
                    overall_breach += 1
                elif outcome == "consistency_locked":
                    consistency_locked += 1

            pass_prob = round((passed / n_paths) * 100.0, 2)
            d_breach_prob = round((daily_breach / n_paths) * 100.0, 2)
            o_breach_prob = round((overall_breach / n_paths) * 100.0, 2)
            c_lock_prob = round((consistency_locked / n_paths) * 100.0, 2)
            timeout_prob = round(max(0.0, 100.0 - pass_prob - d_breach_prob - o_breach_prob - c_lock_prob), 2)
            med_days = int(np.median(days_to_pass)) if days_to_pass else None
            gross_payout = target_profit * float(payout_split_pct)
            ev_net = round((passed / n_paths) * gross_payout - (1.0 - (passed / n_paths)) * float(challenge_fee_usd), 2)
            return {
                "risk_pct_per_trade": round(r_pct, 5),
                "pass_probability_pct": pass_prob,
                "daily_breach_probability_pct": d_breach_prob,
                "overall_breach_probability_pct": o_breach_prob,
                "consistency_locked_pct": c_lock_prob,
                "timeout_probability_pct": timeout_prob,
                "median_days_to_pass": med_days,
                "expected_net_value_usd": ev_net,
            }

        primary = _sim_risk_level(float(risk_pct_per_trade), int(seed))
        grid = [0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015]
        curve = [_sim_risk_level(g, int(seed) + idx * 17) for idx, g in enumerate(grid)]
        best = max(curve, key=lambda x: (x["expected_net_value_usd"], x["pass_probability_pct"]))

        if primary["pass_probability_pct"] >= 55.0 and primary["daily_breach_probability_pct"] <= 20.0:
            verdict = "FAVORABLE_EVALUATION_GEOMETRY"
        elif primary["daily_breach_probability_pct"] + primary["overall_breach_probability_pct"] >= 50.0:
            verdict = "HIGH_BREACH_RISK_REDUCE_SIZING"
        else:
            verdict = "MARGINAL_EVALUATION_ODDS"

        return {
            "tool": "gym.prop_challenge_sim",
            "verdict": verdict,
            "format_type": fmt,
            "n_paths": n_paths,
            "max_trading_days": max_trading_days,
            "trades_per_day": trades_per_day,
            "evaluated_risk_pct_per_trade": primary["risk_pct_per_trade"],
            "pass_probability_pct": primary["pass_probability_pct"],
            "daily_breach_probability_pct": primary["daily_breach_probability_pct"],
            "overall_breach_probability_pct": primary["overall_breach_probability_pct"],
            "consistency_locked_pct": primary["consistency_locked_pct"],
            "timeout_probability_pct": primary["timeout_probability_pct"],
            "median_days_to_pass": primary["median_days_to_pass"],
            "expected_net_value_usd": primary["expected_net_value_usd"],
            "optimal_risk_pct_per_trade": best["risk_pct_per_trade"],
            "optimal_expected_net_value_usd": best["expected_net_value_usd"],
            "risk_sizing_curve": curve,
            "methodology": (
                "Monte Carlo path simulation on caller-supplied trade R-multiples (or parametric "
                "win/loss payoffs) with 0.55x target-lock taper at >=70% profit target progress; "
                "never a guarantee of future prop-evaluation results."
            ),
        }

    return await run_tool("gym.prop_challenge_sim", _do)


def _eval_symbolic_ast(
    formula: str,
    close_mat: pd.DataFrame,
    vol_mat: pd.DataFrame,
) -> pd.DataFrame:
    """Safely evaluate a caller-supplied symbolic formula on a (dates x symbols) panel.

    Supports only causal time-series and cross-sectional primitives via a strict
    AST whitelist — never eval() or arbitrary attribute access, and never exposes
    any internal formulas.
    """
    import ast as _ast

    ret1d = close_mat.pct_change()
    vol_avg20 = vol_mat.rolling(20, min_periods=5).mean().replace(0.0, np.nan)
    vol_ratio_base = (vol_mat / vol_avg20).fillna(1.0)

    def _to_df(val: Any) -> pd.DataFrame:
        if isinstance(val, pd.DataFrame):
            return val
        return pd.DataFrame(float(val), index=close_mat.index, columns=close_mat.columns)

    def _cs_zscore(x: Any) -> pd.DataFrame:
        df = _to_df(x)
        mu = df.mean(axis=1)
        sd = df.std(axis=1, ddof=0).replace(0.0, np.nan)
        return df.sub(mu, axis=0).div(sd, axis=0).fillna(0.0)

    def _cs_rank(x: Any) -> pd.DataFrame:
        df = _to_df(x)
        return df.rank(axis=1, pct=True).fillna(0.5) - 0.5

    def _cs_winsorize(x: Any, clip_z: Any = 3.0) -> pd.DataFrame:
        z = _cs_zscore(x)
        lim = max(0.5, min(float(clip_z), 10.0))
        return z.clip(lower=-lim, upper=lim)

    def _ts_mean(x: Any, window: Any = 20) -> pd.DataFrame:
        w = max(2, min(int(window), 120))
        return _to_df(x).rolling(w, min_periods=max(2, w // 2)).mean().fillna(0.0)

    def _ts_std(x: Any, window: Any = 20) -> pd.DataFrame:
        w = max(2, min(int(window), 120))
        return _to_df(x).rolling(w, min_periods=max(2, w // 2)).std(ddof=0).fillna(0.0)

    def _ts_delta(x: Any, window: Any = 5) -> pd.DataFrame:
        w = max(1, min(int(window), 120))
        return _to_df(x).diff(w).fillna(0.0)

    def _ts_corr(x: Any, y: Any, window: Any = 20) -> pd.DataFrame:
        w = max(5, min(int(window), 120))
        dx, dy = _to_df(x), _to_df(y)
        return dx.rolling(w, min_periods=max(5, w // 2)).corr(dy).replace([np.inf, -np.inf], 0.0).fillna(0.0)

    def _ret(window: Any = 1) -> pd.DataFrame:
        w = max(1, min(int(window), 120))
        return close_mat.pct_change(w).fillna(0.0)

    def _kaufman_er(window: Any = 10) -> pd.DataFrame:
        w = max(2, min(int(window), 120))
        direction = close_mat.diff(w).abs()
        volatility = close_mat.diff(1).abs().rolling(w, min_periods=2).sum().replace(0.0, np.nan)
        return (direction / volatility).fillna(0.0)

    funcs = {
        "CS_ZScore": _cs_zscore,
        "CS_Rank": _cs_rank,
        "CS_Winsorize": _cs_winsorize,
        "TS_Mean": _ts_mean,
        "TS_Std": _ts_std,
        "TS_Delta": _ts_delta,
        "TS_Corr": _ts_corr,
        "KaufmanER": _kaufman_er,
        "Ret": _ret,
        "Abs": lambda x: _to_df(x).abs(),
        "Sign": lambda x: np.sign(_to_df(x)),
    }
    names = {
        "ret1d": ret1d.fillna(0.0),
        "ret5d": close_mat.pct_change(5).fillna(0.0),
        "ret20d": close_mat.pct_change(20).fillna(0.0),
        "close": close_mat,
        "volume": vol_mat,
        "vol_ratio": vol_ratio_base,
    }

    tree = _ast.parse(formula.strip(), mode="eval")

    def _walk(node: _ast.AST) -> Any:
        if isinstance(node, _ast.Expression):
            return _walk(node.body)
        if isinstance(node, _ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, _ast.Name):
            if node.id in names:
                return names[node.id]
            raise ValueError(f"Unknown variable {node.id!r}; allowed variables: {sorted(names)}")
        if isinstance(node, _ast.UnaryOp):
            v = _walk(node.operand)
            if isinstance(node.op, _ast.USub):
                return -v
            if isinstance(node.op, _ast.UAdd):
                return v
            raise ValueError("Unsupported unary operator")
        if isinstance(node, _ast.BinOp):
            left = _walk(node.left)
            right = _walk(node.right)
            if isinstance(node.op, _ast.Add):
                return left + right
            if isinstance(node.op, _ast.Sub):
                return left - right
            if isinstance(node.op, _ast.Mult):
                return left * right
            if isinstance(node.op, _ast.Div):
                denom = right.replace(0.0, np.nan) if isinstance(right, pd.DataFrame) else (right if right != 0 else np.nan)
                res = left / denom
                return res.replace([np.inf, -np.inf], 0.0).fillna(0.0) if isinstance(res, pd.DataFrame) else float(res)
            raise ValueError("Unsupported binary operator")
        if isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name):
            fn_name = node.func.id
            if fn_name not in funcs:
                raise ValueError(f"Unsupported function {fn_name!r}; allowed functions: {sorted(funcs)}")
            pos_args = [_walk(a) for a in node.args]
            return funcs[fn_name](*pos_args)
        raise ValueError(f"Unsupported syntax node {type(node).__name__} in symbolic formula")

    out = _to_df(_walk(tree))
    return out.replace([np.inf, -np.inf], 0.0).fillna(0.0)


async def evaluate_formula(
    formula: str,
    symbols: list[str] | None = None,
    bars: list[dict] | None = None,
    forward_horizon_days: int = 5,
) -> dict:
    """Evaluate a caller-supplied symbolic formula causally with a strict no-lookahead check.

    Computes out-of-sample Spearman cross-sectional rank IC, Newey-West HAC t-statistic,
    top-minus-bottom quantile spread (bps/day), daily turnover, and causal truncation
    invariance. Never exposes internal formulas or issues local promotion verdicts.
    """
    redaction.reject_keylike_args({
        "formula": formula,
        "symbols": symbols,
        "bars": bars,
        "forward_horizon_days": forward_horizon_days,
    })

    async def _do():
        expr = (formula or "").strip()
        if not expr or len(expr) > 400:
            raise ValueError("formula must be a non-empty symbolic expression up to 400 characters")
        h = max(1, min(int(forward_horizon_days), 20))
        panel_df = _load_panel(symbols, bars)
        syms = sorted(panel_df["symbol"].astype(str).unique().tolist())
        source_note = "caller_supplied_bars" if bars else "local_sqlite_cache"
        if len(syms) < 2:
            raise ValueError("gym.evaluate_formula requires at least 2 symbols for cross-sectional IC")

        close_mat = panel_df.pivot_table(index="ts", columns="symbol", values="close", aggfunc="last").sort_index().ffill()
        vol_mat = panel_df.pivot_table(index="ts", columns="symbol", values="volume", aggfunc="last").sort_index().ffill().fillna(1.0)
        if len(close_mat) < max(25, h + 15):
            raise ValueError(f"need at least {max(25, h + 15)} sessions to evaluate symbolic formula")

        sig_full = _eval_symbolic_ast(expr, close_mat, vol_mat)

        # Explicit no-lookahead truncation guard: recompute on panel truncated 5 sessions early
        trunc_idx = len(close_mat) - 5
        sig_trunc = _eval_symbolic_ast(expr, close_mat.iloc[:trunc_idx], vol_mat.iloc[:trunc_idx])
        max_trunc_diff = float((sig_full.iloc[:trunc_idx] - sig_trunc).abs().max().max())
        lookahead_clean = bool(max_trunc_diff <= 1e-9)

        # Forward h-day return aligned causally at decision date t (close_{t+h} / close_t - 1)
        fwd_ret = close_mat.shift(-h) / close_mat - 1.0
        valid_rows = sig_full.iloc[20 : len(close_mat) - h]
        valid_fwd = fwd_ret.iloc[20 : len(close_mat) - h]

        ic_series: list[float] = []
        spread_bps_series: list[float] = []
        for dt in valid_rows.index:
            s_row = valid_rows.loc[dt].dropna()
            r_row = valid_fwd.loc[dt].dropna()
            common = s_row.index.intersection(r_row.index)
            if len(common) < 2:
                continue
            sv = s_row.loc[common].to_numpy(dtype=float)
            rv = r_row.loc[common].to_numpy(dtype=float)
            if float(np.std(sv)) <= 1e-12 or float(np.std(rv)) <= 1e-12:
                ic_series.append(0.0)
                spread_bps_series.append(0.0)
                continue
            sr = pd.Series(sv).rank().to_numpy(dtype=float)
            rr = pd.Series(rv).rank().to_numpy(dtype=float)
            corr = float(np.corrcoef(sr, rr)[0, 1])
            if math.isfinite(corr):
                ic_series.append(corr)
            q_cut = max(1, len(common) // 3)
            order = np.argsort(sv)
            bot_ret = float(np.mean(rv[order[:q_cut]]))
            top_ret = float(np.mean(rv[order[-q_cut:]]))
            spread_bps_series.append(((top_ret - bot_ret) / h) * 10_000.0)

        if not ic_series:
            raise ValueError("formula produced constant or degenerate cross-sectional values across symbols")

        ic_arr = np.asarray(ic_series, dtype=float)
        n_obs = len(ic_arr)
        mean_ic = float(np.mean(ic_arr))
        std_ic = float(np.std(ic_arr, ddof=1)) if n_obs > 1 else 1.0

        # Newey-West HAC variance adjustment for overlapping forward horizons (lag = h - 1)
        lag = max(0, h - 1)
        gamma0 = float(np.mean((ic_arr - mean_ic) ** 2))
        hac_var = gamma0
        for k in range(1, min(lag + 1, n_obs // 2)):
            weight = 1.0 - k / (lag + 1.0)
            cov_k = float(np.mean((ic_arr[k:] - mean_ic) * (ic_arr[:-k] - mean_ic)))
            hac_var += 2.0 * weight * cov_k
        se_hac = math.sqrt(max(hac_var, 1e-12) / max(n_obs, 1))
        t_hac = round(mean_ic / se_hac, 3) if se_hac > 1e-9 else 0.0
        ir_annualized = round((mean_ic / max(std_ic, 1e-6)) * math.sqrt(252.0 / h), 3)

        rank_df = valid_rows.rank(axis=1, pct=True) - 0.5
        turnover_l1 = round(float(rank_df.diff().abs().mean(axis=1).dropna().mean()), 4) if len(rank_df) > 1 else 0.0
        mean_spread_bps = round(float(np.mean(spread_bps_series)), 2) if spread_bps_series else 0.0

        return {
            "tool": "gym.evaluate_formula",
            "status": "UNDERPOWERED_LOCAL",
            "formula": expr,
            "symbols": syms,
            "source": source_note,
            "forward_horizon_days": h,
            "n_cross_sections": n_obs,
            "lookahead_truncation_check": {
                "passed": lookahead_clean,
                "max_abs_truncation_diff": round(max_trunc_diff, 12),
            },
            "spearman_rank_ic": round(mean_ic, 4),
            "ic_std": round(std_ic, 4),
            "ir_annualized": ir_annualized,
            "t_hac": t_hac,
            "quantile_spread_bps_per_day": mean_spread_bps,
            "daily_rank_turnover_l1": turnover_l1,
            "allowed_operators": [
                "CS_ZScore", "CS_Rank", "CS_Winsorize", "TS_Mean", "TS_Std",
                "TS_Delta", "TS_Corr", "KaufmanER", "Ret", "Abs", "Sign",
            ],
            "why_not_a_promotion": (
                "Local formula evaluation tests only caller-supplied expressions on your panel window "
                "without the monotonic trials ledger, deflated-Sharpe gate, or 5-regime paired replay "
                "against the live champion."
            ),
            "cloud_job": {
                "tool": "tournament.submit",
                "next": "Encode your signal thresholds into a genome and submit to tournament.submit for 100-episode paired evaluation.",
            },
        }

    return await run_tool("gym.evaluate_formula", _do)




"""Hosted Shadow Tournament runner (runs inside the hosted HTTP service only).

Flow: site charges credits and POSTs the job to ``/internal/tournament/run``
(shared secret). This module replays the challenger AND the swarm's current
champion over the identical episode-seed matrix on the hosted bars_1day panel
(200 simulated genome-episodes = 100 seed draws per genome across 5 regimes x
4 per regime x 5 seeds, deduplicating to ~62 unique historical Friday paths
above MIN_EPISODES=20), computes the paired statistics, classifies the OUTCOME,
and calls the site back on ``/api/mcp/tournament/complete``.

What this deliberately is NOT: the promotion gate. Deflated-Sharpe margins,
the PBO cap and the monotonic trials ledger live in the swarm's private
registry (see README "IP boundary"); a CHALLENGER_BEATS_CHAMPION outcome is
what makes a genome eligible for that gate, and — with ``contribute`` — what
puts its vector in front of the swarm's proposer as an external challenger.

Configuration (hosted service env):
  SWARM_MCP_INTERNAL_KEY        shared secret with the site (required)
  SWARM_MCP_SERVICE_TOKEN       institutional token used to fill the hosted
                                panel through the relay (users are never
                                billed for the runner's own data pulls)
   SWARM_MCP_CHAMPION_GENOME     path to the registry's current champion JSON
                                 (override — step 29: no packaged fallback; when
                                 unset, the runner reads the live registry
                                 champion from Firestore champion_genome/latest)
   SWARM_MCP_CHAMPION_PROJECT    Firestore project for the champion doc
                                 (default initiative-121-prod)
  SWARM_MCP_TOURNAMENT_UNIVERSE comma-separated symbols (default: the data
                                server's DEFAULT_UNIVERSE)
  SWARM_MCP_TOURNAMENT_LOOKBACK_DAYS  panel depth (default 1010 ~ 4 years)
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import hashlib
import json
import logging
import os

import httpx
import pandas as pd

from swarm_mcp import vendor_path  # noqa: F401

from gates import MIN_EPISODES, PAIRED_P_VALUE_MAX  # vendored guardrails
from genome_schema import genome_hash, validate_genome  # vendored guardrails
from gym.regime import episode_pool, episode_seed_matrix, label_all_regimes  # vendored alpha
from gym.simulator import TierScoringRefusal  # vendored alpha
from objective import hard_constraint_violations  # vendored guardrails

from swarm_mcp import champion, genome_compat, gym_v4, plans, relay, request_context, server_meta
from swarm_mcp.gym_v4 import evaluate_genome_v4 as evaluate_genome
from swarm_mcp.cache import bars as cache_bars
from swarm_mcp.cache.db import get_db
from swarm_mcp.tools import gym_tools

log = logging.getLogger("swarm_mcp.tournament")

INTERNAL_KEY_ENV = "SWARM_MCP_INTERNAL_KEY"
SERVICE_TOKEN_ENV = "SWARM_MCP_SERVICE_TOKEN"
CHAMPION_ENV = "SWARM_MCP_CHAMPION_GENOME"
UNIVERSE_ENV = "SWARM_MCP_TOURNAMENT_UNIVERSE"
LOOKBACK_ENV = "SWARM_MCP_TOURNAMENT_LOOKBACK_DAYS"
DEFAULT_LOOKBACK_DAYS = 1010
DEFAULT_WINDOW_SESSIONS = 700
DEFAULT_WARMUP_SESSIONS = 260
MAX_HOLD_DAYS = 10
MIN_SYMBOLS = 8
CALLBACK_TIMEOUT_S = 30.0

OUTCOME_BEATS = "CHALLENGER_BEATS_CHAMPION"
OUTCOME_LOSES = "CHALLENGER_LOSES"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"

MIN_POPULATED_REGIMES = 3
MIN_ACTIVE_EPISODES = 10

_SCORER_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tournament-scorer")


def internal_key() -> str:
    return os.environ.get(INTERNAL_KEY_ENV, "").strip()


def load_champion() -> dict:
    """The registry's current champion (validated as its normalized v3 copy).

    Resolution: SWARM_MCP_CHAMPION_GENOME file override, else the live registry
    champion in Firestore ``champion_genome/latest`` (swarm_mcp.champion).
    Step 29: the packaged fallback is gone — a public wheel must never ship a
    champion that paying users are scored against. The returned genome is the
    ORIGINAL as-sourced dict (its hash identity is schema-agnostic); evaluation
    happens on the normalized v3 copy in run_job.
    """
    loaded = champion.load_champion_genome(file_override_env=CHAMPION_ENV)
    genome = loaded["genome"]
    normalized, errors = genome_compat.normalize_for_validation(genome)
    if not errors:
        errors = validate_genome(normalized)
    if errors:
        raise RuntimeError(f"champion genome invalid: {errors}")
    load_champion.last_loaded = loaded
    return genome


load_champion.last_loaded = {"genome_id": None, "champion_source": None}


def universe() -> list[str]:
    raw = os.environ.get(UNIVERSE_ENV, "").strip()
    if raw:
        return sorted({s.strip().upper() for s in raw.split(",") if s.strip()})
    from swarm_mcp.tools.data_tools import DEFAULT_UNIVERSE

    return list(DEFAULT_UNIVERSE)


def classify_detailed(stats: dict, violations: int, lookahead: int) -> dict:
    """Paired outcome classification + explicit benchmark & participation guards.

    BEATS:   n >= MIN_EPISODES, populated_regimes >= MIN_POPULATED_REGIMES (3),
             mean_challenger_bps > 0, challenger_active_episodes >= MIN_ACTIVE_EPISODES (10),
             Wilcoxon p <= PAIRED_P_VALUE_MAX, positive mean delta, non-negative
             worst populated regime margin, zero hard-constraint and lookahead violations.
    LOSES:   any violation, or a significant negative delta.
    else:    INCONCLUSIVE (underpowered, not significant, or blocked by anti-triviality guard).
    """
    mean_champ = stats.get("mean_champion_bps")
    mean_chal = stats.get("mean_challenger_bps")
    active_eps = stats.get("challenger_active_episodes")
    pop_regimes = int(stats.get("populated_regimes", 5))
    champ_neg = mean_champ is not None and float(mean_champ) < 0.0
    benchmark_status = "champion_negative_expectancy" if champ_neg else "ok"
    neg_suffix = (
        f" — champion negative-expectancy (champion mean {float(mean_champ):.2f} bps/wk)"
        if champ_neg
        else ""
    )

    if violations > 0 or lookahead > 0:
        return {
            "outcome": OUTCOME_LOSES,
            "outcome_detail": f"{OUTCOME_LOSES} (constraint/lookahead violation){neg_suffix}",
            "benchmark_status": benchmark_status,
        }
    n = stats["n_paired_episodes"]
    p = stats["paired_p_value"]
    if n < MIN_EPISODES or p is None:
        return {
            "outcome": OUTCOME_INCONCLUSIVE,
            "outcome_detail": f"{OUTCOME_INCONCLUSIVE} (underpowered n={n}){neg_suffix}",
            "benchmark_status": benchmark_status,
        }
    worst_margin = stats.get("worst_regime_margin_bps")
    if p <= PAIRED_P_VALUE_MAX and stats["mean_delta_bps"] < 0:
        return {
            "outcome": OUTCOME_LOSES,
            "outcome_detail": f"{OUTCOME_LOSES} (significant negative delta){neg_suffix}",
            "benchmark_status": benchmark_status,
        }
    if (
        p <= PAIRED_P_VALUE_MAX
        and stats["mean_delta_bps"] > 0
        and worst_margin is not None
        and worst_margin >= 0
    ):
        if pop_regimes < MIN_POPULATED_REGIMES:
            return {
                "outcome": OUTCOME_INCONCLUSIVE,
                "outcome_detail": (
                    f"{OUTCOME_INCONCLUSIVE} (populated_regimes={pop_regimes} < {MIN_POPULATED_REGIMES})"
                    f"{neg_suffix}"
                ),
                "benchmark_status": benchmark_status,
            }
        if mean_chal is not None and float(mean_chal) <= 0.0:
            return {
                "outcome": OUTCOME_INCONCLUSIVE,
                "outcome_detail": (
                    f"{OUTCOME_INCONCLUSIVE} (challenger mean {float(mean_chal):.2f} bps/wk <= 0; "
                    f"trading less than a losing champion cannot win){neg_suffix}"
                ),
                "benchmark_status": benchmark_status,
            }
        if active_eps is not None and int(active_eps) < MIN_ACTIVE_EPISODES:
            return {
                "outcome": OUTCOME_INCONCLUSIVE,
                "outcome_detail": (
                    f"{OUTCOME_INCONCLUSIVE} (challenger_active_episodes={int(active_eps)} < "
                    f"{MIN_ACTIVE_EPISODES} minimum active weeks){neg_suffix}"
                ),
                "benchmark_status": benchmark_status,
            }
        return {
            "outcome": OUTCOME_BEATS,
            "outcome_detail": f"{OUTCOME_BEATS}{neg_suffix}",
            "benchmark_status": benchmark_status,
        }
    return {
        "outcome": OUTCOME_INCONCLUSIVE,
        "outcome_detail": f"{OUTCOME_INCONCLUSIVE}{neg_suffix}",
        "benchmark_status": benchmark_status,
    }


def classify(stats: dict, violations: int, lookahead: int) -> str:
    """Paired outcome classification — statistics only, never a promotion."""
    return classify_detailed(stats, violations, lookahead)["outcome"]


def slice_panel_window(panel: pd.DataFrame, panel_window: dict | None) -> pd.DataFrame:
    """Pin or bound the evaluation panel to an explicit [start_session, end_session] window."""
    if not isinstance(panel_window, dict):
        return panel
    out = panel
    start_s = panel_window.get("start_session")
    end_s = panel_window.get("end_session") or panel_window.get("as_of")
    if start_s:
        out = out[out["ts"] >= pd.Timestamp(str(start_s))]
    if end_s:
        out = out[out["ts"] <= pd.Timestamp(str(end_s))]
    return _recompute_causal_panel_upto_end(out.reset_index(drop=True))


def _recompute_causal_panel_upto_end(panel: pd.DataFrame) -> pd.DataFrame:
    """Recompute forward-return columns strictly within `panel` so zero bars after
    `panel['ts'].max()` can ever leak into `entry_open_next` or `fwd_ret_{1..10}d`."""
    if panel.empty:
        return panel
    base_cols = [c for c in ("symbol", "ts", "open", "high", "low", "close", "volume") if c in panel.columns]
    if len(base_cols) == 7:
        return gym_v4.prepare_panel_v4(panel[base_cols].copy())
    out = panel.sort_values(["symbol", "ts"]).reset_index(drop=True).copy()
    grouped = out.groupby("symbol", sort=False)
    if "open" in out.columns:
        out["entry_open_next"] = grouped["open"].shift(-1)
    if "close" in out.columns and "entry_open_next" in out.columns:
        for h in range(1, 11):
            out[f"fwd_ret_{h}d"] = grouped["close"].shift(-h) / out["entry_open_next"] - 1.0
    return out


def compute_panel_data_hash(panel: pd.DataFrame) -> str:
    """Deterministic SHA-256 over sorted (symbol, ts, o, h, l, c, v) rows."""
    if panel.empty:
        return hashlib.sha256(b"").hexdigest()
    cols = [c for c in ("symbol", "ts", "open", "high", "low", "close", "volume") if c in panel.columns]
    sub = panel[cols].drop_duplicates(subset=["symbol", "ts"], keep="last").sort_values(["symbol", "ts"])
    h = hashlib.sha256()
    for row in sub.itertuples(index=False):
        sym = str(getattr(row, "symbol", "")).upper()
        day = str(pd.Timestamp(getattr(row, "ts")).date())
        o = float(getattr(row, "open", 0.0))
        hi = float(getattr(row, "high", 0.0))
        lo = float(getattr(row, "low", 0.0))
        c = float(getattr(row, "close", 0.0))
        v = int(getattr(row, "volume", 0))
        h.update(f"{sym}|{day}|{o:.6f}|{hi:.6f}|{lo:.6f}|{c:.6f}|{v}\n".encode("utf-8"))
    return h.hexdigest()


def build_rolling_window_context(
    panel: pd.DataFrame,
    *,
    as_of: str | None = None,
    window_sessions: int | None = None,
    warmup_sessions: int = DEFAULT_WARMUP_SESSIONS,
    max_hold_days: int = MAX_HOLD_DAYS,
    data_hash: str | None = None,
) -> dict:
    """Build point-in-time causal rolling window metadata & completed episode bounds.

    Guarantees:
      1. Zero rows with `ts > as_of` survive into feature, regime, or return calculation.
      2. When `window_sessions` is set (and panel has warm-up depth), `window_start`
         leaves `warmup_sessions` trailing bars prior to `window_start` so 250d regime
         quantiles and SMA50/ATR14 are fully warmed up.
      3. Scored Friday episodes end at `sessions[-1 - max_hold_days]` (when sufficient
         sessions exist) so every 1..10d hold exit is already realized on or before
         `as_of` — adding bar `as_of + 1` tomorrow changes ZERO historical scores.
    """
    work = panel
    if as_of:
        work = work[work["ts"] <= pd.Timestamp(str(as_of)[:10])].reset_index(drop=True)
    work = _recompute_causal_panel_upto_end(work)
    if work.empty:
        raise ValueError(f"panel has zero rows on or before as_of={as_of}")
    sessions = sorted(pd.to_datetime(work["ts"]).unique())
    if window_sessions is not None and int(window_sessions) > 0:
        win_len = int(window_sessions)
        total_needed = win_len + max(0, int(warmup_sessions))
        if len(sessions) > total_needed:
            # Trim excess ancient bars strictly before `warmup_start` so a rolling
            # window depends ONLY on `[warmup_start .. as_of]` (constant depth).
            keep_from = sessions[len(sessions) - total_needed]
            work = _recompute_causal_panel_upto_end(
                work[work["ts"] >= keep_from].reset_index(drop=True)
            )
            sessions = sorted(pd.to_datetime(work["ts"]).unique())
        win_start_idx = max(0, len(sessions) - win_len) if len(sessions) > win_len else 0
    else:
        win_start_idx = 0

    win_start_ts = pd.Timestamp(sessions[win_start_idx])
    win_end_ts = pd.Timestamp(sessions[-1])
    if len(sessions) - win_start_idx > max_hold_days + 25:
        completed_cutoff_ts = pd.Timestamp(sessions[len(sessions) - 1 - max_hold_days])
    else:
        completed_cutoff_ts = win_end_ts

    resolved_as_of = str(as_of)[:10] if as_of else str(win_end_ts.date())
    return {
        "panel": work,
        "as_of": resolved_as_of,
        "window_start": str(win_start_ts.date()),
        "window_end": str(win_end_ts.date()),
        "window_start_ts": win_start_ts,
        "completed_cutoff_ts": completed_cutoff_ts,
        "data_hash": data_hash or compute_panel_data_hash(work),
    }


async def _load_panel(*, as_of: str | None = None, window_sessions: int | None = None):
    """Fill the hosted panel through the relay with the service token (cached in
    the service's SQLite afterwards) and prepare it causally up to `as_of`."""
    db = get_db()
    syms = universe()
    lookback = int(os.environ.get(LOOKBACK_ENV, "").strip() or DEFAULT_LOOKBACK_DAYS)
    service_token = os.environ.get(SERVICE_TOKEN_ENV, "").strip()
    tok_ctx = request_context.current_token.set(service_token) if service_token else None
    internal_ctx = request_context.internal_runner_fill.set(True)
    try:
        if as_of or window_sessions:
            total_sessions = int(window_sessions or DEFAULT_WINDOW_SESSIONS) + DEFAULT_WARMUP_SESSIONS
            cal_days = max(int(lookback) + 420, int(total_sessions * 365 / 245) + 45)
            result = await cache_bars.get_bars_as_of_cached(
                db, syms, as_of=as_of, lookback_calendar_days=cal_days
            )
        else:
            result = await cache_bars.get_bars_cached(db, syms, lookback)
    finally:
        request_context.internal_runner_fill.reset(internal_ctx)
        if tok_ctx is not None:
            request_context.current_token.reset(tok_ctx)
    rows = result["rows"]
    if not rows:
        raise RuntimeError("hosted panel is empty — relay fill failed")
    panel = gym_tools._panel_from_bars(rows)
    if panel.empty:
        raise RuntimeError("hosted panel empty after preparation")
    return panel


def evaluate_champion_margin_gate(
    panel: pd.DataFrame,
    champion_genome: dict,
    seeds: list[int] | None = None,
    per_regime: int = 4,
    *,
    as_of: str | None = None,
    window_sessions: int | None = DEFAULT_WINDOW_SESSIONS,
) -> dict:
    """Check whether a candidate champion has clearly positive expectancy on the warmed-up panel.

    Margin rule: `mean_weekly_bps > 0` over `>= MIN_ACTIVE_EPISODES` (10) active weeks AND
    (`ci_95_lower > 0.0` OR `mean_weekly_bps >= 2.0`), preventing near-zero/negative champions
    where abstaining ('trading less') artificially wins.
    """
    use_seeds = list(seeds or plans.TOURNAMENT["seeds"])
    norm_champ, errs = genome_compat.normalize_for_validation(champion_genome)
    if errs:
        raise ValueError(f"champion invalid: {errs}")
    ctx = build_rolling_window_context(panel, as_of=as_of, window_sessions=window_sessions)
    work = ctx["panel"]
    labels = label_all_regimes(work)
    full_pool = episode_pool(work, labels, min_symbols=MIN_SYMBOLS)
    pool = [
        ep for ep in full_pool
        if ctx["window_start_ts"] <= ep["date"] <= ctx["completed_cutoff_ts"]
    ]
    matrix = episode_seed_matrix(pool, use_seeds, per_regime)
    run = evaluate_genome(work, matrix, norm_champ, use_seeds)
    eps_by_date = {e["date"]: e for e in run["episodes"]}
    unique_dates = sorted(eps_by_date)
    vals = [float(eps_by_date[d]["weekly_net_bps"]) for d in unique_dates]
    active_weeks = sum(
        1 for d in unique_dates
        if int(eps_by_date[d].get("n_positions", 0) or 0) > 0 or bool(eps_by_date[d].get("positions"))
    )
    mean_bps = float(pd.Series(vals).mean()) if vals else 0.0
    ci_lo, ci_hi, prob_pos = gym_tools._bootstrap_summary(vals)
    clearly_positive = (
        active_weeks >= MIN_ACTIVE_EPISODES
        and mean_bps > 0.0
        and (ci_lo > 0.0 or mean_bps >= 2.0)
    )
    if clearly_positive:
        reason = (
            f"PASS: mean={mean_bps:+.2f} bps/wk, 95% CI=[{ci_lo:+.2f}, {ci_hi:+.2f}], "
            f"active_weeks={active_weeks} >= {MIN_ACTIVE_EPISODES}"
        )
    else:
        reason = (
            f"FAIL_MARGIN: mean={mean_bps:+.2f} bps/wk, 95% CI=[{ci_lo:+.2f}, {ci_hi:+.2f}], "
            f"active_weeks={active_weeks} (requires active_weeks >= {MIN_ACTIVE_EPISODES} and "
            f"CI lower > 0 or mean >= +2.0 bps/wk)"
        )
    return {
        "passes_margin_gate": clearly_positive,
        "genome_hash": genome_compat.genome_hash_canonical(champion_genome),
        "as_of": ctx["as_of"],
        "window_start": ctx["window_start"],
        "window_end": ctx["window_end"],
        "data_hash": ctx["data_hash"],
        "n_unique_weeks": len(unique_dates),
        "active_weeks": active_weeks,
        "mean_weekly_bps": round(mean_bps, 4),
        "ci_95": [round(ci_lo, 4), round(ci_hi, 4)],
        "bootstrap_prob_positive": round(prob_pos, 4),
        "per_regime_bps": gym_tools._regime_scores_nullable([eps_by_date[d] for d in unique_dates]),
        "reason": reason,
    }


def paired_outcome(panel, champion: dict, challenger: dict, seeds: list[int], per_regime: int,
                   champion_eval: dict | None = None, challenger_eval: dict | None = None,
                   *, as_of: str | None = None, window_sessions: int | None = None,
                   warmup_sessions: int = DEFAULT_WARMUP_SESSIONS,
                   max_hold_days: int = MAX_HOLD_DAYS,
                   data_hash: str | None = None) -> dict:
    """Run both genomes on the identical matrix and return the result block.

    ``*_eval`` carry the schema-normalized evaluation copies (v2 -> v3); the
    original genomes are only used for their canonical hash identity. Costs are
    the immutable house constants, never the genome's cost genes (A-01).
    """
    ctx = build_rolling_window_context(
        panel,
        as_of=as_of,
        window_sessions=window_sessions,
        warmup_sessions=warmup_sessions,
        max_hold_days=max_hold_days,
        data_hash=data_hash,
    )
    work = ctx["panel"]
    labels = label_all_regimes(work)
    full_pool = episode_pool(work, labels, min_symbols=MIN_SYMBOLS)
    pool = [
        ep for ep in full_pool
        if ctx["window_start_ts"] <= ep["date"] <= ctx["completed_cutoff_ts"]
    ]
    matrix = episode_seed_matrix(pool, seeds, per_regime)
    champ_run = evaluate_genome(work, matrix, champion_eval or champion, seeds)
    chal_run = evaluate_genome(work, matrix, challenger_eval or challenger, seeds,
                               champion_genome=champion_eval or champion)
    stats = gym_tools.paired_stats(champ_run, chal_run)
    per_ep = [{"max_gross_exposure_pct": e["max_gross_exposure_pct"],
               "max_position_pct": e["max_position_pct"]} for e in chal_run["episodes"]]
    violations = hard_constraint_violations(chal_run["weekly_net_bps"], per_ep)
    lookahead = len(chal_run["lookahead_violations"])
    verdict_info = classify_detailed(stats, len(violations), lookahead)
    win_lo = pd.Timestamp(ctx["window_start"]).date()
    win_hi = pd.Timestamp(ctx["window_end"]).date()
    win_weeks = round((win_hi - win_lo).days / 7.0, 2)
    return {
        "outcome": verdict_info["outcome"],
        "outcome_detail": verdict_info["outcome_detail"],
        "benchmark_status": verdict_info["benchmark_status"],
        "as_of": ctx["as_of"],
        "window_start": ctx["window_start"],
        "window_end": ctx["window_end"],
        "completed_episode_end": str(ctx["completed_cutoff_ts"].date()),
        "data_hash": ctx["data_hash"],
        "cost_model": gym_v4.HOUSE_COST_MODEL,
        "schema_version": gym_v4.EVALUATED_SCHEMA_VERSION,
        "evaluated_schema_version": gym_v4.EVALUATED_SCHEMA_VERSION,
        "n_paired_episodes": stats["n_paired_episodes"],
        "seed_draws_per_genome": stats["seed_draws_per_genome"],
        "mean_delta_bps": stats["mean_delta_bps"],
        "mean_champion_bps": stats["mean_champion_bps"],
        "champion_ci_95": stats["champion_ci_95"],
        "champion_active_episodes": stats["champion_active_episodes"],
        "mean_challenger_bps": stats["mean_challenger_bps"],
        "challenger_active_episodes": stats["challenger_active_episodes"],
        "paired_p_value": stats["paired_p_value"],
        "delta_ci_95": stats["delta_ci_95"],
        "win_share_pct": stats["win_share_pct"],
        "bootstrap_prob_positive_delta": stats["bootstrap_prob_positive_delta"],
        "worst_regime_margin_bps": stats["worst_regime_margin_bps"],
        "populated_regimes": stats["populated_regimes"],
        "per_regime_episode_counts": stats["per_regime_episode_counts"],
        "per_regime_delta_bps": stats["per_regime_delta_bps"],
        "per_regime_champion_bps": stats["per_regime_champion"],
        "per_regime_challenger_bps": stats["per_regime_challenger"],
        "hard_constraint_violations": len(violations),
        "lookahead_violations": lookahead,
        "champion_hash": genome_compat.genome_hash_canonical(champion),
        "challenger_hash": genome_compat.genome_hash_canonical(challenger),
        "seeds": seeds,
        "per_regime": per_regime,
        "fidelity": chal_run["fidelity"],
        "panel": {
            "symbols": int(work["symbol"].nunique()),
            "oldest_session": ctx["window_start"],
            "newest_session": ctx["window_end"],
            "warmup_oldest_session": str(work["ts"].min().date()),
            "depth_weeks": win_weeks,
            "data_hash": ctx["data_hash"],
        },
        "gate_note": ("paired outcome only — the promotion gate (DSR margin, PBO cap, trials ledger) "
                      "runs inside the swarm's registry on qualifying challengers"),
        "runner_version": server_meta.PACKAGE_VERSION,
    }


async def _callback(job_id: str, *, result: dict | None = None, error: str | None = None,
                    requeue: bool = False) -> None:
    payload: dict = {"job_id": job_id}
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    if requeue:
        payload["requeue"] = True
    # The site's complete handler is idempotent for terminal job states, so a
    # retried callback can never double-refund or double-promote.
    last_status = None
    last_text = ""
    for attempt, delay in enumerate((0.0, 1.0, 4.0, 16.0)):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_S) as client:
                r = await client.post(f"{relay.relay_base()}/tournament/complete", json=payload,
                                      headers={"X-Swarm-Internal-Key": internal_key()})
            last_status = r.status_code
            last_text = r.text[:200]
            if r.status_code == 200:
                log.info("tournament callback for %s delivered (attempt %s)", job_id, attempt + 1)
                return
        except httpx.HTTPError as e:
            last_status, last_text = None, type(e).__name__
        log.warning("tournament callback for %s attempt %s failed: HTTP %s %s",
                    job_id, attempt + 1, last_status, last_text)
    log.error("tournament callback for %s failed after %s attempts: HTTP %s %s",
              job_id, len((0.0, 1.0, 4.0, 16.0)), last_status, last_text)


async def run_job(job: dict) -> None:
    """Score one job end-to-end and report back. Never raises into the server."""
    job_id = str(job.get("job_id", ""))
    try:
        challenger = job.get("genome")
        if not isinstance(challenger, dict):
            raise ValueError("job has no genome")
        normalized, errors = genome_compat.normalize_for_validation(challenger)
        if not errors:
            errors = validate_genome(normalized)
        if errors:
            raise ValueError(f"challenger genome invalid: {errors[:3]}")
        seeds = [int(s) for s in (job.get("seeds") or plans.TOURNAMENT["seeds"])]
        per_regime = int(job.get("per_regime") or plans.TOURNAMENT["per_regime"])
        champ_genome = load_champion()
        champ_norm, champ_errors = genome_compat.normalize_for_validation(champ_genome)
        if champ_errors or validate_genome(champ_norm):
            raise RuntimeError("champion genome invalid")
        if not job.get("house_seed") and not job.get("rescore"):
            try:
                gym_v4.assert_has_scored_change(normalized, champ_norm)
            except gym_v4.NoScoredChangeError as e:
                await _callback(job_id, error=str(e))
                return
        raw_window = job.get("panel_window") if isinstance(job.get("panel_window"), dict) else {}
        job_as_of = job.get("as_of") or raw_window.get("as_of") or raw_window.get("end_session")
        job_win_sessions = job.get("window_sessions") or raw_window.get("window_sessions")
        if job_as_of or job_win_sessions:
            raw_panel = await _load_panel(
                as_of=str(job_as_of)[:10] if job_as_of else None,
                window_sessions=int(job_win_sessions) if job_win_sessions else DEFAULT_WINDOW_SESSIONS,
            )
            panel = raw_panel
            eff_win_sessions = int(job_win_sessions) if job_win_sessions else DEFAULT_WINDOW_SESSIONS
        else:
            raw_panel = await _load_panel()
            panel = slice_panel_window(raw_panel, job.get("panel_window"))
            eff_win_sessions = None
        if panel.empty:
            raise RuntimeError("hosted panel empty after applying rolling window")
        loop = asyncio.get_running_loop()
        bound_outcome = functools.partial(
            paired_outcome,
            panel,
            champ_genome,
            challenger,
            seeds,
            per_regime,
            champion_eval=champ_norm,
            challenger_eval=normalized,
            as_of=str(job_as_of)[:10] if job_as_of else None,
            window_sessions=eff_win_sessions,
        )
        try:
            result = await loop.run_in_executor(_SCORER_POOL, bound_outcome)
        except TierScoringRefusal as e:
            # Tier-B/C mutations cannot be scored on the tier-A panel: refuse
            # (credits are refunded by the site), never neutral-fill.
            await _callback(job_id, error=f"UNSCORABLE: {e}")
            return
        loaded = load_champion.last_loaded or {}
        result["champion_id"] = loaded.get("genome_id")
        result["champion_source"] = loaded.get("champion_source")
        result["cost_model"] = gym_v4.HOUSE_COST_MODEL
        result["evaluated_schema_version"] = gym_v4.EVALUATED_SCHEMA_VERSION
        log.info("tournament job %s scored: outcome=%s n=%s", job_id, result.get("outcome"),
                 result.get("n_paired_episodes"))
        await _callback(job_id, result=result)
    except asyncio.CancelledError:
        log.warning("tournament job %s interrupted by runner shutdown; requesting requeue", job_id)
        try:
            await asyncio.shield(_callback(job_id, error="runner_draining", requeue=True))
        except Exception:  # noqa: BLE001
            log.exception("tournament job %s: requeue callback failed", job_id)
        raise
    except Exception as e:  # noqa: BLE001 — must always report back so credits refund
        log.exception("tournament job %s failed", job_id)
        try:
            await _callback(job_id, error=f"{type(e).__name__}: {str(e)[:300]}")
        except Exception:  # noqa: BLE001
            log.exception("tournament job %s: callback failed", job_id)

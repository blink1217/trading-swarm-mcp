"""Hosted Shadow Tournament runner (runs inside the hosted HTTP service only).

Flow: site charges credits and POSTs the job to ``/internal/tournament/run``
(shared secret). This module replays the challenger AND the swarm's current
champion over the identical episode-seed matrix on the hosted bars_1day panel
(full geometry: 5 regimes x 4 per regime x 5 seeds = 100 paired episodes, above
MIN_EPISODES=20), computes the paired statistics, classifies the OUTCOME, and
calls the site back on ``/api/mcp/tournament/complete``.

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
                                (default: packaged swarm_mcp/data/champion_genome.json)
  SWARM_MCP_TOURNAMENT_UNIVERSE comma-separated symbols (default: the data
                                server's DEFAULT_UNIVERSE)
  SWARM_MCP_TOURNAMENT_LOOKBACK_DAYS  panel depth (default 1010 ~ 4 years)
"""
from __future__ import annotations

import json
import logging
import os
from importlib import resources

import httpx

from swarm_mcp import vendor_path  # noqa: F401

from gates import MIN_EPISODES, PAIRED_P_VALUE_MAX  # vendored guardrails
from genome_schema import genome_hash, validate_genome  # vendored guardrails
from gym.regime import episode_pool, episode_seed_matrix, label_all_regimes  # vendored alpha
from gym.simulator import TierScoringRefusal, evaluate_genome  # vendored alpha
from objective import hard_constraint_violations  # vendored guardrails

from swarm_mcp import plans, relay, request_context, server_meta
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
MIN_SYMBOLS = 8
CALLBACK_TIMEOUT_S = 30.0

OUTCOME_BEATS = "CHALLENGER_BEATS_CHAMPION"
OUTCOME_LOSES = "CHALLENGER_LOSES"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"


def internal_key() -> str:
    return os.environ.get(INTERNAL_KEY_ENV, "").strip()


def load_champion() -> dict:
    path = os.environ.get(CHAMPION_ENV, "").strip()
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            genome = json.load(fh)
    else:
        genome = json.loads(resources.files("swarm_mcp.data").joinpath("champion_genome.json").read_text("utf-8"))
    genome.pop("_note", None)
    errors = validate_genome(genome)
    if errors:
        raise RuntimeError(f"champion genome invalid: {errors}")
    return genome


def universe() -> list[str]:
    raw = os.environ.get(UNIVERSE_ENV, "").strip()
    if raw:
        return sorted({s.strip().upper() for s in raw.split(",") if s.strip()})
    from swarm_mcp.tools.data_tools import DEFAULT_UNIVERSE

    return list(DEFAULT_UNIVERSE)


def classify(stats: dict, violations: int, lookahead: int) -> str:
    """Paired outcome classification — statistics only, never a promotion.

    BEATS:   n >= MIN_EPISODES, Wilcoxon p <= PAIRED_P_VALUE_MAX, positive mean
             delta, non-negative worst-regime margin, zero hard-constraint and
             lookahead violations.
    LOSES:   any violation, or a significant negative delta.
    else:    INCONCLUSIVE (underpowered or not significant).
    """
    if violations > 0 or lookahead > 0:
        return OUTCOME_LOSES
    n = stats["n_paired_episodes"]
    p = stats["paired_p_value"]
    if n < MIN_EPISODES or p is None:
        return OUTCOME_INCONCLUSIVE
    if p <= PAIRED_P_VALUE_MAX and stats["mean_delta_bps"] > 0 and stats["worst_regime_margin_bps"] >= 0:
        return OUTCOME_BEATS
    if p <= PAIRED_P_VALUE_MAX and stats["mean_delta_bps"] < 0:
        return OUTCOME_LOSES
    return OUTCOME_INCONCLUSIVE


async def _load_panel():
    """Fill the hosted panel through the relay with the service token (cached in
    the service's SQLite afterwards) and prepare it for the gym."""
    db = get_db()
    syms = universe()
    lookback = int(os.environ.get(LOOKBACK_ENV, "").strip() or DEFAULT_LOOKBACK_DAYS)
    service_token = os.environ.get(SERVICE_TOKEN_ENV, "").strip()
    tok_ctx = request_context.current_token.set(service_token) if service_token else None
    try:
        result = await cache_bars.get_bars_cached(db, syms, lookback)
    finally:
        if tok_ctx is not None:
            request_context.current_token.reset(tok_ctx)
    rows = result["rows"]
    if not rows:
        raise RuntimeError("hosted panel is empty — relay fill failed")
    panel = gym_tools._panel_from_bars(rows)
    if panel.empty:
        raise RuntimeError("hosted panel empty after preparation")
    return panel


def paired_outcome(panel, champion: dict, challenger: dict, seeds: list[int], per_regime: int) -> dict:
    """Run both genomes on the identical matrix and return the result block."""
    labels = label_all_regimes(panel)
    pool = episode_pool(panel, labels, min_symbols=MIN_SYMBOLS)
    matrix = episode_seed_matrix(pool, seeds, per_regime)
    champ_run = evaluate_genome(panel, matrix, champion, seeds)
    chal_run = evaluate_genome(panel, matrix, challenger, seeds, champion_genome=champion)
    stats = gym_tools.paired_stats(champ_run, chal_run)
    per_ep = [{"max_gross_exposure_pct": e["max_gross_exposure_pct"],
               "max_position_pct": e["max_position_pct"]} for e in chal_run["episodes"]]
    violations = hard_constraint_violations(chal_run["weekly_net_bps"], per_ep)
    lookahead = len(chal_run["lookahead_violations"])
    depth = gym_tools._depth_meta(panel)
    return {
        "outcome": classify(stats, len(violations), lookahead),
        "n_paired_episodes": stats["n_paired_episodes"],
        "mean_delta_bps": stats["mean_delta_bps"],
        "paired_p_value": stats["paired_p_value"],
        "delta_ci_95": stats["delta_ci_95"],
        "worst_regime_margin_bps": stats["worst_regime_margin_bps"],
        "per_regime_delta_bps": stats["per_regime_delta_bps"],
        "per_regime_champion_bps": stats["per_regime_champion"],
        "per_regime_challenger_bps": stats["per_regime_challenger"],
        "hard_constraint_violations": len(violations),
        "lookahead_violations": lookahead,
        "champion_hash": genome_hash(champion),
        "challenger_hash": genome_hash(challenger),
        "seeds": seeds,
        "per_regime": per_regime,
        "fidelity": chal_run["fidelity"],
        "panel": {
            "symbols": int(panel["symbol"].nunique()),
            "oldest_session": depth["oldest_session"],
            "newest_session": depth["newest_session"],
            "depth_weeks": depth["local_depth_weeks"],
        },
        "gate_note": ("paired outcome only — the promotion gate (DSR margin, PBO cap, trials ledger) "
                      "runs inside the swarm's registry on qualifying challengers"),
        "runner_version": server_meta.PACKAGE_VERSION,
    }


async def _callback(job_id: str, *, result: dict | None = None, error: str | None = None) -> None:
    payload: dict = {"job_id": job_id}
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = error
    async with httpx.AsyncClient(timeout=CALLBACK_TIMEOUT_S) as client:
        r = await client.post(f"{relay.relay_base()}/tournament/complete", json=payload,
                              headers={"X-Swarm-Internal-Key": internal_key()})
    if r.status_code != 200:
        log.error("tournament callback for %s failed: HTTP %s %s", job_id, r.status_code, r.text[:200])


async def run_job(job: dict) -> None:
    """Score one job end-to-end and report back. Never raises into the server."""
    job_id = str(job.get("job_id", ""))
    try:
        challenger = job.get("genome")
        if not isinstance(challenger, dict):
            raise ValueError("job has no genome")
        errors = validate_genome(challenger)
        if errors:
            raise ValueError(f"challenger genome invalid: {errors[:3]}")
        seeds = [int(s) for s in (job.get("seeds") or plans.TOURNAMENT["seeds"])]
        per_regime = int(job.get("per_regime") or plans.TOURNAMENT["per_regime"])
        champion = load_champion()
        panel = await _load_panel()
        try:
            result = paired_outcome(panel, champion, challenger, seeds, per_regime)
        except TierScoringRefusal as e:
            # Tier-B/C mutations cannot be scored on the tier-A panel: refuse
            # (credits are refunded by the site), never neutral-fill.
            await _callback(job_id, error=f"UNSCORABLE: {e}")
            return
        await _callback(job_id, result=result)
    except Exception as e:  # noqa: BLE001 — must always report back so credits refund
        log.exception("tournament job %s failed", job_id)
        try:
            await _callback(job_id, error=f"{type(e).__name__}: {str(e)[:300]}")
        except Exception:  # noqa: BLE001
            log.exception("tournament job %s: callback failed", job_id)

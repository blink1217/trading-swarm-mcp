"""Episode replay: the deterministic weekly decision path.

FIDELITY LABEL — TIER-A PRICE SUBSET ONLY. This simulator replays bars_1day
prices through the screen/sizing/weekend-gate logic with a pessimistic fill
model. It does NOT model Finnhub sentiment, the earnings calendar, Finviz
membership, weather/energy context, or LLM verdicts. Any genome whose MUTATED
genes exceed tier A cannot be scored here — the gym refuses (it never silently
defaults tier-B/C features to 0.0, which would fabricate evidence). Scoring
such a challenger is the tape_replay service's job (plan decision 5, R9).

replay_episode(panel, episode, genome, equity) replays ONE week for ONE genome
on a FIXED market path (the panel). Because the path is fixed and features are
causal, the same episode is identical across genomes except for the genome's
own parameters — this is what makes paired champion-vs-challenger comparison
legitimate.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import guardrails_path  # noqa: E402,F401  (ensures guardrails on sys.path)

from fill_model import round_trip_cost_bps  # noqa: E402  (guardrails)
from objective import MAX_GROSS_EXPOSURE_PCT  # noqa: E402  (guardrails)
from tiers import highest_mutated_tier  # noqa: E402  (guardrails)

from gym.panel import decision_features, assert_no_lookahead  # noqa: E402
from gym.policy_head import weekend_action  # noqa: E402
from shared.swing_screens import union_mask  # noqa: E402

EQUITY = 100_000.0
FIDELITY_LABEL = "tier-A price subset (bars_1day only; no sentiment/earnings/finviz/weather/LLM inputs)"

# Decision-time state recorded on every simulated position (the replay-buffer
# observation). All are trailing-window features from gym.panel — no lookahead.
STATE_FEATURES = ["atr_pct", "rsi_14", "mom_5d", "mom_20d", "vol_ratio_20",
                  "breakout_dist_20d", "gap_open", "vwap_stretch_20", "ret_1d"]


class TierScoringRefusal(RuntimeError):
    """The gym refuses to score a genome whose mutated genes exceed tier A.

    This is the anti-fabrication invariant: a neutral fill (defaulting
    finnhub_sentiment / earnings_flag / finviz_score to 0.0) would silently
    manufacture evidence the price panel cannot provide. The correct verdict
    is UNSCORABLE (plan decision 5, R8/R9).
    """


def assert_tier_a_scortable(genome: dict, champion_genome: dict | None) -> None:
    """Raise TierScoringRefusal if the challenger's mutated genes exceed tier A.

    With no champion given there is nothing to diff against and the genome is
    scored as a baseline measurement (tier-A price subset).
    """
    if champion_genome is None:
        return
    tier = highest_mutated_tier(champion_genome, genome)
    if tier in ("B", "C"):
        raise TierScoringRefusal(
            f"UNSCORABLE: mutated genes reach provenance tier {tier}; the gym is {FIDELITY_LABEL}. "
            f"Use services/tape_replay with archived point-in-time values instead — "
            f"never neutral-fill tier-B/C features.")


def _cross_section(panel: pd.DataFrame, episode) -> pd.DataFrame:
    """Cross-sectional feature frame (index=symbol) at the episode date, causal."""
    rows = {}
    for s in episode["symbols"]:
        if s not in panel["symbol"].unique():
            continue
        f = decision_features(panel, s, episode["date"])
        if f is not None and not np.isnan(f["close"]):
            rows[s] = f
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).T


def _screen_mask(panel: pd.DataFrame, episode, genome) -> pd.Series:
    """Boolean membership mask over candidate symbols via the shared bar-based
    screens (shared/swing_screens.py) — the same predicates services/analyst
    runs live, so gym membership and live membership are directly comparable."""
    df = _cross_section(panel, episode)
    if df.empty:
        return pd.Series(dtype=bool)
    return union_mask(df, genome)


def _heuristic_filter_score(feats: dict) -> float:
    """Bar-feature-only heuristic fallback scoring (mirrors analyst's LGBM gate).

    STRICTLY tier-A inputs. This function must never read finnhub_sentiment,
    earnings_flag, or finviz_score — defaulting those to 0.0 would be a silent
    provenance substitution. Callers holding a genome with tier-B/C mutations
    are refused upstream by assert_tier_a_scortable.
    """
    score = 0.5
    score += 0.15 if feats.get("vol_ratio_20", 1.0) > 1.5 else -0.10
    score += 0.12 if feats.get("mom_20d", 0.0) > 0.05 else -0.08
    bd = feats.get("breakout_dist_20d", 0.0)
    score += 0.10 if -0.02 < bd < 0.02 else -0.12
    rsi = feats.get("rsi_14", 50.0)
    score += 0.05 if 40.0 <= rsi <= 65.0 else -0.05
    return max(0.0, min(1.0, score))


def _analyst_score(feats: dict, genome) -> tuple[float, bool]:
    """LGBM gate (or bar-only heuristic fallback) + genome threshold."""
    threshold = genome["analyst"]["lgbm_threshold"]
    try:
        from analyst_filter import score_candidate  # vendored analyst module if present
        proba, _ = score_candidate(feats)
        return proba, proba >= threshold
    except Exception:
        proba = _heuristic_filter_score(feats)
        return proba, proba >= threshold


def _size_position(genome, atr_14: float, close: float) -> float:
    """qty = equity * risk_pct / (ATR * atr_mult); position pct of equity."""
    r = genome["risk"]
    if not atr_14 or atr_14 <= 0 or not close or close <= 0:
        return 0.0
    qty = (EQUITY * r["risk_pct_per_trade"]) / (atr_14 * r["atr_sizing_mult"])
    pct = qty * close / EQUITY
    return min(pct, 0.25)  # MAX_POSITION_PCT floor lives here and is invariant-tested


def replay_episode(panel: pd.DataFrame, episode: dict, genome: dict,
                   earnings_map: dict | None = None, energy_map: dict | None = None) -> dict:
    """Replay one week for one genome on a fixed market path.

    Returns an episode summary dict with weekly net bps, turnover, max gross
    exposure, max per-name position, trades, and lookahead violations (should
    be empty). `earnings_map`/`energy_map` are optional TIER-B/C context maps
    the tape_replay service supplies; the gym itself never fabricates them.
    """
    exec_cfg = genome["execution"]
    cost_bps = round_trip_cost_bps(exec_cfg["spread_bps"], exec_cfg["slippage_bps"], exec_cfg["adverse_selection_bps"])

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
        # Runtime enforcement mirror: the risk layer refuses anything above the
        # weekend gross cap, so the replay adds positions in score order only
        # until the cap is reached (the rest of the candidates never execute).
        if gross >= MAX_GROSS_EXPOSURE_PCT:
            break
        size_pct = min(size_pct, MAX_GROSS_EXPOSURE_PCT - gross)
        earnings_days = (earnings_map or {}).get((s, pd.Timestamp(episode["date"])))
        energy_bias = (energy_map or {}).get(s)
        action, target_frac = weekend_action(genome["weekend_gate"], earnings_days=earnings_days,
                                             atr_pct=float(f["atr_pct"]) if not np.isnan(f["atr_pct"]) else None,
                                             gap_risk_pct=float(f["gap_open"]) if not np.isnan(f["gap_open"]) else None,
                                             energy_bias=energy_bias)
        final_pct = size_pct * target_frac
        gross += final_pct
        fwd = float(f["fwd_ret_5d"]) if "fwd_ret_5d" in f and not np.isnan(f["fwd_ret_5d"]) else 0.0
        net_ret = fwd * 1e4 - cost_bps
        positions.append({
            "symbol": s, "size_pct": size_pct, "target_frac": target_frac, "final_pct": final_pct,
            "gross_ret_bps": fwd * 1e4, "net_ret_bps": net_ret, "proba": proba,
            # decision-time state + the action taken: the (s, a, r) transition
            # the learned world model trains on. Causal features only.
            "action": action,
            "state": {k: (None if np.isnan(float(f[k])) else float(f[k])) for k in STATE_FEATURES if k in f},
        })

    gross = sum(p["final_pct"] for p in positions)
    max_pos = max((p["final_pct"] for p in positions), default=0.0)
    turnover = sum(p["final_pct"] for p in positions)  # % of equity traded this week
    weekly_net_bps = sum(p["final_pct"] * p["net_ret_bps"] for p in positions)

    return _episode_summary(episode, positions, weekly_net_bps, turnover, gross, max_pos, lookahead, [])


def _episode_summary(episode, positions, weekly_net_bps, turnover_pct, gross_pct, max_pos_pct, lookahead, violations) -> dict:
    return {
        "date": str(pd.Timestamp(episode["date"]).date()),
        "regime": episode.get("regime", "chop"),
        "symbols": episode.get("symbols", []),
        "n_positions": len(positions),
        "weekly_net_bps": round(weekly_net_bps, 4),
        "turnover_pct": round(turnover_pct, 6),
        "max_gross_exposure_pct": round(gross_pct, 6),
        "max_position_pct": round(max_pos_pct, 6),
        "positions": positions,
        "lookahead_violations": lookahead,
        "constraint_violations": violations,
    }


def evaluate_genome(panel: pd.DataFrame, episodes_by_seed: dict[int, list[dict]], genome: dict,
                    seeds: list[int] | None = None, champion_genome: dict | None = None) -> dict:
    """Run a genome over the shared episode-seed matrix.

    Returns per-seed and pooled episode summaries plus aggregate stats. Two
    genomes evaluated with the SAME `episodes_by_seed` produce comparable runs
    (paired). If `champion_genome` is given and the challenger's mutated genes
    exceed tier A, TierScoringRefusal is raised BEFORE any episode runs — the
    gym never fabricates tier-B/C evidence.
    """
    assert_tier_a_scortable(genome, champion_genome)
    seeds = seeds or list(episodes_by_seed.keys())
    all_eps: list[dict] = []
    per_seed = {}
    for s in seeds:
        eps = [replay_episode(panel, e, genome) for e in episodes_by_seed[s]]
        for e in eps:
            e["seed"] = int(s)
        per_seed[s] = eps
        all_eps.extend(eps)
    weekly = [e["weekly_net_bps"] for e in all_eps if e["lookahead_violations"] == []]
    lookahead = [v for e in all_eps for v in e["lookahead_violations"]]
    return {
        "fidelity": FIDELITY_LABEL,
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

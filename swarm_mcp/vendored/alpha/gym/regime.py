"""Adversarial regime sampler — the 'co-evolving opponent'.

The market does not react to the swarm, so true self-play is impossible. Instead the
'opponent' is a sampler that can hand the challenger the regime that hurts it most
(maximin, not mean). Regime labels are computed from TRAILING data only (no lookahead),
and episodes are block-bootstrapped deterministically per seed so champion and
challenger see IDENTICAL market paths in paired tournaments.
"""
from __future__ import annotations

import random

import numpy as np
import pandas as pd

REGIMES = ["high_vol", "drawdown", "chop", "melt_up", "gap_heavy"]

VOL_QUANTILE = 0.80
DRAWDOWN_FROM_HIGH = 0.05
MELTUP_MOM_60D = 0.15
GAP_HEAVY_MEAN_GAP = 0.015


def market_returns(panel: pd.DataFrame) -> pd.Series:
    """Equal-weight daily market return series indexed by ts."""
    s = panel.groupby("ts")["ret_1d"].mean().sort_index()
    return s.dropna()


def label_regime(panel: pd.DataFrame, decision_date, lookback: int = 250) -> str:
    """Classify the regime as of `decision_date` using only trailing data.

    Priority order: high_vol > drawdown > melt_up > gap_heavy > chop.
    """
    mr = market_returns(panel)
    hist = mr[mr.index <= pd.Timestamp(decision_date)]
    if len(hist) < 30:
        return "chop"
    rets = hist.iloc[-1]
    vol20 = hist.iloc[-20:].std() * np.sqrt(252) if len(hist) >= 20 else 0.0
    vol_all = hist.rolling(20).std().dropna() * np.sqrt(252)
    if len(vol_all) >= 20 and vol20 >= vol_all.quantile(VOL_QUANTILE):
        return "high_vol"
    cum = (1.0 + rets).cumprod()
    high = cum.iloc[-1] / (cum.iloc[-90:].max() + 1e-12) - 1.0 if len(cum) >= 90 else 0.0
    if high <= -DRAWDOWN_FROM_HIGH:
        return "drawdown"
    mom60 = cum.iloc[-1] / (cum.iloc[-61] + 1e-12) - 1.0 if len(cum) >= 61 else 0.0
    if mom60 >= MELTUP_MOM_60D:
        return "melt_up"
    avg_gap = panel[panel["ts"] <= pd.Timestamp(decision_date)]["gap_open"].abs().tail(20).mean()
    if not np.isnan(avg_gap) and avg_gap >= GAP_HEAVY_MEAN_GAP:
        return "gap_heavy"
    return "chop"


def label_all_regimes(panel: pd.DataFrame) -> dict:
    """Map decision date -> regime label, computed causally."""
    dates = sorted(panel["ts"].unique())
    return {d: label_regime(panel, d) for d in dates}


def episode_pool(panel: pd.DataFrame, regime_labels: dict, min_symbols: int = 8) -> list[dict]:
    """Build the pool of candidate episodes: one per Friday (or last trading day of
    each ISO week) where at least `min_symbols` have a bar that day.

    An episode spec is {'date': Timestamp, 'regime': str, 'symbols': [...]} — the
    market path is FIXED by the panel; only which episodes get sampled varies by seed.
    """
    panel = panel.copy()
    panel["week"] = panel["ts"].dt.to_period("W")
    out = []
    for week, g in panel.groupby("week"):
        friday = g["ts"].max()
        syms = g[g["ts"] == friday]["symbol"].tolist()
        if len(syms) < min_symbols:
            continue
        out.append({"date": friday, "regime": regime_labels.get(friday, "chop"), "symbols": sorted(syms)})
    return out


def sample_episodes(pool: list[dict], seed: int, per_regime: int, rng: random.Random | None = None) -> list[dict]:
    """Block-bootstrap `per_regime` episodes per regime, deterministically from `seed`.

    Identical (seed, pool) -> identical episode list, so paired tournaments compare
    genomes on the exact same market paths.
    """
    if rng is None:
        rng = random.Random(seed)
    by_regime: dict[str, list[dict]] = {}
    for ep in pool:
        by_regime.setdefault(ep["regime"], []).append(ep)
    out: list[dict] = []
    for regime in REGIMES:
        eps = by_regime.get(regime, [])
        for _ in range(per_regime):
            if not eps:
                continue
            ep = dict(rng.choice(eps))
            out.append(ep)
    out.sort(key=lambda e: e["date"])
    return out


def episode_seed_matrix(pool: list[dict], seeds: list[int], per_regime: int) -> dict[int, list[dict]]:
    """Precompute the episode list for every seed ONCE, so all genomes share paths."""
    return {s: sample_episodes(pool, s, per_regime) for s in seeds}

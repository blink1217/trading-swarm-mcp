"""Bar-based reimplementation of the three Finviz swing screens (plan decision 6).

This module is the SINGLE source of truth for screen predicates, used by BOTH
`gym/` (tier-A replay) and `services/analyst` (live candidates). Every input is
a `bars_1day`-derived feature (tier A): no Finviz Score, no news sentiment, no
earnings calendar — the Finviz strings are approximated by their technical
content only:

  momentum        "ta_pattern_channelup,sh_avgvol_o1000,sh_price_o5"
                  -> close near/above the trailing 20-day channel high,
                     positive 20-day momentum, and (when absolute data is
                     available) avg volume > 1M shares, price > $5.
  mean_reversion  "ta_rsi_os40,sh_avgvol_o500,ta_change_u-5"
                  -> RSI(14) < 40 with a negative 5-day move (< -5%).
  episodic_pivot  "ta_change_u3,sh_relvol_o1.5,earningsdate_thisweek"
                  -> +3% day on >1.5x relative volume at/near the channel high.
                     The `earningsdate_thisweek` leg is TIER B and is
                     deliberately NOT reimplemented on bars; the analyst adds it
                     live from the Finnhub calendar and the tape records the
                     weekly Jaccard agreement of the bar-based sets vs Finviz.

Genome knobs (`genome["screen"]`):
  momentum_quantile / reversion_quantile  cross-sectional confirmation gates
  vol_spike_ratio                         relative-volume leg of episodic pivot
  breakout_dist_min                       min distance to the 20d channel high
  min_cross_section                       min cohort size before quantile gates apply

Quantile gates only apply when the cohort is >= min_cross_section; below that,
absolute predicates alone decide (small universes must still be screenable).
"""
from __future__ import annotations

import pandas as pd

RSI_OVERSOLD = 40.0
REVERSION_5D_CHANGE = -0.05
PIVOT_DAY_CHANGE = 0.03
MIN_AVG_VOLUME_MOMENTUM = 1_000_000
MIN_AVG_VOLUME_REVERSION = 500_000
MIN_PRICE = 5.0

REQUIRED_COLS = ("close", "mom_5d", "mom_20d", "rsi_14", "vol_ratio_20", "breakout_dist_20d", "ret_1d")


def _enough_cohort(df: pd.DataFrame, genome: dict) -> bool:
    return len(df) >= int(genome.get("min_cross_section", 5))


def _have(df: pd.DataFrame, col: str) -> bool:
    return col in df.columns and df[col].notna().any()


def screen_momentum(df: pd.DataFrame, genome: dict) -> pd.Series:
    """Channel-up momentum: near/above the 20d high with positive 20d momentum."""
    scr = genome["screen"]
    mask = (df["breakout_dist_20d"] >= scr["breakout_dist_min"]) & (df["mom_20d"] > 0.0)
    if _have(df, "mom_5d") and _enough_cohort(df, genome):
        mask &= df["mom_5d"] >= df["mom_5d"].quantile(scr["momentum_quantile"])
    if _have(df, "avg_volume_20"):
        mask &= df["avg_volume_20"] >= MIN_AVG_VOLUME_MOMENTUM
    if _have(df, "close"):
        mask &= df["close"] >= MIN_PRICE
    return mask.fillna(False)


def screen_mean_reversion(df: pd.DataFrame, genome: dict) -> pd.Series:
    """Oversold snapback: RSI(14) < 40 with a down 5-day move."""
    scr = genome["screen"]
    mask = (df["rsi_14"] < RSI_OVERSOLD) & (df["mom_5d"] < REVERSION_5D_CHANGE)
    if _have(df, "vwap_stretch_20") and _enough_cohort(df, genome):
        mask &= df["vwap_stretch_20"] <= df["vwap_stretch_20"].quantile(1.0 - scr["reversion_quantile"])
    if _have(df, "avg_volume_20"):
        mask &= df["avg_volume_20"] >= MIN_AVG_VOLUME_REVERSION
    return mask.fillna(False)


def screen_episodic_pivot(df: pd.DataFrame, genome: dict) -> pd.Series:
    """Episodic pivot WITHOUT the earnings leg (tier B, added live by analyst)."""
    scr = genome["screen"]
    mask = (
        (df["ret_1d"] > PIVOT_DAY_CHANGE)
        & (df["vol_ratio_20"] > scr["vol_spike_ratio"])
        & (df["breakout_dist_20d"] >= scr["breakout_dist_min"])
    )
    return mask.fillna(False)


SCREENS = {
    "momentum": ("enable_momentum", screen_momentum),
    "mean_reversion": ("enable_mean_reversion", screen_mean_reversion),
    "episodic_pivot": ("enable_episodic_pivot", screen_episodic_pivot),
}


def apply_screens(df: pd.DataFrame, genome: dict) -> dict[str, pd.Series]:
    """Per-screen boolean masks over the cross-section `df` (index = symbol).

    Returns {screen_name: mask} for every ENABLED screen; callers OR them for
    membership and keep the per-screen masks for the Jaccard agreement check.
    """
    out: dict[str, pd.Series] = {}
    for name, (flag, fn) in SCREENS.items():
        if genome.get("screen", {}).get(flag):
            out[name] = fn(df, genome)
    return out


def union_mask(df: pd.DataFrame, genome: dict) -> pd.Series:
    """Membership in ANY enabled screen."""
    masks = apply_screens(df, genome)
    if not masks:
        return pd.Series(False, index=df.index)
    out = masks.popitem()[1]
    for m in masks.values():
        out = out | m
    return out


def jaccard(a: set, b: set) -> float:
    """Agreement metric recorded weekly on the tape (bar-based vs live Finviz)."""
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

"""Panel preparation and loading for the gym.

Input is `bars_1day`-shaped data: one row per (symbol, trading day) with
open/high/low/close/volume. `prepare_panel` computes the same technical features
used by the analyst (research_engine.py `_prepare_panel`) plus a forward 5-day
return used ONLY for evaluation (never as a decision feature).

EVERY decision-time feature uses trailing windows only (rolling/shift). The
no-lookahead invariant test poisons a panel with future bars and asserts the
causal features are unchanged (immunity) and that any decision row derived
from the poisoned future is rejected (detection).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_COLS = [
    "atr_14", "vwap_stretch_20", "vol_ratio_20", "mom_5d", "mom_20d",
    "gap_open", "breakout_dist_20d", "ret_1d", "atr_pct", "rsi_14",
    "avg_volume_20",
]


def load_bars_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["ts"])
    required = {"symbol", "ts", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"panel csv missing columns {sorted(missing)}")
    df = df.sort_values(["symbol", "ts"]).reset_index(drop=True)
    return df


def load_bars_bigquery(bq, project: str, dataset: str, min_ts: str | None = None) -> pd.DataFrame:
    where = f"WHERE ts >= '{min_ts}'" if min_ts else ""
    q = f"""
    SELECT symbol, ts, open, high, low, close, volume
    FROM `{project}.{dataset}.bars_1day`
    {where}
    ORDER BY symbol, ts
    """
    return bq.query(q).to_dataframe()


def prepare_panel(bars: pd.DataFrame) -> pd.DataFrame:
    """Compute causal technical features + evaluation-only forward return."""
    frames = []
    for symbol, g in bars.groupby("symbol"):
        g = g.sort_values("ts").reset_index(drop=True)
        if len(g) < 30:
            continue
        g["ret_1d"] = g["close"].pct_change()
        g["fwd_ret_5d"] = g["close"].shift(-5) / g["close"] - 1.0
        g["atr_14"] = (g["high"] - g["low"]).rolling(14).mean()
        g["atr_pct"] = g["atr_14"] / g["close"]
        g["vwap_20"] = (g["close"] * g["volume"]).rolling(20).sum() / g["volume"].rolling(20).sum()
        g["vwap_stretch_20"] = (g["close"] - g["vwap_20"]) / g["vwap_20"]
        g["vol_ratio_20"] = g["volume"] / g["volume"].rolling(20).mean()
        g["avg_volume_20"] = g["volume"].rolling(20).mean()
        g["mom_5d"] = g["close"] / g["close"].shift(5) - 1.0
        g["mom_20d"] = g["close"] / g["close"].shift(20) - 1.0
        g["gap_open"] = g["open"] / g["close"].shift(1) - 1.0
        g["rolling_high_20"] = g["high"].rolling(20).max().shift(1)
        g["breakout_dist_20d"] = (g["close"] - g["rolling_high_20"]) / g["close"]
        g["rsi_14"] = _rsi(g["close"], 14)
        g["symbol"] = symbol
        frames.append(g)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).rolling(n).mean()
    loss = (-delta.clip(upper=0.0)).rolling(n).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def decision_features(panel: pd.DataFrame, symbol: str, decision_date) -> pd.Series | None:
    """Causal feature row for `symbol` at `decision_date`.

    Returns a Series of decision-time features (plus the evaluation-only
    `fwd_ret_5d` and `ts`), or None if the symbol has no bar exactly at
    `decision_date`. All features are trailing-window values computed on bars with
    ts <= decision_date. This is THE function the no-lookahead guard checks.
    """
    g = panel[(panel["symbol"] == symbol) & (panel["ts"] <= pd.Timestamp(decision_date))]
    if g.empty:
        return None
    row = g.sort_values("ts").iloc[-1]
    if row["ts"] != pd.Timestamp(decision_date):
        return None
    return row[FEATURE_COLS + ["close", "fwd_ret_5d", "ts"]]


def assert_no_lookahead(panel: pd.DataFrame, symbol: str, decision_date, decision_row,
                        strict_panel: bool = False) -> list[str]:
    """Guard used by the invariant test: verify a decision row carries no future data.

    Returns a list of violation strings (empty == clean):
    - the decision row's timestamp must be exactly `decision_date`;
    - every feature must equal a FRESH causal recomputation at `decision_date`
      (a row derived from future data — a poisoned feed, a leaked join — will
      differ and be rejected);
    - with `strict_panel=True` (tape_replay decision-time snapshots) the panel
      itself must contain no bars strictly after `decision_date` for the symbol.

    The gym's full evaluation panel is NOT strict: it legitimately keeps future
    bars to compute the evaluation-only `fwd_ret_5d`; the causal-recomputation
    check is what proves no future value entered the decision features.
    """
    violations: list[str] = []
    t = pd.Timestamp(decision_date)
    if strict_panel:
        g = panel[(panel["symbol"] == symbol)]
        future = g[g["ts"] > t]
        if not future.empty:
            violations.append(f"panel contains {len(future)} bar(s) after decision date {t.date()} for {symbol}")
    clean = decision_features(panel, symbol, t)
    if clean is None:
        violations.append(f"no causal feature row available at {t.date()} for {symbol}")
        return violations
    if pd.Timestamp(decision_row["ts"]) != t:
        violations.append(f"decision row ts {decision_row['ts']} != decision date {t.date()}")
    for c in FEATURE_COLS:
        if c in decision_row.index and c in clean.index:
            a, b = float(decision_row[c]), float(clean[c])
            na_a, na_b = bool(np.isnan(a)), bool(np.isnan(b))
            if na_a != na_b or (not na_a and abs(a - b) > 1e-12):
                violations.append(f"feature {c} differs from causal computation at {t.date()} ({a} vs {b})")
        else:
            violations.append(f"feature {c} missing from decision row at {t.date()}")
    return violations

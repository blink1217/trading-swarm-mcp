"""Port of trading-swarm-alpha gym/tests/test_no_lookahead.py — the
poisoned-fixture test — against the vendored panel module."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from gym.panel import FEATURE_COLS, assert_no_lookahead, decision_features, prepare_panel

from helpers import synthetic_bars


def _bars_df():
    rows = synthetic_bars([f"S{i:02d}" for i in range(12)], days=260, seed=11,
                          end=dt.date(2019, 6, 28))
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def _panels():
    bars = _bars_df()
    return bars, prepare_panel(bars)


def _pick(panel):
    syms = sorted(panel["symbol"].unique())
    sym = syms[3]
    dates = sorted(panel[panel["symbol"] == sym]["ts"].unique())
    t = dates[len(dates) * 3 // 4]
    return sym, pd.Timestamp(t)


def test_causal_features_immune_to_future_poisoning():
    bars, panel = _panels()
    sym, t = _pick(panel)
    clean_row = decision_features(panel, sym, t)
    assert clean_row is not None

    poisoned = bars.copy()
    m = (poisoned["symbol"] == sym) & (poisoned["ts"] > t)
    assert m.sum() > 0, "fixture must have future bars to poison"
    poisoned.loc[m, "close"] = poisoned.loc[m, "close"] * 3.0
    poisoned.loc[m, "high"] = poisoned.loc[m, "high"] * 3.0
    poisoned.loc[m, "volume"] = poisoned.loc[m, "volume"] * 50
    poisoned_panel = prepare_panel(poisoned)

    poison_row = decision_features(poisoned_panel, sym, t)
    assert poison_row is not None
    for c in FEATURE_COLS:
        a, b = float(clean_row[c]), float(poison_row[c])
        na, nb = np.isnan(a), np.isnan(b)
        assert na == nb, f"{c}: NaN pattern changed under future poisoning"
        if not na:
            assert abs(a - b) < 1e-9, f"feature {c} leaked future data: {a} vs {b}"


def test_guard_rejects_row_from_the_future():
    bars, panel = _panels()
    sym, t = _pick(panel)
    g = panel[panel["symbol"] == sym].sort_values("ts")
    future_dates = g[g["ts"] > t]["ts"].unique()
    assert len(future_dates) >= 6
    leaked_row = g[g["ts"] == future_dates[5]].iloc[0]
    violations = assert_no_lookahead(panel, sym, t, leaked_row)
    assert violations, "guard accepted a decision row taken from a future date"
    assert any("ts" in v for v in violations)


def test_guard_rejects_poisoned_feature_values():
    bars, panel = _panels()
    sym, t = _pick(panel)
    row = decision_features(panel, sym, t)
    tampered = row.copy()
    tampered["mom_5d"] = float(tampered["mom_5d"]) + 0.5 if not np.isnan(tampered["mom_5d"]) else 0.5
    violations = assert_no_lookahead(panel, sym, t, tampered)
    assert any("mom_5d" in v for v in violations)


def test_guard_strict_mode_flags_future_bars():
    bars, panel = _panels()
    sym, t = _pick(panel)
    row = decision_features(panel, sym, t)
    assert assert_no_lookahead(panel, sym, t, row, strict_panel=False) == []
    strict = assert_no_lookahead(panel, sym, t, row, strict_panel=True)
    assert any("after decision date" in v for v in strict)


def test_clean_causal_row_passes():
    bars, panel = _panels()
    sym, t = _pick(panel)
    row = decision_features(panel, sym, t)
    assert assert_no_lookahead(panel, sym, t, row) == []

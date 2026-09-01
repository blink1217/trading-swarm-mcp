"""Port of trading-swarm-alpha gym/tests/test_tier_refusal.py — the gym NEVER
scores a genome whose mutated genes exceed tier A (never neutral-fills B/C)."""
from __future__ import annotations

import datetime as dt

import pytest

from gym.regime import episode_pool, episode_seed_matrix, label_all_regimes
from gym.simulator import (
    TierScoringRefusal,
    _heuristic_filter_score,
    assert_tier_a_scortable,
    evaluate_genome,
)

from genomes import baseline, mutate_a, mutate_b, mutate_c, mutate_prompt
from helpers import synthetic_bars
from swarm_mcp.tools.gym_tools import _panel_from_bars


def _panel_and_matrix():
    rows = synthetic_bars([f"S{i:02d}" for i in range(20)], days=420, seed=5,
                          end=dt.date(2019, 12, 31))
    panel = _panel_from_bars(rows)
    labels = label_all_regimes(panel)
    pool = episode_pool(panel, labels, min_symbols=5)
    matrix = episode_seed_matrix(pool, [0, 1], 2)
    return panel, matrix


def test_baseline_measurement_without_champion_is_allowed():
    panel, matrix = _panel_and_matrix()
    run = evaluate_genome(panel, matrix, baseline())
    assert run["fidelity"].startswith("tier-A")
    assert run["lookahead_violations"] == []


def test_tier_a_only_challenger_scores():
    panel, matrix = _panel_and_matrix()
    champ = baseline()
    run = evaluate_genome(panel, matrix, mutate_a(champ), champion_genome=champ)
    assert run["n_episodes"] >= 0


def test_tier_b_challenger_refused():
    panel, matrix = _panel_and_matrix()
    champ = baseline()
    with pytest.raises(TierScoringRefusal) as ei:
        evaluate_genome(panel, matrix, mutate_b(champ), champion_genome=champ)
    assert "UNSCORABLE" in str(ei.value)
    assert "tier B" in str(ei.value)


def test_tier_c_challenger_refused():
    panel, matrix = _panel_and_matrix()
    champ = baseline()
    with pytest.raises(TierScoringRefusal):
        evaluate_genome(panel, matrix, mutate_c(champ), champion_genome=champ)


def test_prompt_variant_challenger_refused():
    panel, matrix = _panel_and_matrix()
    champ = baseline()
    with pytest.raises(TierScoringRefusal):
        evaluate_genome(panel, matrix, mutate_prompt(champ), champion_genome=champ)


def test_identical_challenger_not_refused():
    panel, matrix = _panel_and_matrix()
    champ = baseline()
    assert_tier_a_scortable(baseline(), champ)


def test_heuristic_score_uses_no_tier_b_c_features():
    feats_a = {"vol_ratio_20": 1.8, "mom_20d": 0.08, "breakout_dist_20d": 0.0, "rsi_14": 55.0}
    feats_b = dict(feats_a, finnhub_sentiment=0.9, earnings_flag=1.0, finviz_score=88.0)
    assert _heuristic_filter_score(feats_a) == _heuristic_filter_score(feats_b)

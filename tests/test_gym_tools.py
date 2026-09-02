"""swarm-gym-mcp tools: never promote, seed caps, tier refusal, underpowered
labels, determinism on identical paths."""
from __future__ import annotations

from gates import REGIMES
from genome_schema import genome_hash
from helpers import run_async, synthetic_bars
from swarm_mcp import plans
from swarm_mcp.tools import gym_tools

from genomes import baseline, mutate_a, mutate_b

SYMS = [f"S{i:02d}" for i in range(20)]


def _rows(days=500, seed=9):
    return synthetic_bars(SYMS, days=days, seed=seed)


def test_label_regimes_reports_counts_and_depth():
    r = run_async(gym_tools.label_regimes(bars=_rows(), min_symbols=5))
    assert "error" not in r
    assert set(r["regime_counts"]) == set(REGIMES)
    assert r["pool_size"] > 0
    assert r["labels_are_causal"]
    assert "depth" in r and "local_depth_weeks" in r["depth"]


def test_probe_fragility_baseline_measurement_never_promotes():
    r = run_async(gym_tools.probe_fragility(baseline(), seeds=[0, 1, 2, 3], per_regime=2,
                                            bars=_rows(), min_symbols=5))
    assert "error" not in r
    assert r["verdict"].startswith("FRAGILITY_REPORT")
    assert r["fidelity"].startswith("tier-A")
    assert isinstance(r["hard_constraint_violations"], list)
    assert r["worst_regime"] in REGIMES
    assert r["underpowered"] is True
    assert "MIN_EPISODES" in r["power_note"]
    assert r["cloud_job"]["body"]["challenger_id"] == genome_hash(baseline())


def test_probe_fragility_seed_cap_enforced():
    r = run_async(gym_tools.probe_fragility(baseline(), seeds=list(range(9)), bars=_rows(),
                                            min_symbols=5))
    assert "error" in r and "seed cap" in r["error"].lower()


def test_probe_fragility_per_regime_cap_enforced():
    r = run_async(gym_tools.probe_fragility(baseline(), seeds=[0], per_regime=3,
                                            bars=_rows(), min_symbols=5))
    assert "error" in r and "per_regime" in r["error"]


def test_probe_fragility_invalid_genome_rejected():
    g = baseline()
    g["risk"]["risk_pct_per_trade"] = 99.0
    r = run_async(gym_tools.probe_fragility(g, bars=_rows(), min_symbols=5))
    assert r["valid_genome"] is False
    assert r["errors"]


def test_probe_fragility_tier_b_diff_refused():
    champ = baseline()
    r = run_async(gym_tools.probe_fragility(mutate_b(champ), champion_genome=champ,
                                            seeds=[0, 1], bars=_rows(), min_symbols=5))
    assert r["verdict"] == "UNSCORABLE"
    assert any("tier B" in v for v in r["audit_request"]["violation_summary"])
    assert "audit_request" in r and "cloud_job" in r


def test_paired_preview_never_promotes_and_is_deterministic():
    champ, chal = baseline(), mutate_a()
    rows = _rows()
    r1 = run_async(gym_tools.paired_preview(champ, chal, seeds=list(range(8)), per_regime=2,
                                            bars=rows, min_symbols=5))
    assert "error" not in r1
    assert r1["verdict"].startswith("UNDERPOWERED_PREVIEW")
    assert r1["promotion"] is None
    for stat in ("mean_delta_bps", "paired_p_value", "delta_ci_95",
                 "worst_regime_champion", "worst_regime_challenger"):
        assert "UNDERPOWERED" in r1["statistics"][stat]["power"]
    assert "MIN_EPISODES" in r1["gate_requirements"]["min_episodes"]
    assert "CSCV" in r1["gate_requirements"]["pbo"]
    assert "server-side" in r1["gate_requirements"]["dsr_margin"]
    assert r1["cloud_job"]["body"]["challenger_id"] == genome_hash(chal)

    r2 = run_async(gym_tools.paired_preview(champ, chal, seeds=list(range(8)), per_regime=2,
                                            bars=rows, min_symbols=5))
    assert r1["statistics"]["mean_delta_bps"]["value"] == r2["statistics"]["mean_delta_bps"]["value"]
    assert r1["statistics"]["paired_p_value"]["value"] == r2["statistics"]["paired_p_value"]["value"]


def test_paired_preview_tier_b_challenger_unscorable():
    r = run_async(gym_tools.paired_preview(baseline(), mutate_b(), seeds=[0, 1], per_regime=2,
                                           bars=_rows(), min_symbols=5))
    assert r["verdict"] == "UNSCORABLE"
    assert "audit_request" in r and "cloud_job" in r


def test_estimate_cloud_run_shapes_the_handoff():
    r = run_async(gym_tools.estimate_cloud_run(seeds=[0, 1, 2, 3, 4], per_regime=4, n_genomes=2))
    assert "error" not in r
    assert r["episodes_required"] == len(REGIMES) * 4 * 5 * 2
    assert r["budget"]["monthly_cap_usd"] == 150.0
    assert r["budget"]["breaker_fraction"] == 0.80
    body = r["submit_bodies"]["tournament.submit"]
    assert body["hosted_geometry"]["panel"] == "bars_1day"
    assert body["hosted_geometry"]["per_regime"] == plans.TOURNAMENT["per_regime"]
    # Rate card: hosted episodes are 1 credit each; the tournament is a fixed price
    # for the full paired geometry, and the Starter pack affords >= 40 of them.
    assert r["credits"]["hosted_gym_episode"] == plans.COMPUTE_RATES["hosted.episode"]
    assert r["credits"]["this_geometry_on_hosted_gym"] == r["episodes_required"]
    assert r["credits"]["shadow_tournament_full"] == plans.TOURNAMENT["credits_full"] == 200
    assert r["credits"]["shadow_tournament_contribute"] == plans.TOURNAMENT["credits_contribute"] == 100
    assert plans.CREDIT_PACKS["credits-10k"]["calls"] // plans.TOURNAMENT["credits_full"] >= 40


def test_paired_stats_reports_worst_regime_margin():
    from swarm_mcp.tools.gym_tools import paired_stats

    champ = {"episodes": [{"date": "2026-01-02", "regime": "chop", "weekly_net_bps": 1.0},
                          {"date": "2026-01-09", "regime": "melt_up", "weekly_net_bps": 5.0}]}
    chal = {"episodes": [{"date": "2026-01-02", "regime": "chop", "weekly_net_bps": 3.0},
                         {"date": "2026-01-09", "regime": "melt_up", "weekly_net_bps": 4.0}]}
    s = paired_stats(champ, chal)
    assert s["n_paired_episodes"] == 2
    assert s["deltas"] == [2.0, -1.0]
    assert s["per_regime_delta_bps"]["chop"] == 2.0
    assert s["per_regime_delta_bps"]["melt_up"] == -1.0
    assert s["worst_regime_margin_bps"] == -1.0
    assert s["paired_p_value"] is None, "fewer than 5 deltas: no p-value, never a fake one"

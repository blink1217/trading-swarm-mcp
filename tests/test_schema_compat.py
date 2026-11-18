"""Schema v2/v3 compatibility and the house-cost exploit regression.

Schema v3 (upstream guardrails A-01) is field-identical to v2; only the
``schema_version`` const moved and the execution cost genes are ignored by the
gym (house constants). These tests pin the three contracts that make the
transition invisible to v2 customers and un-exploitable for everyone:

1. a v2 submission validates and evaluates everywhere (tools, runner);
2. the canonical genome_hash of a v2 genome is unchanged (the site cross-checks);
3. zeroing the cost genes can no longer manufacture a win (the v2-era exploit).
"""
from __future__ import annotations

import copy
import json

import pytest

from gates import REGIMES
from genome_schema import genome_hash
from gym.simulator import evaluate_genome
from swarm_mcp import genome_compat, plans, tournament_runner
from swarm_mcp.tools import gym_tools, tournament_tools, warden_tools

from helpers import run_async, synthetic_bars
from genomes import baseline, mutate_a

SYMS = [f"S{i:02d}" for i in range(20)]


def _zero_cost_genes(genome: dict) -> dict:
    g = copy.deepcopy(genome)
    g["execution"]["spread_bps"] = 0.0
    g["execution"]["slippage_bps"] = 0.0
    g["execution"]["adverse_selection_bps"] = 0.0
    return g


# --- normalization ---------------------------------------------------------------

def test_v2_genome_normalizes_to_v3_fields_unchanged():
    v2 = baseline()  # tests/genomes.py baseline is the v2-era fixture
    assert v2["schema_version"] == 2
    norm = genome_compat.normalize(v2)
    assert norm["schema_version"] == 3
    stripped = {k: v for k, v in norm.items() if k != "schema_version"}
    orig_stripped = {k: v for k, v in v2.items() if k != "schema_version"}
    assert stripped == orig_stripped
    assert v2["schema_version"] == 2, "normalize must not mutate the input"


def test_v2_genome_validates_and_hashes_unchanged():
    v2 = baseline()
    norm, errors = genome_compat.normalize_for_validation(v2)
    assert errors == []
    assert genome_compat.normalize_for_validation(norm)[1] == []
    # independent reimplementation of the pre-vendoring canonical hash contract
    import hashlib
    import json as _json

    expected = hashlib.sha256(
        _json.dumps(v2, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    assert genome_hash(v2) == expected


def test_v5_genome_rejected():
    g = baseline()
    g["schema_version"] = 5
    norm, errors = genome_compat.normalize_for_validation(g)
    assert norm is None and errors and "schema_version" in errors[0]
    with pytest.raises(ValueError):
        genome_compat.normalize(g)


def test_warden_validates_v2_and_v4_genome():
    r = run_async(warden_tools.validate_genome_tool(baseline()))
    assert r["valid"] is True, r["errors"]
    assert r["evaluated_schema_version"] == 4
    v4 = baseline()
    v4["schema_version"] = 4
    r4 = run_async(warden_tools.validate_genome_tool(v4))
    assert r4["valid"] is True, r4["errors"]
    assert r4["evaluated_schema_version"] == 4
    v5 = baseline()
    v5["schema_version"] = 5
    r5 = run_async(warden_tools.validate_genome_tool(v5))
    assert r5["valid"] is False


def test_gym_tools_accept_v2():
    rows = synthetic_bars(SYMS, days=500, seed=9)
    probe = run_async(gym_tools.probe_fragility(baseline(), seeds=[0, 1], per_regime=1, bars=rows,
                                                min_symbols=5))
    assert "error" not in probe, probe.get("error")
    preview = run_async(gym_tools.paired_preview(baseline(), mutate_a(baseline()),
                                                 seeds=[0, 1], per_regime=1, bars=rows,
                                                 min_symbols=5))
    assert "error" not in preview, preview.get("error")


def test_runner_load_champion_accepts_v2_champion(monkeypatch, tmp_path):
    import json
    import os

    path = tmp_path / "champion_v2.json"
    path.write_text(json.dumps(baseline()), encoding="utf-8")
    monkeypatch.setenv("SWARM_MCP_CHAMPION_GENOME", str(path))
    champ = tournament_runner.load_champion()
    assert champ["schema_version"] == 2


def test_submit_sends_v2_vector_as_submitted(monkeypatch):
    """v2 genome: locally valid; the payload carries the ORIGINAL v2 dict + hash."""
    import httpx

    calls: list[dict] = []

    class _FakeAsync:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})

            class R:
                status_code = 202
                text = '{"ok": true}'

                def json(nonself):
                    return {"ok": True, "job_id": "st_" + "b" * 24}
            return R()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsync)
    v2 = baseline()
    r = run_async(tournament_tools.submit(v2, contribute=True))
    assert r["genome_hash"] == genome_compat.genome_hash_canonical(v2), r
    assert "error" not in r and "errors" not in r, r
    assert calls and calls[0]["json"]["genome"] == v2
    assert "tournament/submit" in calls[0]["url"]


def test_canonical_hash_is_number_form_independent():
    """2.0 and 2 hash identically across languages; the site cross-check must
    hold for clients that write Integral floats (the JS canonical rule)."""
    import hashlib

    g = baseline()
    float_form = copy.deepcopy(g)
    float_form["risk"]["atr_sizing_mult"] = 1.0
    int_form = copy.deepcopy(float_form)
    int_form["risk"]["atr_sizing_mult"] = 1
    assert genome_compat.genome_hash_canonical(float_form) == \
        genome_compat.genome_hash_canonical(int_form)
    # equals the site's canonicalGenomeHash rule (sorted keys, compact JSON,
    # integral floats collapsed) reimplemented independently:
    def js_canon(v):
        if isinstance(v, dict):
            return {k: js_canon(v[k]) for k in sorted(v)}
        if isinstance(v, list):
            return [js_canon(x) for x in v]
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v
    expected = hashlib.sha256(
        json.dumps(js_canon(float_form), sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True).encode("utf-8")).hexdigest()
    assert genome_compat.genome_hash_canonical(float_form) == expected
    assert genome_hash(float_form) != expected, "the vendored python hash is NOT the wire canonical"


def test_submit_local_validation_rejects_v5_before_sending(monkeypatch):
    import httpx

    def _boom(*a, **k):
        raise AssertionError("nothing may be sent for an unsupported schema")

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    g = baseline()
    g["schema_version"] = 5
    r = run_async(tournament_tools.submit(g))
    assert r["valid_genome"] is False and r["errors"]

# --- the zero-cost exploit is dead --------------------------------------------------

def test_zero_cost_genes_cannot_change_outcome():
    # v2-era: each genome paid its own declared costs, so a challenger with all
    # costs at 0 beat the champion by construction (board entry 00dcdb446a4e).
    # A-01: the gym charges immutable house constants; identical strategy genes
    # with optimistic cost genes must produce an identical, INCONCLUSIVE score.
    panel = gym_tools._panel_from_bars(synthetic_bars(SYMS, days=900, seed=3))
    champion = baseline()
    seeds = list(plans.TOURNAMENT["seeds"])
    per_regime = plans.TOURNAMENT["per_regime"]
    champ_norm = genome_compat.normalize(champion)
    exploiter = _zero_cost_genes(champion)
    assert exploiter != champion
    r = tournament_runner.paired_outcome(panel, champion, exploiter, seeds, per_regime,
                                         champion_eval=champ_norm,
                                         challenger_eval=genome_compat.normalize(exploiter))
    assert r["mean_delta_bps"] == 0.0, f"cost genes changed the outcome: {r['mean_delta_bps']}"
    assert r["outcome"] == tournament_runner.OUTCOME_INCONCLUSIVE
    # sanity: the house round trip that replaces the cost genes is non-zero
    from fill_model import house_round_trip_cost_bps  # vendored guardrails

    assert house_round_trip_cost_bps() == 24.0


def test_hold_days_and_weekend_gate_change_weekly_net_bps_and_gene_coverage_is_complete():
    from swarm_mcp import gym_v4
    from gym.regime import episode_pool, episode_seed_matrix, label_all_regimes

    # 1. Exhaustive schema gene partition check
    leaves = gym_v4.schema_leaf_gene_paths()
    covered = gym_v4.SCORED_TIER_A_GENES | gym_v4.TIER_GATED_GENES | gym_v4.UNSCORED_GENES
    assert leaves == covered, f"Schema gene coverage drift: missing={leaves - covered}, extra={covered - leaves}"
    assert (gym_v4.SCORED_TIER_A_GENES & gym_v4.UNSCORED_GENES) == set()

    # 2. Changing risk.hold_days (2 -> 10) actively changes weekly_net_bps under house_v4
    panel = gym_tools._panel_from_bars(synthetic_bars(SYMS, days=500, seed=17))
    labels = label_all_regimes(panel)
    pool = episode_pool(panel, labels, min_symbols=5)
    matrix = episode_seed_matrix(pool, [0, 1], per_regime=2)

    g_hold2 = genome_compat.normalize(baseline())
    g_hold2["risk"]["hold_days"] = 2
    g_hold10 = copy.deepcopy(g_hold2)
    g_hold10["risk"]["hold_days"] = 10

    r2 = gym_v4.evaluate_genome_v4(panel, matrix, g_hold2, [0, 1])
    r10 = gym_v4.evaluate_genome_v4(panel, matrix, g_hold10, [0, 1], champion_genome=g_hold2)
    assert r2["cost_model"] == "house_v4"
    assert r2["evaluated_schema_version"] == 4
    assert r2["weekly_net_bps"] != r10["weekly_net_bps"], "hold_days 2 vs 10 must alter weekly_net_bps"

    # 3. Tightening weekend_gate.* actively changes weekly_net_bps
    g_wg_strict = copy.deepcopy(g_hold2)
    g_wg_strict["weekend_gate"]["max_atr_pct"] = 0.005
    g_wg_strict["weekend_gate"]["trim_threshold"] = 0.70
    g_wg_strict["weekend_gate"]["flatten_threshold"] = 0.45
    r_wg = gym_v4.evaluate_genome_v4(panel, matrix, g_wg_strict, [0, 1], champion_genome=g_hold2)
    assert r_wg["weekly_net_bps"] != r2["weekly_net_bps"], "weekend_gate.* must alter weekly_net_bps"

    # 4. Unscored-only changes raise NoScoredChangeError
    g_noop = _zero_cost_genes(g_hold2)
    g_noop["risk"]["atr_period"] = 25
    with pytest.raises(gym_v4.NoScoredChangeError):
        gym_v4.assert_has_scored_change(g_noop, g_hold2)


"""End-to-end verification suite for the Flagship Quant Swarm Audit (`audit.*`)
workflow, canonical demo fixtures (`fixture_flawed`, `fixture_clean`,
`fixture_inconclusive`), idempotency/spend-cap/approval-token gates, and
`warden.promotion_verdict` readiness semantics."""
from __future__ import annotations

from pathlib import Path

from genomes import baseline, mutate_b
from helpers import run_async
from swarm_mcp.servers import data_server, gym_server, warden_server
from swarm_mcp.tools import audit_tools, warden_tools

ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = ROOT / "examples" / "audit_demo"

REQUIRED_FINDING_KEYS = {
    "finding",
    "category",
    "evidence",
    "why_it_matters",
    "next_experiment",
    "limitation",
}


def setup_function() -> None:
    audit_tools.reset_audit_stores()


def test_canonical_fixtures_end_to_end(tmp_path):
    from examples.audit_demo.run_demo import build_fixtures

    paths = build_fixtures(DEMO_DIR)
    assert (paths["fixture_flawed"] / "returns.csv").exists()
    assert (paths["fixture_clean"] / "returns.csv").exists()
    assert (paths["fixture_inconclusive"] / "returns.csv").exists()

    # --- 1. Flawed fixture ----------------------------------------------------
    pf_flawed = run_async(audit_tools.preflight(
        returns_path=str(paths["fixture_flawed"] / "returns.csv"),
        trades_path=str(paths["fixture_flawed"] / "trades.csv"),
        cost_assumptions_path=str(paths["fixture_flawed"] / "cost_assumptions.json"),
        features_manifest_path=str(paths["fixture_flawed"] / "features_manifest.json"),
        strategy_name="flawed_backtest",
    ))
    assert pf_flawed["eligible"] is True
    assert pf_flawed["credits_charged"] == 0
    cov_f = pf_flawed["coverage_matrix"]
    assert cov_f["outbound_data_preview"]["local_mode"]["bytes_sent"] == 0
    assert any(c["check_id"] == "strategy_code_lookahead_proof" for c in cov_f["not_tested_checks"])
    assert any(c["check_id"] == "benchmark_excess_and_beta" for c in cov_f["not_tested_checks"])

    q_flawed = run_async(audit_tools.quote(preflight_id=pf_flawed["preflight_id"], mode="local"))
    assert q_flawed["credits_cost"] == 0
    assert q_flawed["gbp_cost"]["starter_pack_gbp"] == 0.0
    assert q_flawed["approval_token"].startswith("aq1_")

    run_flawed = run_async(audit_tools.run(
        preflight_id=pf_flawed["preflight_id"],
        approval_token=q_flawed["approval_token"],
        idempotency_key="key-flawed-1",
        max_credits=0,
        mode="local",
    ))
    assert run_flawed["status"] == "completed"
    assert run_flawed["overall_assessment"] == "PROBLEMS_OBSERVED"
    assert run_flawed["counts_by_category"]["OBSERVED_PROBLEM"] >= 3
    assert run_flawed["credits_charged"] == 0

    st_flawed = run_async(audit_tools.status(run_flawed["job_id"]))
    assert st_flawed["status"] == "completed"
    assert st_flawed["overall_assessment"] == "PROBLEMS_OBSERVED"
    assert st_flawed["credits_charged"] == 0

    rep_flawed = run_async(audit_tools.report(
        job_id=run_flawed["job_id"],
        output_html_path=str(tmp_path / "flawed.html"),
        output_json_path=str(tmp_path / "flawed.json"),
    ))
    assert (tmp_path / "flawed.html").exists()
    assert (tmp_path / "flawed.json").exists()
    for f in rep_flawed["findings"]:
        assert REQUIRED_FINDING_KEYS <= set(f.keys())
        assert f["category"] in audit_tools.DIAGNOSTIC_CATEGORIES

    # --- 2. Clean fixture -----------------------------------------------------
    pf_clean = run_async(audit_tools.preflight(
        returns_path=str(paths["fixture_clean"] / "returns.csv"),
        trades_path=str(paths["fixture_clean"] / "trades.csv"),
        benchmark_path=str(paths["fixture_clean"] / "benchmark.csv"),
        cost_assumptions_path=str(paths["fixture_clean"] / "cost_assumptions.json"),
        features_manifest_path=str(paths["fixture_clean"] / "features_manifest.json"),
        strategy_name="clean_backtest",
    ))
    q_clean = run_async(audit_tools.quote(preflight_id=pf_clean["preflight_id"], mode="local"))
    run_clean = run_async(audit_tools.run(
        preflight_id=pf_clean["preflight_id"],
        approval_token=q_clean["approval_token"],
        idempotency_key="key-clean-1",
        max_credits=0,
        mode="local",
    ))
    assert run_clean["overall_assessment"] == "PASSED_LOCAL_AUDIT"
    assert run_clean["counts_by_category"]["OBSERVED_PROBLEM"] == 0
    assert run_clean["counts_by_category"]["SENSITIVITY_WARNING"] == 0
    assert run_clean["counts_by_category"]["PASSED"] == 6
    assert run_clean["counts_by_category"]["NOT_TESTED"] == 1  # strategy code boundary

    # --- 3. Inconclusive fixture ----------------------------------------------
    pf_inc = run_async(audit_tools.preflight(
        returns_path=str(paths["fixture_inconclusive"] / "returns.csv"),
        trades_path=str(paths["fixture_inconclusive"] / "trades.csv"),
        strategy_name="inconclusive_backtest",
    ))
    q_inc = run_async(audit_tools.quote(preflight_id=pf_inc["preflight_id"], mode="local"))
    run_inc = run_async(audit_tools.run(
        preflight_id=pf_inc["preflight_id"],
        approval_token=q_inc["approval_token"],
        idempotency_key="key-inc-1",
        max_credits=0,
        mode="local",
    ))
    assert run_inc["overall_assessment"] == "INSUFFICIENT_EVIDENCE"
    assert run_inc["counts_by_category"]["INSUFFICIENT_EVIDENCE"] >= 3

    # --- 4. Compare flawed vs clean -------------------------------------------
    comp_html = tmp_path / "compare.html"
    comp = run_async(audit_tools.compare(
        job_id_a=run_flawed["job_id"],
        job_id_b=run_clean["job_id"],
        output_html_path=str(comp_html),
    ))
    assert comp["remediation_verdict"] == "REMEDIATION_VERIFIED"
    assert len(comp["resolved_findings"]) >= 3
    assert len(comp["new_warnings"]) == 0
    assert comp["metrics_diff"]["breakeven_cost_multiplier"]["delta"] > 1.0
    assert comp_html.exists() and "REMEDIATION_VERIFIED" in comp_html.read_text(encoding="utf-8")


def test_idempotency_spend_caps_and_approval_token_gates():
    rows = [{"ts": f"2026-06-{d:02d}", "strategy_return": 0.002} for d in range(1, 25)]
    pf = run_async(audit_tools.preflight(returns_rows=rows, strategy_name="gate_test"))
    q_local = run_async(audit_tools.quote(preflight_id=pf["preflight_id"], mode="local"))
    q_hosted = run_async(audit_tools.quote(preflight_id=pf["preflight_id"], mode="hosted"))
    assert q_hosted["credits_cost"] == audit_tools.HOSTED_AUDIT_CREDITS

    # 1. Spend cap refusal on hosted mode when max_credits < 20
    refused_cap = run_async(audit_tools.run(
        preflight_id=pf["preflight_id"],
        approval_token=q_hosted["approval_token"],
        idempotency_key="cap-test-1",
        max_credits=5,
        mode="hosted",
    ))
    assert refused_cap["status"] == "SPEND_CAP_EXCEEDED"
    assert refused_cap["credits_charged"] == 0

    # 2. Tampered approval_token refusal
    refused_tok = run_async(audit_tools.run(
        preflight_id=pf["preflight_id"],
        approval_token="aq1_00000000000000000000000000000000",
        idempotency_key="tok-test-1",
        max_credits=0,
        mode="local",
    ))
    assert refused_tok["status"] == "APPROVAL_TOKEN_INVALID"
    assert refused_tok["credits_charged"] == 0

    # 3. Valid run + idempotent retry returns cached result without re-running
    r1 = run_async(audit_tools.run(
        preflight_id=pf["preflight_id"],
        approval_token=q_local["approval_token"],
        idempotency_key="idem-key-alpha",
        max_credits=0,
        mode="local",
    ))
    assert r1["status"] == "completed"
    assert r1["idempotent_replay"] is False

    r2 = run_async(audit_tools.run(
        preflight_id=pf["preflight_id"],
        approval_token=q_local["approval_token"],
        idempotency_key="idem-key-alpha",
        max_credits=0,
        mode="local",
    ))
    assert r2["job_id"] == r1["job_id"]
    assert r2["idempotent_replay"] is True
    assert r2["credits_charged"] == 0

    # 4. Reusing idempotency_key with different inputs fails with conflict
    pf2 = run_async(audit_tools.preflight(
        returns_rows=[{"ts": "2026-06-01", "strategy_return": -0.01}],
        strategy_name="different_strategy",
    ))
    q2 = run_async(audit_tools.quote(preflight_id=pf2["preflight_id"], mode="local"))
    conflict = run_async(audit_tools.run(
        preflight_id=pf2["preflight_id"],
        approval_token=q2["approval_token"],
        idempotency_key="idem-key-alpha",
        max_credits=0,
        mode="local",
    ))
    assert conflict["status"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert conflict["credits_charged"] == 0


def test_preflight_refusal_never_charges_credits():
    # Duplicate timestamps must fail preflight and refuse quote/run at 0 credits
    dup_rows = [
        {"ts": "2026-06-01", "strategy_return": 0.01},
        {"ts": "2026-06-01", "strategy_return": 0.02},
    ]
    pf = run_async(audit_tools.preflight(returns_rows=dup_rows))
    assert pf["eligible"] is False
    assert any("duplicate timestamp" in e for e in pf["validation_errors"])

    q = run_async(audit_tools.quote(preflight_id=pf["preflight_id"]))
    assert q["status"] == "PREFLIGHT_REFUSED"
    assert q["credits_charged"] == 0

    r = run_async(audit_tools.run(
        preflight_id=pf["preflight_id"],
        approval_token="aq1_any",
        idempotency_key="dup-run-1",
        max_credits=100,
    ))
    assert r["status"] == "PREFLIGHT_REFUSED"
    assert r["credits_charged"] == 0


def test_promotion_verdict_deterministic_readiness_states():
    champ = baseline()
    # 1. Valid Tier-A genome -> READY_FOR_HOSTED_TOURNAMENT, 0 credits
    r_ready = run_async(warden_tools.request_promotion_verdict(champ, champion_genome=champ))
    assert r_ready["verdict"] == "INDETERMINATE_LOCAL"
    assert r_ready["valid_genome"] is True
    assert r_ready["readiness_status"] == "READY_FOR_HOSTED_TOURNAMENT"
    assert r_ready["credits_charged"] == 0

    # 2. Tier-B mutated genome -> BLOCKED_TIER_B_C_REQUIRES_TAPE, 0 credits
    r_tier_b = run_async(warden_tools.request_promotion_verdict(mutate_b(champ), champion_genome=champ))
    assert r_tier_b["verdict"] == "INDETERMINATE_LOCAL"
    assert r_tier_b["highest_mutated_tier"] == "B"
    assert r_tier_b["readiness_status"] == "BLOCKED_TIER_B_C_REQUIRES_TAPE"
    assert r_tier_b["credits_charged"] == 0

    # 3. Invalid genome schema -> BLOCKED_SCHEMA_INVALID, 0 credits
    r_bad = run_async(warden_tools.request_promotion_verdict({"schema_version": 3}))
    assert r_bad["verdict"] == "INDETERMINATE_LOCAL"
    assert r_bad["valid_genome"] is False
    assert r_bad["readiness_status"] == "BLOCKED_SCHEMA_INVALID"
    assert r_bad["credits_charged"] == 0


def test_all_three_servers_register_expected_tools():
    data_names = {t.name for t in run_async(data_server.mcp.list_tools())}
    warden_names = {t.name for t in run_async(warden_server.mcp.list_tools())}
    gym_names = {t.name for t in run_async(gym_server.mcp.list_tools())}

    assert {
        "audit.preflight",
        "audit.quote",
        "audit.run",
        "audit.status",
        "audit.report",
        "audit.compare",
    } <= warden_names
    assert len(data_names) == 17
    assert len(warden_names) == 14
    assert len(gym_names) == 8
    assert len(data_names | warden_names | gym_names) == 39

"""Unit tests for the Phase 1-3 roadmap upgrades across swarm-data-mcp,
swarm-warden-mcp, and swarm-gym-mcp."""
from __future__ import annotations

import json

from helpers import run_async, synthetic_bars
from swarm_mcp.cache.db import get_db
from swarm_mcp.tools import alt_derived_tools, data_tools, gym_tools, warden_tools


def _no_raw_ohlcv(payload: dict) -> None:
    blob = json.dumps(payload)
    for key in ('"open"', '"high"', '"low"', '"close"'):
        assert key not in blob, f"raw field {key} leaked into output: {blob}"


# ---------------------------------------------------------------------------
# Phase 1: De-stubbed Data Tools & New PIT Derived Tools
# ---------------------------------------------------------------------------

def test_volume_forecast_phase_space_and_chaos_metrics(tmp_cache):
    ticker = "AAA"
    rows = synthetic_bars([ticker], days=320, seed=41)
    get_db().upsert_bars("alpaca", "1Day", "split", rows)

    out1 = run_async(data_tools.volume_forecast(symbol=ticker, horizon_bars=1))
    out5 = run_async(data_tools.volume_forecast(symbol=ticker, horizon_bars=5))

    assert out1["tool"] == "volume.forecast"
    assert out1["horizon_bars"] == 1
    assert out5["horizon_bars"] == 5
    assert 0.0 <= out1["hurst_exponent"] <= 1.0
    assert isinstance(out1["lyapunov_exponent"], float)
    assert isinstance(out1["is_chaotic"], bool)
    assert 0.0 <= out1["buy_volume_attractor_share"] <= 1.0
    assert out1["interval_p10_p90"][0] <= out1["interval_p10_p90"][1]
    assert out1["interval_p10_p90"] != out5["interval_p10_p90"]
    _no_raw_ohlcv(out1)


def test_sentiment_and_features_build_pit_polarity(tmp_cache):
    ticker = "AAA"
    rows = synthetic_bars([ticker], days=320, seed=43)
    db = get_db()
    db.upsert_bars("alpaca", "1Day", "split", rows)
    as_of = str(rows[-1]["ts"])[:10]

    # 1. Without enrichment -> altdata_sentiment is UNSCORABLE
    feat_missing = run_async(data_tools.build_features(symbol=ticker, as_of=as_of))
    assert any(v.startswith("altdata_sentiment:") for v in feat_missing["violations"])
    assert feat_missing["vector"]["altdata_sentiment"] is None

    # 2. With PIT enrichment fetched BEFORE as_of -> scored in both sentiment_pulse and build_features
    db.append_enrichment("altdata", ticker, "full", {
        "symbol": ticker,
        "quote": {"c": 105.0, "pc": 100.0},
        "news_headlines": [{"headline": "Beat estimates", "datetime": 1, "source": "x"}],
        "earnings_within_3d": False,
        "sentiment_polarity": 0.42,
    }, fetched_at=f"{as_of}T12:00:00Z")

    sent = run_async(data_tools.sentiment_pulse(symbol=ticker))
    assert sent["tone_bucket"] == "bullish"
    assert sent["tone_polarity_score"] == 0.42

    feat_scored = run_async(data_tools.build_features(symbol=ticker, as_of=as_of))
    assert not any(v.startswith("altdata_sentiment:") for v in feat_scored["violations"])
    assert feat_scored["vector"]["altdata_sentiment"] == 0.42
    prov_by_name = {p["name"]: p for p in feat_scored["provenance"]}
    assert prov_by_name["altdata_sentiment"]["status"] == "OK"


def test_regime_snapshot_includes_causal_state_posteriors(tmp_cache):
    ticker = "AAA"
    rows = synthetic_bars([ticker], days=300, seed=47)
    out = run_async(data_tools.regime_snapshot(bars=rows))
    sig = out["regimes"][ticker]
    post = sig["state_posteriors"]
    assert set(post) == {"calm_trend", "volatile_bull", "choppy_correction", "crisis_down"}
    assert abs(sum(post.values()) - 1.0) < 1e-3
    assert 0.0 <= sig["regime_entropy"] <= 1.0
    _no_raw_ohlcv(out)


def test_market_flow_is_derived_only(tmp_cache):
    tickers = ["AAA", "BBB"]
    rows = synthetic_bars(tickers, days=180, seed=53)
    out = run_async(alt_derived_tools.market_flow(bars=rows))
    assert out["tool"] == "market.flow"
    for sym in tickers:
        sig = out["flows"][sym]
        assert set(sig) >= {
            "off_exchange_absorption_pctile", "short_pressure_zscore",
            "put_call_skew_pctile", "buy_pressure_ratio",
            "vpin_toxicity", "institutional_flow_regime",
        }
        assert 0.0 <= sig["buy_pressure_ratio"] <= 1.0
    _no_raw_ohlcv(out)


def test_market_fundamentals_pit_is_derived_only(tmp_cache):
    tickers = ["AAA", "BBB"]
    rows = synthetic_bars(tickers, days=280, seed=59)
    out = run_async(alt_derived_tools.market_fundamentals_pit(bars=rows))
    assert out["tool"] == "market.fundamentals_pit"
    for sym in tickers:
        sig = out["fundamentals"][sym]
        assert set(sig) >= {
            "profitability_efficiency_pctile", "balance_sheet_resilience_pctile",
            "growth_acceleration_pctile", "quality_composite_pctile",
            "filing_tone_bucket", "insider_activity_bucket",
        }
    _no_raw_ohlcv(out)


def test_market_macro_nowcast_is_derived_only(tmp_cache):
    rows = synthetic_bars(["AAA", "BBB"], days=180, seed=61)
    out = run_async(alt_derived_tools.market_macro_nowcast(bars=rows))
    assert out["tool"] == "market.macro_nowcast"
    assert out["macro_quadrant"] in {
        "goldilocks_expansion", "reflation_boom", "stagflation_stress",
        "disinflation_slowdown",
    }
    assert out["vol_term_structure_state"] in {
        "backwardation_stress", "contango_calm", "flat_transition",
    }
    assert 0.0 <= out["risk_appetite_index"] <= 1.0
    assert 0.0 <= out["crisis_regime_probability"] <= 1.0
    _no_raw_ohlcv(out)


def test_market_transfer_entropy_computes_directional_matrix(tmp_cache):
    tickers = ["AAA", "BBB", "CCC"]
    rows = synthetic_bars(tickers, days=160, seed=67)
    out = run_async(alt_derived_tools.market_transfer_entropy(bars=rows, window=60))
    assert out["tool"] == "market.transfer_entropy"
    assert len(out["pairs"]) == 3  # 3 unordered pairs with directional leader/follower
    assert len(out["leader_ranking"]) == 3
    _no_raw_ohlcv(out)


# ---------------------------------------------------------------------------
# Phase 2: Warden Portfolio Optimizer & Rebalance Planner
# ---------------------------------------------------------------------------

def test_warden_optimize_portfolio_methods_and_caps():
    candidates = [
        {"symbol": "SPY", "momentum_score": 0.14, "realized_vol_annual": 0.12, "above_trend_sma": True, "current_weight": 0.10},
        {"symbol": "GLD", "momentum_score": 0.18, "realized_vol_annual": 0.15, "above_trend_sma": True, "current_weight": 0.10},
        {"symbol": "VXUS", "momentum_score": 0.06, "realized_vol_annual": 0.16, "above_trend_sma": True, "current_weight": 0.0},
        {"symbol": "IEF", "momentum_score": -0.02, "realized_vol_annual": 0.08, "above_trend_sma": False, "current_weight": 0.0},
    ]

    for method in ("vol_target_momentum", "hrp", "mvo_shrinkage"):
        out = run_async(warden_tools.optimize_portfolio(
            candidates=candidates,
            method=method,
            cash_proxy_symbol="SGOV",
            cash_momentum_score=0.04,
            target_volatility=0.12,
            top_k=2,
            turnover_cap=0.50,
        ))
        assert out["tool"] == "warden.optimize_portfolio"
        assert out["verdict"] == "OPTIMIZED"
        assert out["method"] == method
        assert abs(sum(out["target_weights"].values()) - 1.0) < 1e-3
        # House position cap = 0.25 for risk assets
        for sym, w in out["target_weights"].items():
            if sym != "SGOV":
                assert w <= 0.2501


def test_warden_plan_rebalance_sequencing_and_guards():
    # 1. Normal rebalance -> sells sequenced in Phase 1 before buys in Phase 2
    out = run_async(warden_tools.plan_rebalance(
        equity=100_000.0,
        high_water_mark=102_000.0,
        current_positions={"AAA": 25_000.0, "BBB": 5_000.0},
        target_weights={"AAA": 0.10, "BBB": 0.20, "CCC": 0.06},
        quotes={
            "AAA": {"bid": 99.95, "ask": 100.05},
            "BBB": {"bid": 49.98, "ask": 50.02},
            "CCC": {"bid": 24.99, "ask": 25.01},
        },
    ))
    assert out["verdict"] == "REBALANCE_PLANNED"
    phases = [o["phase"] for o in out["sequenced_plan"]]
    sides = [o["side"] for o in out["sequenced_plan"]]
    assert phases == sorted(phases), "Phase 1 SELL orders must precede Phase 2 BUY orders"
    assert sides[0] == "SELL" and out["sequenced_plan"][0]["symbol"] == "AAA"

    # 2. Drift buffer skip (< 3% of equity) and wide spread skip (> 15 bps)
    out_skip = run_async(warden_tools.plan_rebalance(
        equity=100_000.0,
        high_water_mark=100_000.0,
        current_positions={"AAA": 10_000.0},
        target_weights={"AAA": 0.12, "BBB": 0.20},  # AAA diff = 2% (<3%); BBB has wide spread
        quotes={
            "AAA": {"bid": 99.95, "ask": 100.05},
            "BBB": {"bid": 99.00, "ask": 101.00},  # 200 bps spread > 15 bps gate
        },
    ))
    assert "AAA" in out_skip["skipped_within_drift_band"]
    assert any("BBB" in v for v in out_skip["spread_violations"])

    # 3. Max drawdown circuit breaker (>= 12%)
    out_dd = run_async(warden_tools.plan_rebalance(
        equity=85_000.0,
        high_water_mark=100_000.0,
        current_positions={"AAA": 20_000.0},
        target_weights={"AAA": 0.20},
    ))
    assert out_dd["verdict"] == "HALTED_CIRCUIT_BREAKER"
    assert out_dd["circuit_breaker_tripped"] is True

    # 4. Feature PSI drift guard (> 0.25)
    out_psi = run_async(warden_tools.plan_rebalance(
        equity=100_000.0,
        high_water_mark=100_000.0,
        current_positions={"AAA": 20_000.0},
        target_weights={"AAA": 0.10},
        feature_psi=0.31,
    ))
    assert out_psi["verdict"] == "HALTED_FEATURE_DRIFT"
    assert out_psi["psi_drift_tripped"] is True


# ---------------------------------------------------------------------------
# Phase 3: Gym ExpressionGraph (strategy-spec-v3) Evaluator
# ---------------------------------------------------------------------------

def test_gym_evaluate_graph_valid_and_cycle_rejection(tmp_cache):
    tickers = [f"S{i:02d}" for i in range(8)]
    rows = synthetic_bars(tickers, days=320, seed=79)

    valid_graph = {
        "spec_version": "strategy-spec-v3",
        "nodes": [
            {"id": "mom_in", "kind": "input", "metric_id": "mom_20d", "direction": 1},
            {"id": "rsi_in", "kind": "input", "metric_id": "rsi_14", "direction": -1},
            {"id": "mom_rank", "kind": "cross_sectional", "op": "rank", "input": "mom_in"},
            {"id": "rsi_z", "kind": "cross_sectional", "op": "zscore", "input": "rsi_in"},
            {
                "id": "out",
                "kind": "readout",
                "op": "weighted_sum_then_rank",
                "inputs": ["mom_rank", "rsi_z"],
                "weights": [0.7, 0.3],
            },
        ],
        "top_k": 3,
        "min_readout": 0.55,
    }
    res = run_async(gym_tools.evaluate_graph(
        graph=valid_graph, seeds=[0, 1], per_regime=1, bars=rows, min_symbols=8,
    ))
    assert res["tool"] == "gym.evaluate_graph"
    assert res["valid_graph"] is True
    assert res["node_count"] == 5
    assert res["n_episodes"] > 0
    assert isinstance(res["mean_weekly_bps"], float)
    assert res["underpowered"] is True

    # Cyclic graph must be rejected cleanly
    cyclic_graph = {
        "spec_version": "strategy-spec-v3",
        "nodes": [
            {"id": "a", "kind": "cross_sectional", "op": "rank", "input": "b"},
            {"id": "b", "kind": "cross_sectional", "op": "rank", "input": "a"},
            {"id": "out", "kind": "readout", "op": "weighted_sum_then_rank", "inputs": ["a"], "weights": [1.0]},
        ],
    }
    bad = run_async(gym_tools.evaluate_graph(graph=cyclic_graph, bars=rows))
    assert bad["valid_graph"] is False
    assert any("cycle" in e for e in bad["errors"])


# ---------------------------------------------------------------------------
# Phase 4: Prop-Challenge Compliance, Defensive Posture & Sim + Upgrade Funnel
# ---------------------------------------------------------------------------

def test_warden_prop_challenge_check_taper_and_violations():
    # 1. Healthy start -> APPROVED (1.0x multiplier)
    ok = run_async(warden_tools.prop_challenge_check(
        initial_balance=100_000.0,
        current_equity=101_200.0,
        prev_day_close_equity=100_500.0,
        largest_single_day_profit=700.0,
        proposed_risk_usd=750.0,
        proposed_notional_usd=25_000.0,
    ))
    assert ok["tool"] == "warden.prop_challenge_check"
    assert ok["verdict"] == "APPROVED"
    assert ok["recommended_risk_multiplier"] == 1.0
    assert ok["target_lock_taper_active"] is False

    # 2. Near profit target (>=70% of $8,000 = $5,600) -> TAPER_REQUIRED (0.55x multiplier)
    taper = run_async(warden_tools.prop_challenge_check(
        initial_balance=100_000.0,
        current_equity=106_000.0,
        prev_day_close_equity=105_000.0,
        largest_single_day_profit=1_800.0,
        proposed_risk_usd=600.0,
    ))
    assert taper["verdict"] == "TAPER_REQUIRED"
    assert taper["target_lock_taper_active"] is True
    assert taper["recommended_risk_multiplier"] == 0.55

    # 3. Daily drawdown breach -> REJECTED (0.0x multiplier)
    breach = run_async(warden_tools.prop_challenge_check(
        initial_balance=100_000.0,
        current_equity=96_500.0,
        prev_day_close_equity=100_000.0,
        proposed_risk_usd=500.0,
    ))
    assert breach["verdict"] == "REJECTED"
    assert breach["recommended_risk_multiplier"] == 0.0
    assert any("daily_drawdown_stop" in v for v in breach["violations"])


def test_warden_defensive_posture_transitions():
    normal = run_async(warden_tools.defensive_posture(
        benchmark_return_1d_pct=0.25, benchmark_return_5d_pct=0.80,
        realized_vol_20d_pct=14.0, efficiency_ratio_20d=0.35,
    ))
    assert normal["posture"] == "Normal"
    assert normal["risk_scale_multiplier"] == 1.0

    cautious = run_async(warden_tools.defensive_posture(
        benchmark_return_1d_pct=-0.50, benchmark_return_5d_pct=-1.35,
        realized_vol_20d_pct=21.5, efficiency_ratio_20d=0.19,
    ))
    assert cautious["posture"] == "Cautious"
    assert cautious["risk_scale_multiplier"] == 0.68

    defensive = run_async(warden_tools.defensive_posture(
        benchmark_return_1d_pct=-1.45, benchmark_return_5d_pct=-2.35,
        realized_vol_20d_pct=27.5, efficiency_ratio_20d=0.25,
    ))
    assert defensive["posture"] == "Defensive"
    assert defensive["risk_scale_multiplier"] == 0.42
    assert defensive["defensive_rotation_active"] is True

    crisis = run_async(warden_tools.defensive_posture(
        benchmark_return_1d_pct=-3.10, benchmark_return_5d_pct=-4.50,
        realized_vol_20d_pct=38.0, efficiency_ratio_20d=0.40,
    ))
    assert crisis["posture"] == "CrisisShield"
    assert crisis["risk_scale_multiplier"] == 0.20
    assert crisis["effective_stop_heat_cap_pct"] <= 0.75


def test_gym_prop_challenge_sim_and_upgrade_preview():
    from swarm_mcp import envelope

    sim = run_async(gym_tools.prop_challenge_sim(
        trades_r=[1.8, -1.0, 2.2, -1.0, 1.5, 2.4, -1.0, 1.9, -0.8, 2.1],
        trades_per_day=2,
        risk_pct_per_trade=0.0075,
        n_paths=100,
        seed=42,
    ))
    assert sim["tool"] == "gym.prop_challenge_sim"
    assert sim["pass_probability_pct"] > 0.0
    assert len(sim["risk_sizing_curve"]) == 6
    assert 0.0025 <= sim["optimal_risk_pct_per_trade"] <= 0.015

    upg = envelope.upgrade_required("gym.prop_challenge_sim", "free", "https://1.21initiative.com/mcp/")
    assert upg["access"] == "UPGRADE_REQUIRED"
    assert "500-path Monte Carlo" in upg["unlock_preview"]
    assert "tool=gym.prop_challenge_sim" in upg["checkout_url"]
    assert upg["starter_pack"]["price_usd"] == 24
    assert upg["starter_pack"]["price_eur"] == 22


def test_gym_evaluate_formula_and_pit_smart_money_and_forward_league(tmp_cache):
    from swarm_mcp.tools import tournament_tools

    rows = synthetic_bars(["AAA", "BBB", "CCC", "DDD"], days=120, seed=55)

    # 1. Symbolic Formula Evaluator (causal + truncation check)
    ev = run_async(gym_tools.evaluate_formula(
        formula="CS_ZScore(TS_Mean(ret1d, 10) * KaufmanER(10))",
        bars=rows,
        forward_horizon_days=5,
    ))
    assert ev["tool"] == "gym.evaluate_formula"
    assert ev["status"] == "UNDERPOWERED_LOCAL"
    assert ev["lookahead_truncation_check"]["passed"] is True
    assert isinstance(ev["spearman_rank_ic"], float)
    assert isinstance(ev["t_hac"], float)

    # 2. Insider & Institutional PIT + Supply Chain PIT
    as_of = str(rows[-1]["ts"])[:10]
    ins = run_async(alt_derived_tools.market_insider_institutional_pit(bars=rows, as_of=as_of))
    assert ins["tool"] == "market.insider_institutional_pit"
    assert "AAA" in ins["smart_money"]
    assert 0.0 <= ins["smart_money"]["AAA"]["institutional_accumulation_pctile"] <= 1.0

    sc = run_async(alt_derived_tools.market_supply_chain_pit(bars=rows, as_of=as_of))
    assert sc["tool"] == "market.supply_chain_pit"
    assert "AAA" in sc["supply_chain"]
    assert sc["supply_chain"]["AAA"]["pre_earnings_conviction_bucket"] in (
        "tailwind_expansion", "headwind_bottleneck", "neutral",
    )

    # 3. Settled 8-week Forward League Ledger
    fl = run_async(tournament_tools.forward_league(include_weekly_commits=True))
    assert fl["tool"] == "tournament.forward_league"
    assert fl["status"] == "SETTLED_BATCH_01"
    assert fl["settled_weeks"] == 8
    assert len(fl["weekly_settlements"]) == 8
    assert len(fl["live_benchmark_sleeves"]) == 3



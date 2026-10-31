# Quant Swarm Audit Report — `momentum_breakout_v1_flawed`

- **Job ID:** `audit_906ea84d9a70a362`
- **Overall Assessment:** `PROBLEMS_OBSERVED`
- **Input SHA-256:** `158d402e172551c6ae69b548be0e435c19f82159be0094a5f1be19c625eda825`
- **Package Version:** `quant-swarm==0.7.0`
- **Execution Mode:** `local` (`0 credits charged`)

## Headline Metrics

| Metric | Value |
| --- | --- |
| Observations / Weekly Blocks | 180 sessions / 37 weeks |
| Trade Count | 35 |
| Gross Annualized Sharpe | -1.64 (90% CI: [-3.25, 0.51]) |
| Net Annualized Sharpe (1.0x House Costs) | -2.06 |
| Mean Gross Edge / Trade | 7.79 bps |
| Round-Trip Pessimistic Cost (1.0x) | 15.00 bps |
| Mean Net Edge / Trade (1.0x / 2.0x) | -7.21 bps / -22.21 bps |
| Breakeven Cost Multiplier | 0.52x |
| Max Drawdown | 23.74% |
| Top-5 Trade PnL Concentration | 93.2% |

## Structured Diagnostic Findings

### 1. Sample Depth & Statistical Power Floor — `OBSERVED_PROBLEM`
- **Evidence:** Observed 180 sessions (37 weekly blocks); annualized gross Sharpe is -1.64 with 90% bootstrap CI [-3.25, 0.50] crossing zero.
- **Why It Matters:** A bootstrap confidence interval that crosses zero indicates the observed positive return is within 1-sigma noise of a flat or losing strategy.
- **Next Experiment:** Decompose returns by market regime (`gym.label_regimes`) and test whether a single drawdown cluster or noisy entry filter is diluting the signal.
- **Limitation:** Bootstrap resampling preserves marginal distribution but does not test structural regime breaks.

### 2. Pessimistic Execution Cost & Slippage Stress Survival — `OBSERVED_PROBLEM`
- **Evidence:** Mean gross edge is 7.79 bps/trade, which is consumed by 1.0x pessimistic round-trip costs of 15.00 bps/trade -> net -7.21 bps/trade (breakeven cost multiplier: 0.52x; annualized cost drag: 735.0 bps; net Sharpe: -2.07).
- **Why It Matters:** Strategies whose gross edge per trade is smaller than realistic spread + slippage + adverse selection bleed capital deterministically in live execution.
- **Next Experiment:** Increase minimum holding horizon or signal conviction threshold to raise gross edge per trade above 22.5 bps, or reduce turnover via a wider no-trade buffer (`warden.plan_rebalance`).
- **Limitation:** Assumes static per-trade bps friction; flash-crash tail slippage can exceed 2.0x normal assumptions.

### 3. Drawdown Tail & Volatility-Regime Consistency — `OBSERVED_PROBLEM`
- **Evidence:** Max drawdown reached 23.74% (worst 5-session loss: -13.33%; calm-regime mean: 1.35 bps/day vs high-vol mean: -28.04 bps/day).
- **Why It Matters:** Drawdowns exceeding 25% or sharp 5-day crashes trigger risk-engine halts and force liquidation at the bottom.
- **Next Experiment:** Apply ATR-scaled position sizing (`warden.explain_sizing`) and halve exposure on overnight gaps > 1.2x ATR.
- **Limitation:** Evaluates close-to-close equity; intraday excursion can exceed close-to-close drawdown.

### 4. Trade Outlier Concentration & House Position Cap Compliance — `OBSERVED_PROBLEM`
- **Evidence:** Top-5 trades account for 93.2% of positive gross PnL across 35 trades; max single-position weight reached 38.0% of equity (5 trade(s) breached the 25% house cap).
- **Why It Matters:** Backtests driven by a handful of oversized outlier trades fail in production when the live risk gate (`warden.validate_order`) clamps single-name exposure at 25% of equity.
- **Next Experiment:** Re-run the backtest with hard pre-trade position clamping (`max_position_pct <= 0.25`, `max_gross_exposure_pct <= 0.60`) and winsorize top-5 trades to verify edge persists.
- **Limitation:** Evaluates per-trade size; full intraday portfolio overlap requires timestamped position snapshots.

### 5. Point-in-Time Feature Provenance & Lookahead Audit — `OBSERVED_PROBLEM`
- **Evidence:** Detected 1 timestamp lookahead leak(s) ['earnings_flag (available_at=2026-10-18T16:00:00Z > as_of=2026-10-15T20:00:00Z)'] and 1 banned actuals source(s) ["weather_hdd_shock (source='open-meteo.archive')"].
- **Why It Matters:** Features computed from timestamps after `as_of` or from archive actuals (rather than point-in-time vintage releases) inject future knowledge into historical decisions and invalidate the backtest.
- **Next Experiment:** Lag offending features to their public filing/release timestamp (`available_at <= as_of`) and replace archive actuals with point-in-time vintage series (`warden.audit_features`).
- **Limitation:** Audits declared manifest metadata; does not parse arbitrary Python/C# feature-engineering code.

### 6. Benchmark Active Return & Information Ratio — `NOT_TESTED`
- **Evidence:** No benchmark return series (`benchmark_path` or `benchmark_rows`) was supplied.
- **Why It Matters:** Without a benchmark series, passive beta exposure cannot be separated from active alpha.
- **Next Experiment:** Supply a benchmark CSV/Parquet (e.g. SPY or equal-weight universe) to compute active Information Ratio.
- **Limitation:** Absolute Sharpe can be inflated by secular bull-market drift.

### 7. Underlying Strategy Code & Multiple-Testing Trial Ledger Boundary — `NOT_TESTED`
- **Evidence:** Tabular artifact audit only — strategy source code and discarded trial count were not evaluated locally.
- **Why It Matters:** Even a clean return series and manifest cannot prove that 500 parameter combinations were not swept behind the scenes to find one lucky backtest.
- **Next Experiment:** Submit the candidate genome to the hosted Shadow Tournament (`tournament.submit`) for paired replay against the live champion and Deflated Sharpe ratio accounting.
- **Limitation:** Local tabular checks test the artifact you brought, not the research process that selected it.

## Reproducibility Bundle
```json
{
  "job_id": "audit_906ea84d9a70a362",
  "preflight_id": "pf_158d402e172551c6",
  "strategy_name": "momentum_breakout_v1_flawed",
  "input_sha256": "158d402e172551c6ae69b548be0e435c19f82159be0094a5f1be19c625eda825",
  "artifact_hashes": {
    "returns": "8513f8255edc52d55d733e2dc77a8740fb374f294999902b187ff908d3a33dac",
    "trades": "402b7db6ab02e37ae67c88b1fcbdc9d85c725e66583b373095fc2e3068e9dfc9",
    "cost_assumptions": "28bf32f0f5607e7aee1aa6cd2cbb41d7255a521be6922470e05b1bd35a23042d",
    "features_manifest": "2ca34ad5845eca83dee0628dec4cf723a823793c5ccdb20b35dc451926f94bc1"
  },
  "cost_assumptions": {
    "spread_bps": 2.0,
    "slippage_bps": 3.0,
    "adverse_selection_bps": 2.0,
    "commission_bps": 0.5
  },
  "mode": "local",
  "package_version": "0.7.0",
  "random_seeds": [
    0,
    1,
    2,
    3,
    4
  ],
  "approval_token": "aq1_afa3d09cf7152747ce4e4bbb2ef13a1a",
  "idempotency_key": "demo-flawed-v1",
  "generated_at": "2026-10-31T00:24:31.679414+00:00"
}
```

*Generated by Quant Swarm Audit (`quant-swarm`). Diagnostic evidence only — not investment advice.*

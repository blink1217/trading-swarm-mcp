# Quant Swarm Audit Report — `momentum_breakout_v2_remediated`

- **Job ID:** `audit_b05e984c671fe229`
- **Overall Assessment:** `PASSED_LOCAL_AUDIT`
- **Input SHA-256:** `b287ae1fc4f67c6204ec64a4f73f08a1d7235ab898028034c73677bc648617f5`
- **Package Version:** `quant-swarm==0.7.0`
- **Execution Mode:** `local` (`0 credits charged`)

## Headline Metrics

| Metric | Value |
| --- | --- |
| Observations / Weekly Blocks | 180 sessions / 37 weeks |
| Trade Count | 45 |
| Gross Annualized Sharpe | 3.09 (90% CI: [1.09, 5.49]) |
| Net Annualized Sharpe (1.0x House Costs) | 2.16 |
| Mean Gross Edge / Trade | 39.68 bps |
| Round-Trip Pessimistic Cost (1.0x) | 15.00 bps |
| Mean Net Edge / Trade (1.0x / 2.0x) | 24.68 bps / 9.68 bps |
| Breakeven Cost Multiplier | 2.65x |
| Max Drawdown | 3.13% |
| Top-5 Trade PnL Concentration | 15.2% |

## Structured Diagnostic Findings

### 1. Sample Depth & Statistical Power Floor — `PASSED`
- **Evidence:** Observed 180 sessions across 37 weekly blocks (clears 60-session / 20-week floor). Gross Sharpe 3.09, 90% bootstrap CI [1.09, 5.48].
- **Why It Matters:** Provides sufficient time-series degrees of freedom to evaluate regime stability and cost drag.
- **Next Experiment:** Proceed to multi-seed paired comparison against the live champion (`tournament.submit`).
- **Limitation:** Single-path historical depth does not account for unrecorded parameter trials (Deflated Sharpe).

### 2. Pessimistic Execution Cost & Slippage Stress Survival — `PASSED`
- **Evidence:** Gross edge of 39.68 bps/trade survives 1.0x (15.00 bps -> net +24.68 bps) and 2.0x pessimistic cost stress (30.00 bps -> net +9.68 bps; breakeven multiplier: 2.65x; net Sharpe: 2.16).
- **Why It Matters:** Confirms a structural buffer above realistic bid-ask spread, slippage, and adverse selection.
- **Next Experiment:** Verify liquidity capacity on target symbols using `market.microstructure` before scaling notional.
- **Limitation:** Based on declared/house bps parameters; borrow fees for short legs are not included.

### 3. Drawdown Tail & Volatility-Regime Consistency — `PASSED`
- **Evidence:** Max drawdown contained at 3.13% (worst 5-session return: -2.18%); positive mean return in both calm (+15.65 bps/day) and elevated-volatility (+7.49 bps/day) windows.
- **Why It Matters:** Demonstrates controlled tail excursion and positive expectancy across volatility environments.
- **Next Experiment:** Verify worst-regime margin across all 5 canonical regimes via `gym.probe_fragility`.
- **Limitation:** Close-to-close series does not capture intraday stop-out slippage.

### 4. Trade Outlier Concentration & House Position Cap Compliance — `PASSED`
- **Evidence:** Across 45 trades, top-5 trades account for 15.2% of gross gains (well below 45% threshold) and max single-position size is 16.0% (0 breaches of the 25% house floor).
- **Why It Matters:** Confirms broad-based trade breadth and full compliance with live-capital position sizing invariants.
- **Next Experiment:** Validate multi-asset rebalance trade lists using `warden.plan_rebalance`.
- **Limitation:** Assumes trade timestamps reflect actual execution fill times.

### 5. Point-in-Time Feature Provenance & Lookahead Audit — `PASSED`
- **Evidence:** All 3 declared feature(s) passed point-in-time timestamp checks (`available_at <= as_of`), zero banned actuals sources, and coverage >= 90%.
- **Why It Matters:** Verifies declared feature inputs are causally knowable at decision time.
- **Next Experiment:** Cross-check cross-sectional IC significance on alternative-data features with `pit.analysis`.
- **Limitation:** Validates the feature manifest contract; does not verify internal code implementation.

### 6. Benchmark Active Return & Information Ratio — `PASSED`
- **Evidence:** Matched 180 overlapping sessions vs benchmark; annualized active return is 2754.4 bps with Information Ratio 1.72.
- **Why It Matters:** Separates genuine active edge from passive market beta exposure.
- **Next Experiment:** Verify active return stability across drawdown and chop regimes (`gym.paired_preview`).
- **Limitation:** Linear benchmark subtraction does not capture nonlinear tail beta.

### 7. Underlying Strategy Code & Multiple-Testing Trial Ledger Boundary — `NOT_TESTED`
- **Evidence:** Tabular artifact audit only — strategy source code and discarded trial count were not evaluated locally.
- **Why It Matters:** Even a clean return series and manifest cannot prove that 500 parameter combinations were not swept behind the scenes to find one lucky backtest.
- **Next Experiment:** Submit the candidate genome to the hosted Shadow Tournament (`tournament.submit`) for paired replay against the live champion and Deflated Sharpe ratio accounting.
- **Limitation:** Local tabular checks test the artifact you brought, not the research process that selected it.

## Reproducibility Bundle
```json
{
  "job_id": "audit_b05e984c671fe229",
  "preflight_id": "pf_b287ae1fc4f67c62",
  "strategy_name": "momentum_breakout_v2_remediated",
  "input_sha256": "b287ae1fc4f67c6204ec64a4f73f08a1d7235ab898028034c73677bc648617f5",
  "artifact_hashes": {
    "returns": "de053179e207ebeadd12f5d5dade4d65b1f57027d610f497e685afe65bf2541b",
    "trades": "c9cbb91116395c9bda1bc52f13a66f7ef37cbabfbdd5055f308ba2829ebbbf4d",
    "benchmark": "606aa085d26bdec4f36484ec6335fe10628018d441dd9d1d7a6a65a4829fa416",
    "cost_assumptions": "28bf32f0f5607e7aee1aa6cd2cbb41d7255a521be6922470e05b1bd35a23042d",
    "features_manifest": "1e83d3c64422303b8c47ae4166ac8fc64b6dbc8e9c526d19c34a95d0a07872fb"
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
  "approval_token": "aq1_3ae700c96b0aad931c439f2cc5f9f88a",
  "idempotency_key": "demo-clean-v2",
  "generated_at": "2026-10-31T00:24:31.745476+00:00"
}
```

*Generated by Quant Swarm Audit (`quant-swarm`). Diagnostic evidence only — not investment advice.*

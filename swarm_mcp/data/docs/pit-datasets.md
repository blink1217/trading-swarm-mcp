### PIT significance, explained (`pit.analysis`)
`pit.analysis` evaluates cross-sectional Spearman rank IC per `(dataset | metric | horizon)` family with size/sector neutralization, Newey-West (Bartlett) overlap-adjusted t-statistics, Benjamini-Hochberg `q <= 0.05` multiple-testing control, staleness caps, and a shuffled-date placebo gate (`SIGNIFICANT`, `NOT_SIGNIFICANT`, `NOT_JUDGED`, or `UNVERIFIED`).

### Point-in-time data, for real
Every hosted dataset (`market.fundamentals_pit`, `market.insider_institutional_pit`, `market.supply_chain_pit`, `market.macro_nowcast`) is indexed strictly by its public filing or release timestamp (`available_at <= as_of`)—never restated fiscal-period archive dates—and returns derived ratios, z-scores, and percentiles rather than raw vendor series.

# quant-swarm verdicts (pit.analysis)

- SIGNIFICANT: an L1 survivor of the SAME pit run - rolling 52-week IR > 0 and
  Benjamini-Hochberg q <= 0.05 across every family tested, with harmonic-mean p
  and the Newey-West t recorded. A raw p <= 0.05 alone is never enough.
- NOT_SIGNIFICANT: states the reason (negative IR, or a nominal p that does not
  survive the multiple-testing correction).
- NOT_JUDGED: below the 26-observation floor, stale release, or a diagnostic-only
  raw level - the p-value column is null and no claim is made.
- UNVERIFIED: no gate artifact, gate from a different pit run, or a failed
  placebo - a raw p-value is never upgraded on its own.
- research_only: the dataset covers under 50% of the universe - research
  evidence, not a tradable signal.

Verdicts describe a family's cross-sectional predictive power across the
universe - never a forecast for one name - and are not investment advice.

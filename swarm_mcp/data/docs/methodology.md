# quant-swarm methodology

Cross-sectional Spearman rank IC per (dataset|metric|horizon) family, computed
point-in-time (each dataset dated by when it was publicly knowable). Rolling
52-week IR on a size/sector-neutralized metric against neutralized forward
returns (log recent dollar volume + sector dummies per date). Inference is
Newey-West (Bartlett) at lag ceil(horizon/5) because horizons longer than the
decision spacing overlap. Raw accounting levels are diagnostic-only; the L1
gate tests scale-free variants (Year-over-year growth, per-share,
assets-scaled). Families whose latest release is older than the dataset
staleness cap are never judged. A shuffled-date placebo runs against every
release; a flagged run (gate_unreliable) withholds all verdicts.

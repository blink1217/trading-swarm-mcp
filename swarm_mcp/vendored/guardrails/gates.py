# VENDORED SUBSET of trading-swarm-guardrails/gates.py.py at b3654b98ede3a9c8144a325dda8dcf6ef11fa0f7.
# Excluded — server-side selection machinery, deliberately never shipped: should_promote.
# Everything below is byte-identical to the pinned source. License: LICENSE.md.
"""Promotion gates for the evolution loop.

A challenger genome is promoted only if ALL gates pass. Failure of any single gate is
a hard `False` (no promotion). These thresholds are HUMAN-TUNED ONLY.

Two gate layers:

1. `tier_promotion_gate` — the PROVENANCE gate. It decides whether the challenger is
   even SCORABLE on the evidence presented, before any statistical comparison. It
   implements the lifecycle state machine and the two promotion paths:

     COLLECTING    (tape depth < 8 wks)   run/learn only; no promotions
     TIER_A_ONLY   (8 <= depth < 26 wks)  FAST PATH: tier-A-only diffs on 10y replay
     TAPE_ELIGIBLE (depth >= 26 wks)      SLOW PATH also allowed: tier-B/C diffs need
                                          tape-fidelity evidence of >= the mutated tier
                                          AND coverage >= MIN_COVERAGE.

   Insufficient coverage or evidence tier is UNSCORABLE — never neutral-filled.

2. `should_promote` — the STATISTICAL gates (maximin, DSR margin, PBO, paired
   significance, zero violations). Takes the tier gate verdict and refuses to
   promote when the provenance gate says unscorable.
"""
from __future__ import annotations

# ---- Tunable by HUMAN COMMIT ONLY. ----
MIN_WORST_REGIME_MARGIN = 0.01        # challenger worst-regime score must beat champion by this much
MIN_DSR_MARGIN = 0.05                 # challenger DSR must beat champion by at least this
MAX_PBO = 0.30                        # probability of backtest overfitting cap
PAIRED_P_VALUE_MAX = 0.05             # paired test significance on identical seeds
MIN_EPISODES = 20                     # minimum paired episodes for a promotable match
MAX_VIOLATIONS = 0                    # zero hard-constraint violations allowed
RECONCILIATION_TOLERANCE_BPS = 30.0   # sim-vs-realized mean absolute error budget (plan step 9)

# ---- Tier-aware promotion thresholds (plan decisions 5, R8, R9). ----
MIN_COVERAGE = 0.90                   # tape coverage: fraction of challenger-selected symbols in snapshot
TAPE_DEPTH_TIER_A_WEEKS = 8           # fast-path promotions allowed at >= this depth
TAPE_DEPTH_TAPE_ELIGIBLE_WEEKS = 26   # slow-path (tier-B/C) promotions allowed at >= this depth
MIN_SCREEN_AGREEMENT = 0.70           # Jaccard of reimplemented screens vs live Finviz, recorded on tape

# Lifecycle states
COLLECTING = "COLLECTING"
TIER_A_ONLY = "TIER_A_ONLY"
TAPE_ELIGIBLE = "TAPE_ELIGIBLE"

# Evidence kinds and the tier they can support
EVIDENCE_TIERS = {
    "bars_replay": "A",       # 10-year bars_1day replay in the gym (tier-A price subset)
    "tape_replay": "C",       # archived wide-superset tape replay (point-in-time B/C values)
}

# Regimes the adversarial sampler can pick (labels shared with gym/regime.py)
REGIMES = ["high_vol", "drawdown", "chop", "melt_up", "gap_heavy"]

_TIER_ORDER = {"A": 0, "B": 1, "C": 2}


def lifecycle_state(tape_depth_weeks: float) -> str:
    """Map cumulative archived-tape depth (weeks) to the lifecycle state."""
    if tape_depth_weeks < TAPE_DEPTH_TIER_A_WEEKS:
        return COLLECTING
    if tape_depth_weeks < TAPE_DEPTH_TAPE_ELIGIBLE_WEEKS:
        return TIER_A_ONLY
    return TAPE_ELIGIBLE


def tier_promotion_gate(
    mutated_tier: str | None,
    evidence_kind: str,
    lifecycle: str,
    coverage: float | None = None,
    screen_agreement: float | None = None,
) -> dict:
    """Decide whether a challenger is scorable/promotable on the presented evidence.

    `mutated_tier` is the highest tier among the challenger's mutated genes
    (guardrails.tiers.highest_mutated_tier); None means identical to champion.
    `coverage` is the tape coverage fraction (R8) — required for slow path.
    `screen_agreement` is the weekly Jaccard of reimplemented screens vs live
    Finviz (plan decision 6); below MIN_SCREEN_AGREEMENT bars bar-based
    (tier-A) evidence because the screen membership being scored is suspect.

    Returns {'scorable', 'allowed', 'path', 'verdicts'} — an unscorable verdict
    must be reported as UNSCORABLE, never silently neutral-filled.
    """
    verdicts: list[str] = []
    evidence_tier = EVIDENCE_TIERS.get(evidence_kind)
    if evidence_tier is None:
        return {"scorable": False, "allowed": False, "path": None,
                "verdicts": [f"UNSCORABLE unknown evidence kind {evidence_kind!r}"]}

    if mutated_tier is None:
        return {"scorable": True, "allowed": True, "path": "identical",
                "verdicts": ["IDENTICAL no mutated genes — nothing to gate"]}

    if lifecycle == COLLECTING:
        verdicts.append(f"UNSCORABLE lifecycle {lifecycle}: tape depth below {TAPE_DEPTH_TIER_A_WEEKS} wks — run/learn only, no promotions")
        return {"scorable": False, "allowed": False, "path": None, "verdicts": verdicts}

    # FAST PATH: tier-A-only mutation on the 10-year replay.
    if mutated_tier == "A":
        if evidence_tier != "A":
            verdicts.append(f"UNSCORABLE tier-A mutation scored with non-bar evidence {evidence_kind!r}")
            return {"scorable": False, "allowed": False, "path": None, "verdicts": verdicts}
        if screen_agreement is not None and screen_agreement < MIN_SCREEN_AGREEMENT:
            verdicts.append(f"UNSCORABLE screen agreement {screen_agreement:.2f} < {MIN_SCREEN_AGREEMENT} — bar-based screen membership suspect")
            return {"scorable": False, "allowed": False, "path": None, "verdicts": verdicts}
        verdicts.append(f"FAST_PATH tier-A mutation on {evidence_kind} in lifecycle {lifecycle}")
        return {"scorable": True, "allowed": True, "path": "fast", "verdicts": verdicts}

    # SLOW PATH: any tier-B/C mutation needs tape-fidelity evidence.
    if lifecycle != TAPE_ELIGIBLE:
        verdicts.append(f"UNSCORABLE tier-{mutated_tier} mutation requires lifecycle {TAPE_ELIGIBLE} (tape >= {TAPE_DEPTH_TAPE_ELIGIBLE_WEEKS} wks), have {lifecycle}")
        return {"scorable": False, "allowed": False, "path": None, "verdicts": verdicts}
    if _TIER_ORDER.get(evidence_tier, 99) < _TIER_ORDER[mutated_tier]:
        verdicts.append(f"UNSCORABLE evidence tier {evidence_tier} < mutated tier {mutated_tier} — evidence must be >= highest mutated tier")
        return {"scorable": False, "allowed": False, "path": None, "verdicts": verdicts}
    if coverage is None or coverage < MIN_COVERAGE:
        verdicts.append(f"UNSCORABLE tape coverage {coverage} < {MIN_COVERAGE} — challenger-selected symbols missing from snapshot (R8)")
        return {"scorable": False, "allowed": False, "path": None, "verdicts": verdicts}
    verdicts.append(f"SLOW_PATH tier-{mutated_tier} mutation on {evidence_kind}, coverage {coverage:.2f} >= {MIN_COVERAGE}")
    return {"scorable": True, "allowed": True, "path": "slow", "verdicts": verdicts}



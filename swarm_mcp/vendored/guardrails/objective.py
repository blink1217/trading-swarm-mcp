# VENDORED SUBSET of trading-swarm-guardrails/objective.py.py at b3654b98ede3a9c8144a325dda8dcf6ef11fa0f7.
# Excluded — server-side selection machinery, deliberately never shipped: deflated_sharpe, _norm_ppf, score.
# Everything below is byte-identical to the pinned source. License: LICENSE.md.
"""The single objective definition for the evolution loop.

score = DSR(weekly net returns, n_trials=cumulative) - lambda_dd * max_drawdown - lambda_to * turnover
evaluated MAXIMIN across the adversarial regime set, with -inf (never a penalty) on any
hard-constraint violation.

This module is the scoreboard. If the loop can edit it, the loop games it. It lives in
the guardrails repo, pinned by SHA, and is imported (never copied) by the gym and the
evolution service.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ---- Tunable by HUMAN COMMIT ONLY. Never by evolution-engineer. ----
DEFAULT_DSR_TRIALS = 1.0          # baseline trials at first use; monotonic n_trials overrides
DEFAULT_LAMBDA_DD = 0.5           # penalty weight on max drawdown (fraction of equity)
DEFAULT_LAMBDA_TO = 0.05          # penalty weight on turnover (fraction of equity per week)
MIN_EPISODES_FOR_SCORE = 10       # below this, no score (None)
ANNUALIZATION = 52                # weekly episodes per year
# ---- Hard constraints (violation -> -inf) ----
MAX_GROSS_EXPOSURE_PCT = 0.60     # weekend gross exposure cap (matches risk env WEEKEND_GROSS_CAP_PCT)
MAX_POSITION_PCT = 0.25           # per-name cap (matches risk env MAX_POSITION_PCT)
MIN_EPISODES_HARD = 8             # statistical-power floor for promotion
CVAR_95_FLOOR = -0.12             # weekly CVaR95 floor: worse than -12% equity in the tail -> infeasible


@dataclass(frozen=True)
class ObjectiveParams:
    lambda_dd: float = DEFAULT_LAMBDA_DD
    lambda_to: float = DEFAULT_LAMBDA_TO
    annualization: int = ANNUALIZATION






def hard_constraint_violations(
    weekly_net_bps: list[float],
    per_episode: list[dict],
    params: ObjectiveParams | None = None,
) -> list[str]:
    """Return a list of violated hard constraints (empty == feasible).

    - gross exposure / per-name caps come from per_episode['max_gross_exposure_pct']
      and per_episode['max_position_pct'] recorded by the gym.
    - min-episodes floor for statistical power.
    - CVaR95 floor: the 5th-percentile weekly return must not be below CVAR_95_FLOOR.
    """
    violations: list[str] = []
    if len(weekly_net_bps) < MIN_EPISODES_HARD:
        violations.append(f"min episodes: {len(weekly_net_bps)} < {MIN_EPISODES_HARD}")
    if weekly_net_bps:
        sorted_r = sorted(weekly_net_bps)
        tail_n = max(1, int(0.05 * len(sorted_r) + 0.9999))
        cvar95 = sum(sorted_r[:tail_n]) / tail_n
        if cvar95 < CVAR_95_FLOOR * 1e4:
            violations.append(f"CVaR95 {cvar95 / 1e4:.4f} < floor {CVAR_95_FLOOR}")
    for ep in per_episode:
        g = ep.get("max_gross_exposure_pct") or 0.0
        if g > MAX_GROSS_EXPOSURE_PCT + 1e-9:
            violations.append(f"gross exposure {g:.3f} > cap {MAX_GROSS_EXPOSURE_PCT}")
        p = ep.get("max_position_pct") or 0.0
        if p > MAX_POSITION_PCT + 1e-9:
            violations.append(f"per-name position {p:.3f} > cap {MAX_POSITION_PCT}")
    return violations



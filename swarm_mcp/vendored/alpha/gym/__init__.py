"""The gym: deterministic replay of the swarm's weekly decision path.

screen -> LGBM gate -> ATR sizing -> MOO/MOC fill -> 5d hold -> exit
over `bars_1day`. One episode = one trading week. Runs as a Cloud Run Job
(`python -m gym.main --mode simulate`), and is importable as a library by the
evolution service and the invariant suite (no lookahead test).

HARD RULES (enforced by tests in trading-swarm-guardrails/invariants):
- No lookahead: every feature at decision time uses data <= t.
- Costs are charged at the pessimistic end (guardrails/fill_model.py).
- Gross exposure and per-name caps are recorded per episode and enforced by
  guardrails/objective.py (violation -> -inf).
"""

"""sys.path shim for the vendored checker subset.

Inserts the vendored alpha tree (gym/, shared/, bars_fetch, order_checks,
guardrails_path) and the vendored guardrails checker subset so the vendored
modules import exactly as they do in trading-swarm-alpha:

    import guardrails_path  # resolves vendored guardrails
    from objective import MAX_POSITION_PCT
    from gym.simulator import evaluate_genome
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
VENDORED_DIR = os.path.join(_HERE, "vendored")
ALPHA_DIR = os.path.join(VENDORED_DIR, "alpha")
GUARDRAILS_DIR = os.path.join(VENDORED_DIR, "guardrails")


def ensure_vendored_on_path() -> None:
    for p in (ALPHA_DIR, GUARDRAILS_DIR):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


ensure_vendored_on_path()

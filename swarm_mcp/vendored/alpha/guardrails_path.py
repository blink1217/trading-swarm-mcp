"""Vendoring shim — same resolution contract as trading-swarm-alpha's
guardrails_path.py, pointed at the vendored guardrails checker subset.

Resolution order:
1. `GYM_GUARDRAILS_DIR` env var (override)
2. vendored subset at <this file>/../guardrails
"""
from __future__ import annotations

import os
import sys


def guardrails_path() -> str:
    env = os.environ.get("GYM_GUARDRAILS_DIR", "")
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    vendored = os.path.normpath(os.path.join(here, "..", "guardrails"))
    if os.path.isdir(vendored) and any(os.path.isfile(os.path.join(vendored, m)) for m in ("objective.py", "gates.py")):
        return vendored
    raise ImportError("vendored guardrails subset not found — set GYM_GUARDRAILS_DIR or run scripts\\vendor.ps1")


def ensure_guardrails_on_path() -> str:
    p = guardrails_path()
    if p not in sys.path:
        sys.path.insert(0, p)
    return p


ensure_guardrails_on_path()

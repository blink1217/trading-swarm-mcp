"""IP boundary: the shipped tree never contains or references the selection
machinery (objective.score, objective.deflated_sharpe, gates.should_promote)."""
from __future__ import annotations

import ast
import os

import gates
import objective

PKG_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "swarm_mcp")
BANNED_NAMES = {"score", "deflated_sharpe", "should_promote"}


def _all_py():
    for dirpath, dirnames, filenames in os.walk(PKG_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def test_selection_machinery_absent_at_runtime():
    for name in ("score", "deflated_sharpe"):
        assert not hasattr(objective, name), f"objective.{name} must stay server-side"
    assert not hasattr(gates, "should_promote"), "gates.should_promote must stay server-side"


def test_checkers_and_constants_still_ship():
    assert hasattr(objective, "hard_constraint_violations")
    assert objective.MAX_POSITION_PCT == 0.25
    assert objective.MAX_GROSS_EXPOSURE_PCT == 0.60
    assert gates.MIN_EPISODES == 20
    assert gates.MAX_PBO == 0.30
    assert gates.MIN_DSR_MARGIN == 0.05
    assert gates.MIN_WORST_REGIME_MARGIN == 0.01
    assert gates.TAPE_DEPTH_TIER_A_WEEKS == 8
    assert gates.TAPE_DEPTH_TAPE_ELIGIBLE_WEEKS == 26
    assert gates.MIN_COVERAGE == 0.90


def test_no_code_references_selection_machinery():
    offenders = []
    for path in _all_py():
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("objective", "gates"):
                for alias in node.names:
                    if alias.name in BANNED_NAMES:
                        offenders.append(f"{path}: imports {node.module}.{alias.name}")
            if isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
                offenders.append(f"{path}: attribute access .{node.attr}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in BANNED_NAMES:
                offenders.append(f"{path}: defines {node.name}")
    assert not offenders, offenders

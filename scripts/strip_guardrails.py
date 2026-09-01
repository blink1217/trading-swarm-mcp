"""Strip excluded top-level functions from a Python source file by line range.

Used to vendor the guardrails CHECKER subset from the pinned SHA: everything
except the excluded function blocks stays byte-identical, and a provenance
header is prepended. Deterministic — check_pin.py applies the exact same
transform to the pinned source when verifying the vendored tree.
"""
from __future__ import annotations

import ast


def header_for(module: str, sha: str, excluded: list[str]) -> str:
    return (
        f"# VENDORED SUBSET of trading-swarm-guardrails/{module}.py at {sha}.\n"
        f"# Excluded — server-side selection machinery, deliberately never shipped: {', '.join(excluded)}.\n"
        "# Everything below is byte-identical to the pinned source. License: LICENSE.md.\n"
    )


def strip_functions(source: str, excluded: set[str], header: str = "") -> str:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in excluded:
            start = node.lineno - 1
            end = node.end_lineno if node.end_lineno is not None else len(lines)
            spans.append((start, end))
    for start, end in sorted(spans, reverse=True):
        del lines[start:end]
    return header + "".join(lines)

"""Genome schema loader + validator + canonical hash.

A genome is a bounded JSON config (data, not code). Every numeric field has explicit
min/max in `genome_schema.json`; out-of-range genomes are REJECTED on load in the gym,
coordinator, and warden. This module is stdlib-only (plus `jsonschema` if present, with
a builtin fallback validator) so the coordinator can import it without heavy deps.
"""
from __future__ import annotations

import hashlib
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(_HERE, "genome_schema.json")
CURRENT_SCHEMA_VERSION = 2


def load_schema(path: str | None = None) -> dict:
    with open(path or SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


_SCHEMA = load_schema()


def _expect_type(v, t) -> list[str]:
    if t == "number":
        return [] if isinstance(v, (int, float)) and not isinstance(v, bool) else [f"expected number, got {type(v).__name__}"]
    if t == "integer":
        return [] if isinstance(v, int) and not isinstance(v, bool) else [f"expected integer, got {type(v).__name__}"]
    if t == "boolean":
        return [] if isinstance(v, bool) else [f"expected boolean, got {type(v).__name__}"]
    if t == "string":
        return [] if isinstance(v, str) else [f"expected string, got {type(v).__name__}"]
    if t == "object":
        return [] if isinstance(v, dict) else [f"expected object, got {type(v).__name__}"]
    if t == "array":
        return [] if isinstance(v, list) else [f"expected array, got {type(v).__name__}"]
    return []


def _validate_node(inst, node, path: str) -> list[str]:
    """Minimal JSON Schema subset: type, const, minimum/maximum, pattern, properties,
    required, additionalProperties. Sufficient for genome_schema.json."""
    errors: list[str] = []

    def p(msg: str) -> None:
        errors.append(f"{path}: {msg}")

    t = node.get("type")
    if t:
        for e in _expect_type(inst, t):
            p(e)
    if "const" in node and inst != node["const"]:
        p(f"const violation: expected {node['const']}, got {inst!r}")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in node and inst < node["minimum"]:
            p(f"{inst} < minimum {node['minimum']}")
        if "maximum" in node and inst > node["maximum"]:
            p(f"{inst} > maximum {node['maximum']}")
    if isinstance(inst, str) and "pattern" in node:
        import re
        if not re.match(node["pattern"], inst):
            p(f"pattern violation: {inst!r}")
    if t == "object":
        props = node.get("properties", {})
        required = node.get("required", [])
        if not isinstance(inst, dict):
            return errors
        for k in required:
            if k not in inst:
                p(f"missing required property {k!r}")
        for k, v in inst.items():
            if k in props:
                errors.extend(_validate_node(v, props[k], f"{path}.{k}"))
            elif node.get("additionalProperties") is False:
                p(f"additional property not allowed: {k!r}")
    if t == "array":
        if not isinstance(inst, list):
            return errors
        items = node.get("items")
        if items:
            for i, v in enumerate(inst):
                errors.extend(_validate_node(v, items, f"{path}[{i}]"))
    return errors


def validate_genome(genome: dict, schema: dict | None = None) -> list[str]:
    """Return a list of validation errors (empty == valid)."""
    return _validate_node(genome, schema or _SCHEMA, "$")


def genome_hash(genome: dict) -> str:
    """Canonical SHA-256 of the canonical JSON. Stable across key order."""
    canonical = json.dumps(genome, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_genome(path: str | None = None, raw: str | dict | None = None, schema: dict | None = None) -> dict:
    """Load and strictly validate a genome. Raises ValueError on any violation."""
    if raw is not None:
        genome = json.loads(raw) if isinstance(raw, str) else raw
    elif path:
        with open(path, encoding="utf-8") as f:
            genome = json.load(f)
    else:
        raise ValueError("load_genome requires either path or raw")
    errors = validate_genome(genome, schema)
    if errors:
        raise ValueError(f"genome failed validation ({len(errors)} errors): " + "; ".join(errors[:10]))
    if genome.get("schema_version") != CURRENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version {genome.get('schema_version')}")
    return genome

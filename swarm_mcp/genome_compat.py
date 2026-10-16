"""Genome schema compatibility: accept v2 submissions under the vendored v3 validator.

Schema v3 (upstream guardrails A-01) is field-identical to v2; only the
``schema_version`` const moved and the execution *_bps cost genes became
informational (the gym charges the immutable house cost instead of the
genome-declared costs, so cost genes can no longer change an outcome).

v2 genomes are therefore normalized to v3 for EVALUATION ONLY. The canonical
``genome_hash`` identity is always computed over the genome exactly as
submitted, because the site cross-checks the client hash and the leaderboard
metadata records the evaluated schema separately.
"""
from __future__ import annotations

import copy

from swarm_mcp import vendor_path  # noqa: F401

from genome_schema import genome_hash  # vendored guardrails

SUPPORTED_SCHEMA_VERSIONS = (2, 3)


def normalize(genome: dict) -> dict:
    """Return a validated-shaped v3 copy of ``genome`` (does not mutate the input).

    Raises ValueError for any schema_version outside SUPPORTED_SCHEMA_VERSIONS.
    Note: normalization only rewrites schema_version. Structural validation is
    left to genome_schema.validate_genome (call normalize first, then validate).
    """
    if not isinstance(genome, dict):
        raise ValueError("genome must be a dict")
    version = genome.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            f"unsupported schema_version {version!r} — supported: {SUPPORTED_SCHEMA_VERSIONS}")
    normalized = copy.deepcopy(genome)
    normalized["schema_version"] = 3
    return normalized


def canonicalize_numbers(value):
    """Collapse float-valued integers to ints, recursively (copy).

    Cross-language hash canonicalization: Python serializes 2.0 as "2.0" while
    JS JSON.stringify emits "2", so a genome written with ``2.0`` hashed
    DIFFERENTLY in Python and JS and the site's client-hash cross-check failed
    with "genome_hash does not match". The canonical number rule treats every
    float with an integral value as the integer it equals; bools stay bools,
    non-integral floats are untouched.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: canonicalize_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [canonicalize_numbers(v) for v in value]
    return value


def genome_hash_canonical(genome: dict) -> str:
    """User identity hash: the number-form-independent canonical SHA-256.

    Equals the site's canonicalGenomeHash for every JSON-expressible genome
    (JSON.parse loses the 2.0/2 distinction exactly like this rule does).
    """
    return genome_hash(canonicalize_numbers(genome))


def normalize_for_validation(genome: dict) -> tuple[dict | None, list[str]]:
    """(normalized_genome, errors) — empty errors means the caller may proceed.

    On a non-dict genome or an unsupported schema_version the error text mirrors
    the vendored validator's message so callers that surface ``errors`` keep
    their existing response shape.
    """
    try:
        return normalize(genome), []
    except ValueError as exc:
        return None, [str(exc)]

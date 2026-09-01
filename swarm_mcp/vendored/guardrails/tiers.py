"""Gene-level data-provenance tiers — the evidence-path contract.

Every gene declares the provenance tier of the data it consumes:

  A  bars_1day technicals — ~10 years of history, fully reconstructable.
  B  Finnhub enrichment (deterministic keyword sentiment, earnings calendar)
     — ~1 year, degraded (free-tier lookback, news survivorship).
  C  Tape-only / zero-history: Finviz vendor numbers, Open-Meteo FORECAST
     anomalies (the archive API returns actuals, NOT what the forecast said
     on a past date), energy bias, LLM prompt variants.

Promotion gates (gates.py) use the HIGHEST tier among the genes a challenger
mutated to pick the evidence path: tier-A-only diffs may promote on the
10-year replay (fast path); any tier-B/C diff requires tape-fidelity evidence
of at least that tier (slow path). Insufficient coverage is UNSCORABLE —
never neutral-filled.

Group-level `data_tier` consts in genome_schema.json are the max over each
group's genes; test_tiers asserts the two stay consistent.
"""
from __future__ import annotations

import json
import os

TIERS = ("A", "B", "C")
TIER_ORDER = {"A": 0, "B": 1, "C": 2}

# Per-gene tiers. Dotted paths; a bare group name covers every gene in the
# group not listed individually.
GENE_TIERS: dict[str, str] = {
    # screen predicates are bar-based re-implementations (plan decision 6)
    "screen": "A",
    # analyst gates on bar features only (LGBM trained on bars_1day features)
    "analyst": "A",
    "risk": "A",
    "execution": "A",
    # weekend_gate is mixed: the price-risk genes are tier A, the earnings
    # calendar is tier B, the forecast-anomaly energy bias is tier C.
    "weekend_gate.max_earnings_proximity_days": "B",
    "weekend_gate.min_energy_bias": "C",
    "weekend_gate": "A",  # remaining weekend_gate genes (atr/gap/thresholds)
    # prompt variants are irreproducible: tape-only
    "prompt_variant_id": "C",
}

_HERE = os.path.dirname(os.path.abspath(__file__))


def gene_tier(path: str) -> str:
    """Resolve the tier of a dotted gene path (most-specific match wins)."""
    if path in GENE_TIERS:
        return GENE_TIERS[path]
    group = path.split(".", 1)[0]
    if group in GENE_TIERS:
        return GENE_TIERS[group]
    raise KeyError(f"gene path {path!r} has no declared data tier — refusing to score")


def max_tier(tiers: list[str]) -> str | None:
    """Highest tier in the list, or None for an empty list."""
    known = [t for t in tiers if t in TIER_ORDER]
    if not known:
        return None
    return max(known, key=lambda t: TIER_ORDER[t])


def _walk_diff(a: dict | list | object, b: dict | list | object, prefix: str, out: list[str]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{prefix}.{k}" if prefix else k)
            else:
                _walk_diff(a[k], b[k], f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(a, list) and isinstance(b, list):
        if a != b:
            out.append(prefix)
    else:
        if a != b:
            out.append(prefix)


def diff_genes(champion: dict, challenger: dict, include_tier_tags: bool = False) -> list[str]:
    """Dotted paths of every gene whose value differs.

    `data_tier` tags are schema consts (metadata, not strategy); they are
    excluded from the mutation set unless `include_tier_tags` is set — a
    challenger that differs ONLY in const tier tags is identical as strategy.
    """
    out: list[str] = []
    _walk_diff(champion, challenger, "", out)
    if not include_tier_tags:
        out = [p for p in out if not p.endswith(".data_tier") and p != "data_tier"]
    return sorted(out)


def mutated_tiers(champion: dict, challenger: dict) -> list[str]:
    """Tier of each mutated gene (schema/const noise excluded)."""
    tiers = []
    for path in diff_genes(champion, challenger):
        try:
            tiers.append(gene_tier(path))
        except KeyError:
            tiers.append("C")  # unknown provenance is fail-closed to the worst tier
    return tiers


def highest_mutated_tier(champion: dict, challenger: dict) -> str | None:
    """Highest provenance tier among the challenger's mutated genes.

    None means the genomes are strategy-identical (nothing mutated).
    """
    return max_tier(mutated_tiers(champion, challenger))


def group_tier_maxes(schema: dict | None = None) -> dict[str, str]:
    """Group -> max gene tier, derived from GENE_TIERS. test_tiers checks this
    against the `data_tier` consts in genome_schema.json."""
    if schema is None:
        with open(os.path.join(_HERE, "genome_schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
    groups = [k for k, v in schema.get("properties", {}).items()
              if isinstance(v, dict) and v.get("type") == "object"]
    out: dict[str, str] = {}
    for g in groups:
        gene_tiers = [gene_tier(f"{g}.{name}") for name in schema["properties"][g].get("properties", {}) if name != "data_tier"]
        out[g] = max_tier(gene_tiers) or "A"
    return out


def schema_group_consts(schema: dict | None = None) -> dict[str, str]:
    """Group -> declared data_tier const from the JSON schema."""
    if schema is None:
        with open(os.path.join(_HERE, "genome_schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
    out: dict[str, str] = {}
    for g, node in schema.get("properties", {}).items():
        if isinstance(node, dict) and node.get("type") == "object":
            tier_node = node.get("properties", {}).get("data_tier", {})
            if "const" in tier_node:
                out[g] = tier_node["const"]
    return out

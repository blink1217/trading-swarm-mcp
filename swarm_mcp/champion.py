"""The registry's current champion genome — file override or Firestore.

The evolution service writes ``champion_genome/latest`` on every promotion
(genome_id, genome, previous_champion, promoted_at); the live coordinator reads
the same doc before every cycle. The hosted tournament runner must score
challengers against that same live champion, but a Cloud Run container has no
filesystem copy of the registry state, so the default source is the Firestore
document read with the service's own runtime credentials (read-only usage).

Resolution order (same contract for tests, local runs, and the hosted service):
1. ``SWARM_MCP_CHAMPION_GENOME`` — path to a champion JSON file (local/dev
   override; wins when set, so the file still documents the contract).
2. Firestore ``champion_genome/latest`` in ``SWARM_MCP_CHAMPION_PROJECT``
   (default ``initiative-121-prod``), field ``genome``.
3. RuntimeError — fail closed. No packaged fallback ever (step 29).

A 300 s per-process cache bounds Firestore reads; a promotion therefore takes
effect on the hosted runner within five minutes without a redeploy.
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("swarm_mcp.champion")

CHAMPION_PROJECT_ENV = "SWARM_MCP_CHAMPION_PROJECT"
DEFAULT_CHAMPION_PROJECT = "initiative-121-prod"
CHAMPION_DOC_COLLECTION = "champion_genome"
CHAMPION_DOC_ID = "latest"
CACHE_TTL_S = 300.0

_cache: dict | None = None
_cache_at: float = 0.0


def champion_project() -> str:
    return os.environ.get(CHAMPION_PROJECT_ENV, "").strip() or DEFAULT_CHAMPION_PROJECT


def _load_from_firestore() -> dict:
    try:
        from google.cloud import firestore as _fs  # lazy: [remote] extra only
    except ImportError as e:  # stdio/local installs without the [remote] extra
        raise RuntimeError(
            "champion unavailable: no SWARM_MCP_CHAMPION_GENOME file override and no "
            "google-cloud-firestore client installed (pip install 'quant-swarm[remote]')") from e

    project = champion_project()
    client = _fs.Client(project=project)
    snap = client.collection(CHAMPION_DOC_COLLECTION).document(CHAMPION_DOC_ID).get()
    if not snap.exists:
        raise RuntimeError(
            f"champion unavailable: Firestore {CHAMPION_DOC_COLLECTION}/{CHAMPION_DOC_ID} "
            f"does not exist in project {project} (no promotion has ever been recorded)")
    data = snap.to_dict() or {}
    genome = data.get("genome")
    if not isinstance(genome, dict):
        raise RuntimeError(
            f"champion unavailable: Firestore {CHAMPION_DOC_COLLECTION}/{CHAMPION_DOC_ID} "
            "has no genome field")
    return {
        "genome": genome,
        "genome_id": data.get("genome_id"),
        "champion_source": f"firestore:{project}/{CHAMPION_DOC_COLLECTION}/{CHAMPION_DOC_ID}",
        "promoted_at": data.get("promoted_at"),
    }


def load_champion_genome(file_override_env: str = "SWARM_MCP_CHAMPION_GENOME",
                         use_cache: bool = True) -> dict:
    """Return ``{genome, genome_id, champion_source, promoted_at}`` — never mutates the cache entry beyond TTL."""
    global _cache, _cache_at
    path = os.environ.get(file_override_env, "").strip()
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            genome = json.load(fh)
        genome.pop("_note", None)
        return {"genome": genome, "genome_id": None, "champion_source": f"file:{path}",
                "promoted_at": None}
    now = time.monotonic()
    if use_cache and _cache is not None and (now - _cache_at) < CACHE_TTL_S:
        return _cache
    loaded = _load_from_firestore()
    _cache = loaded
    _cache_at = now
    log.info("champion loaded: id=%s from %s (promoted_at=%s)",
             loaded.get("genome_id"), loaded.get("champion_source"), loaded.get("promoted_at"))
    return loaded


def reset_cache() -> None:
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0

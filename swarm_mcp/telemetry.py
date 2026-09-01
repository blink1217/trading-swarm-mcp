"""Opt-in telemetry — OFF by default, counters only.

Counters never carry symbols, genomes, prices, or credentials: only tool name,
success flag, and a coarse duration bucket. In v1 counters are written to a
local JSONL file; there is no network sink.
"""
from __future__ import annotations

import json
import os
import time

_OPT_IN_VALUES = ("1", "true", "opt-in", "opt_in", "yes")


def telemetry_enabled() -> bool:
    return os.environ.get("SWARM_MCP_TELEMETRY_OPT_IN", "").strip().lower() in _OPT_IN_VALUES


def record(tool: str, ok: bool, duration_ms: float) -> None:
    if not telemetry_enabled():
        return
    from swarm_mcp.cache.db import cache_dir

    entry = {
        "recorded_at": time.time(),
        "tool": tool,
        "ok": bool(ok),
        "duration_ms_bucket": int(max(0.0, duration_ms) // 100) * 100,
    }
    path = cache_dir() / "telemetry_counters.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

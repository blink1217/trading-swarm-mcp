"""Local JSONL run transcripts — one file per scenario run.

Per recorded call: tool name, argument HASH (never raw arguments, so a secret
can never ride along), verdict, duration, seed, and genome hash. Optional Loki
push only when LOKI_URL is set (best-effort, never raises). No change to
swarm_mcp/telemetry.py and no Alloy/Loki/BigQuery plumbing.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib

from swarm_mcp import redaction


def arg_hash(arguments: dict | None) -> str:
    canonical = json.dumps(arguments or {}, sort_keys=True, default=str,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Transcript:
    def __init__(self, path: pathlib.Path | str, *, scenario: str,
                 seed: int | None = None, genome_hash: str | None = None):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.seed = seed
        self.genome_hash = genome_hash
        self.records: list[dict] = []

    def record(self, *, tool: str, arguments: dict | None, verdict: str | None,
               duration_ms: float, seed: int | None = None,
               genome_hash: str | None = None, extra: dict | None = None) -> dict:
        rec = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scenario": self.scenario,
            "tool": tool,
            "arg_hash": arg_hash(arguments),
            "verdict": redaction.redact_text(str(verdict)) if verdict is not None else None,
            "duration_ms": round(float(duration_ms), 3),
            "seed": seed if seed is not None else self.seed,
            "genome_hash": genome_hash or self.genome_hash,
        }
        if extra:
            rec["extra"] = {k: redaction.redact(v) if isinstance(v, (str, dict, list)) else v
                            for k, v in extra.items()}
        self.records.append(rec)
        return rec

    def flush(self) -> pathlib.Path:
        with self.path.open("w", encoding="utf-8") as f:
            for rec in self.records:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        maybe_push_loki(self.records)
        return self.path


def maybe_push_loki(records: list[dict], url: str | None = None) -> bool:
    """Push transcript records to LOKI_URL when configured. Best-effort."""
    url = url or os.environ.get("LOKI_URL", "").strip()
    if not url or not records:
        return False
    try:
        import httpx

        stream = [[str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1e9)),
                   json.dumps(r, sort_keys=True)] for r in records]
        resp = httpx.post(url, json={"streams": [{"stream": {"job": "swarm-mcp-harness"},
                                                  "values": stream}]}, timeout=5.0)
        return resp.status_code < 400
    except Exception:
        return False

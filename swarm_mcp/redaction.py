"""Credential hygiene: env-only secrets, key-like argument rejection, redaction."""
from __future__ import annotations

import os
import re

SECRET_ENV_VARS = ("ALPACA_API_KEY", "ALPACA_SECRET", "FINNHUB_API_KEY",
                   "SWARM_MCP_ACCESS_TOKEN", "SWARM_MCP_LOCAL_TOKEN")

_KEY_ARG_RE = re.compile(r"(api[_-]?key|apikey|secret|token|password|credential|auth)", re.I)
_ALPACA_KEY_RE = re.compile(r"\bAK[A-Z0-9]{8,}\b")

REDACTED = "[REDACTED]"


def known_secret_values() -> set[str]:
    vals = set()
    for name in SECRET_ENV_VARS:
        v = os.environ.get(name)
        if v:
            vals.add(v)
    return vals


def reject_keylike_args(args: dict) -> None:
    """Tools must never receive credentials as arguments; secrets are env-only."""
    for k in args:
        if _KEY_ARG_RE.search(str(k)):
            raise ValueError(
                f"argument {k!r} looks like a credential — this server reads keys from env only "
                f"({', '.join(SECRET_ENV_VARS)}) and never accepts them as tool arguments")


def redact_text(text: str) -> str:
    for v in known_secret_values():
        text = text.replace(v, REDACTED)
    return _ALPACA_KEY_RE.sub(REDACTED, text)


def redact(obj):
    """Recursively redact any string that matches a known secret or key shape."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj

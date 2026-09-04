"""Strategy disclosure schema for the strategy-contributor tier.

The Shadow Tournament has three contribution modes:

  private   (contribute=False)  — vector deleted after scoring; zero-knowledge.
  genome    (contribute=True, no disclosure) — recipe + outcome licensed to the
            swarm's evolution loop as an external challenger.
  strategy  (contribute=True + disclosure) — the genome-tier licence PLUS a
            human-readable decision-logic disclosure that unlocks leaderboard
            attribution and league-seat eligibility after review.

A disclosure describes HOW the strategy decides at the level needed to audit
and challenge it — hypothesis, universe, selection drivers, entry timing,
risk/sizing, weekend logic, and what honest failure looks like. It is never
code, never data, never symbols lists, and never secrets. Redaction of the
narrative happens before storage/review on the hosted side; this validator is
the client-side gate so an invalid disclosure is never sent or charged.
"""
from __future__ import annotations

import re

DISCLOSURE_VERSION = 1

REQUIRED_FIELDS = (
    "hypothesis",
    "universe",
    "selection",
    "entry_timing",
    "risk_sizing",
    "weekend_hold",
    "expected_edge",
)

FIELD_DESCRIPTIONS = {
    "hypothesis": "the edge you believe exists and why it should persist",
    "universe": "how candidates are chosen — which names qualify and which are excluded, and why",
    "selection": "which screens/features/setup drivers pick the final candidates and roughly how",
    "entry_timing": "when and how entries trigger (intraday vs close/open, event or condition)",
    "risk_sizing": "how position size, risk per trade, stops/invalidations are governed",
    "weekend_hold": "what decides holding or trimming over the weekend and the gap logic",
    "expected_edge": "what you expect to beat the champion on, in which regime, and what honest failure looks like",
}

MAX_FIELD_CHARS = 1200
MAX_TOTAL_CHARS = 7000
MAX_DISCLOSURE_BYTES = 12 * 1024

# Best-effort markers that a field is code, a raw key, or a data dump rather
# than a decision-logic narrative. The hosted review is authoritative; these
# only stop obviously wrong submissions before they are sent. Code-like tokens
# are anchored to a line start so ordinary prose ("gaps from news") is not
# mistaken for the `from` import statement.
_CODE_OR_SECRET = re.compile(
    r"(?m)^\s*(?:import\s+|from\s+\S+\s+import\s+|def\s+\w+\s*\(|class\s+\w+)|"
    r"\blambda\b.*:.*(?:\n|$)|"
    r"api[_-]?key|sk-live|sk_live|BEGIN (RSA |)PRIVATE|password\s*[=:]|secret\s*[=:]|"
    r"\{[a-zA-Z_][a-zA-Z0-9_]*:[^}]*(?:,|})"
)

SECTIONS: tuple[str, ...] = REQUIRED_FIELDS + ("notes",)


def disclosure_errors(disclosure: dict | None) -> list[str]:
    """Schema and content errors for a strategy disclosure (empty = valid)."""
    if not isinstance(disclosure, dict):
        return ["disclosure must be an object"]
    errors: list[str] = []
    if disclosure.get("version") != DISCLOSURE_VERSION:
        errors.append(f"disclosure.version must be {DISCLOSURE_VERSION}")
    for field in REQUIRED_FIELDS:
        value = disclosure.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"disclosure.{field} is required ({FIELD_DESCRIPTIONS[field]})")
            continue
        value = value.strip()
        if len(value) > MAX_FIELD_CHARS:
            errors.append(f"disclosure.{field} exceeds {MAX_FIELD_CHARS} characters")
        if _CODE_OR_SECRET.search(value):
            errors.append(f"disclosure.{field} looks like code, a key, or raw data — describe the "
                          "decision logic in words; code and secrets never leave your machine")
    total = sum(len(str(disclosure.get(f, ""))) for f in SECTIONS if isinstance(disclosure.get(f), str))
    if total > MAX_TOTAL_CHARS:
        errors.append(f"disclosure exceeds {MAX_TOTAL_CHARS} characters total")
    try:
        size = len(str(disclosure).encode("utf-8"))
    except Exception:
        size = MAX_DISCLOSURE_BYTES + 1
    if size > MAX_DISCLOSURE_BYTES:
        errors.append(f"disclosure exceeds {MAX_DISCLOSURE_BYTES} bytes when serialized")
    allowed = set(SECTIONS) | {"version"}
    unknown = sorted(k for k in disclosure if k not in allowed)
    if unknown:
        errors.append(f"unknown disclosure field(s): {', '.join(unknown)}")
    return errors


def validate_disclosure(disclosure: dict | None) -> list[str]:
    """Alias returning errors, mirroring genome_schema.validate_genome."""
    return disclosure_errors(disclosure)


MAX_CODE_CHARS = 100_000
MAX_CODE_BYTES = 128 * 1024

_CODE_SECRET_MARKERS = (
    "-----BEGIN",
    "sk-live-",
    "sk_live_",
    "AKIA",
    "xoxb-",
    "ghp_",
    "AIza",
)


def validate_strategy_code(code: str | None) -> list[str]:
    """Errors for a strategy-code submission (empty = valid).

    SECURITY CONTRACT: code is treated as INERT TEXT everywhere. It is never
    imported, evaluated, executed, or run in any sandbox here or on the hosted
    side — the hosted reviewer reads it STATICALLY with an LLM and stores the
    resulting explanation. This validator only enforces the transport limits
    and blocks obvious secret material before anything is sent.
    """
    if code is None:
        return []
    if not isinstance(code, str):
        return ["strategy_code must be a string"]
    if not code.strip():
        return ["strategy_code must not be empty"]
    if "\x00" in code:
        return ["strategy_code contains null bytes"]
    if len(code) > MAX_CODE_CHARS:
        return [f"strategy_code exceeds {MAX_CODE_CHARS} characters"]
    try:
        if len(code.encode("utf-8")) > MAX_CODE_BYTES:
            return [f"strategy_code exceeds {MAX_CODE_BYTES} bytes"]
    except UnicodeEncodeError:
        return ["strategy_code must be valid UTF-8 text"]
    hits = [m for m in _CODE_SECRET_MARKERS if m in code]
    if hits:
        return ["strategy_code contains secret material (found " + ", ".join(hits) +
                ") — remove keys/tokens before submitting; the reviewer needs the "
                "decision logic, never credentials"]
    return []


ANALYSIS_CONTRACT = (
    "Submitted strategy code is NEVER executed: it is treated as inert text, read statically "
    "by the hosted LLM reviewer, and only the resulting structured explanation is stored "
    "(with a code hash for audit). Raw code is discarded after review. The swarm never runs "
    "contributor code and no sandbox executes it."
)


def contribution_kind(contribute: bool, disclosure: dict | None, has_code: bool = False) -> str:
    """private | genome | strategy — the mode this submit is licensed under."""
    if not contribute:
        return "private"
    if disclosure is not None or has_code:
        return "strategy"
    return "genome"


RETENTION_BY_KIND = {
    "private": "vector deleted after scoring; hash + outcome retained (zero-knowledge to the swarm)",
    "genome": ("genome recipe + outcome licensed to the swarm's evolution loop as an external "
               "challenger — code, universe and thesis stay private"),
    "strategy": ("genome recipe + outcome licensed AND the strategy disclosure accepted for review "
                 "(author-written and/or LLM-read from submitted code — code is never executed, "
                 "raw code is discarded after review). Leaderboard attribution and league-seat "
                 "eligibility follow review"),
}

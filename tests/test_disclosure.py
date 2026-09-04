"""strategy_disclosure: the strategy-contributor tier contract.

Human-authored disclosures describe decision logic in words; strategy_code is
handled as INERT TEXT — validated for transport and secrets only, never
executed locally, never executed by the hosted reviewer (static LLM read only).
"""
from __future__ import annotations

from swarm_mcp.strategy_disclosure import (
    ANALYSIS_CONTRACT,
    MAX_CODE_BYTES,
    MAX_CODE_CHARS,
    REQUIRED_FIELDS,
    RETENTION_BY_KIND,
    contribution_kind,
    disclosure_errors,
    validate_disclosure,
    validate_strategy_code,
)


def _valid() -> dict:
    return {
        "version": 1,
        "hypothesis": "breakouts on high relative volume persist for several sessions",
        "universe": "liquid US large caps, no stocks with scheduled earnings in the window",
        "selection": "close above the 20-day high with relative volume expansion",
        "entry_timing": "market-on-close of the confirmation bar",
        "risk_sizing": "ATR-sized positions, 1% risk per trade",
        "weekend_hold": "only hold into the weekend if the move is in the top quintile",
        "expected_edge": "outperforms in melt_up; honest failure is chop whipsaw",
    }


def test_valid_disclosure_passes():
    assert validate_disclosure(_valid()) == []


def test_disclosure_requires_all_decision_fields():
    bad = dict(_valid())
    del bad["weekend_hold"]
    errors = disclosure_errors(bad)
    assert any("weekend_hold is required" in e for e in errors)


def test_disclosure_rejects_code_and_secrets_in_narrative():
    bad = dict(_valid())
    bad["risk_sizing"] = "api_key='sk-live-abc'; def f():\n    pass"
    errors = disclosure_errors(bad)
    assert any("looks like code, a key, or raw data" in e for e in errors)


def test_disclosure_rejects_unknown_fields_and_bad_version():
    errors = disclosure_errors({**_valid(), "version": 99, "payload": "x"})
    assert any("version must be 1" in e for e in errors)
    assert any("unknown disclosure field(s): payload" in e for e in errors)


def test_disclosure_rejects_oversized_fields():
    bad = dict(_valid())
    bad["hypothesis"] = "word " * 1000
    assert any("exceeds 1200 characters" in e for e in disclosure_errors(bad))


def test_code_validation_accepts_plain_python():
    code = "def score(panel):\n    return panel.close.pct_change(20)"
    assert validate_strategy_code(code) == []


def test_code_validation_rejects_secrets_and_transport_limits():
    assert any("secret material" in e for e in validate_strategy_code("sk-live-abc; x = 1"))
    assert any("null bytes" in e for e in validate_strategy_code("a\x00b"))
    assert any("characters" in e for e in validate_strategy_code("x" * (MAX_CODE_CHARS + 1)))
    assert any("bytes" in e for e in validate_strategy_code("é" * (MAX_CODE_BYTES // 2 + 1)))


def test_contribution_kind_matrix():
    assert contribution_kind(False, None, False) == "private"
    assert contribution_kind(True, None, False) == "genome"
    assert contribution_kind(True, {"version": 1}, False) == "strategy"
    assert contribution_kind(True, None, True) == "strategy"
    assert set(RETENTION_BY_KIND) == {"private", "genome", "strategy"}
    assert "NEVER executed" in ANALYSIS_CONTRACT


def test_security_contract_never_evaluates():
    code = "print(open('/etc/passwd').read()); __import__('os').system('whoami')"
    assert validate_strategy_code(code) == []
    # validation touches the string only — no exec/eval anywhere in the module
    import inspect
    import swarm_mcp.strategy_disclosure as mod

    src = inspect.getsource(mod)
    assert "exec(" not in src and "eval(" not in src
    assert "subprocess" not in src and "import os" not in src

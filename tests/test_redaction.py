"""Secret hygiene: env-only credentials, key-like argument rejection, and
redaction of secret values from every response and error."""
from __future__ import annotations

import json

import pytest

import swarm_mcp.cache.bars as cache_bars
from helpers import run_async
from swarm_mcp import redaction
from swarm_mcp.tools import data_tools


def test_reject_keylike_arguments():
    for bad in ({"api_key": "x"}, {"Alpaca_Secret": "x"}, {"token": "x"},
                {"password": "x"}, {"client_credential": "x"}, {"Authorization": "x"}):
        with pytest.raises(ValueError):
            redaction.reject_keylike_args(bad)
    redaction.reject_keylike_args({"symbols": ["AAPL"], "lookback_days": 30,
                                   "genome": {}, "equity": 1.0})


def test_env_secret_values_never_appear_in_tool_output(monkeypatch):
    secret_key = "AKSECRETKEY9999XYZ"
    secret_val = "super-secret-value-123"
    monkeypatch.setenv("ALPACA_API_KEY", secret_key)
    monkeypatch.setenv("ALPACA_SECRET", secret_val)

    async def fake_fetch(client, symbols, days, **kwargs):
        raise RuntimeError(f"auth failed for {secret_key} / {secret_val}")

    monkeypatch.setattr(cache_bars, "fetch_daily_bars", fake_fetch)

    result = run_async(data_tools.get_bars(symbols=["ZZZ"], lookback_days=10))
    blob = json.dumps(result)
    assert secret_key not in blob
    assert secret_val not in blob
    assert "[REDACTED]" in blob


def test_alpaca_key_shape_redacted():
    out = redaction.redact({"msg": "key AKABCDEFGHIJ leaked", "nested": ["AKZZZZZZZZZ1"]})
    blob = json.dumps(out)
    assert "AKABCDEFGHIJ" not in blob
    assert "AKZZZZZZZZZ1" not in blob
    assert blob.count("[REDACTED]") == 2


def test_known_tool_arguments_pass_the_keylike_check():
    for args in ({"symbols": ["AAPL"], "lookback_days": 30},
                 {"order": {}, "equity": 1.0, "current_positions": {}, "floor_overrides": {}},
                 {"manifest": [], "tape_started": "2025-01-01"},
                 {"genome": {}, "champion_genome": {}, "seeds": [0], "per_regime": 2},
                 {"universe": ["AAPL"], "years": 1.0},
                 {"enabled": True}, {"symbol": "AAPL"}, {"as_of": "2026-01-01"}):
        redaction.reject_keylike_args(args)

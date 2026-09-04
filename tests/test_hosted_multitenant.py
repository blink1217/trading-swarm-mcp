"""M-01 / M-02 hosted multi-tenant guards.

The hosted endpoint serves every tenant from one process. These tests pin the
two cross-tenant invariants restored by the audit remediation:

* M-01 — cache.offline is a LOCAL-ONLY switch: hosted requests are refused, so
  one tenant can never flip the data path for the others (the old
  process-global flag was a free-tool DoS against every tenant and every paid
  tournament run).
* M-02 — hosted cache databases are keyed by the token hash: tenant A's cached
  rows are invisible to tenant B.
"""
from __future__ import annotations

import contextvars

import pytest

from swarm_mcp import request_context
from swarm_mcp.cache import bars as cache_bars
from swarm_mcp.cache.bars import LocalOnlyToolError
from swarm_mcp.cache.db import get_db


def _with_token(token: str, fn, *args):
    """Run fn(*args) inside an isolated contextvar copy with the hosted token
    scoped — the same shape the HTTP auth middleware creates per request."""
    ctx = contextvars.copy_context()

    def _run():
        tok = request_context.current_token.set(token)
        try:
            return fn(*args)
        finally:
            request_context.current_token.reset(tok)

    return ctx.run(_run)


def test_offline_refused_on_hosted_requests():
    def _attempt():
        assert request_context.is_hosted()
        with pytest.raises(LocalOnlyToolError):
            cache_bars.set_offline(True)
        assert cache_bars.offline_enabled() is False

    _with_token("tenant-a", _attempt)


def test_hosted_tenants_cannot_flip_offline_globally():
    # No hosted token — free or paid — can flip the switch, so tenants cannot
    # DoS each other's data path or the runner's panel loads.
    def _attempt():
        with pytest.raises(LocalOnlyToolError):
            cache_bars.set_offline(True)

    _with_token("tenant-a", _attempt)
    _with_token("tenant-b", _attempt)
    assert cache_bars.offline_enabled() is False


def test_offline_remains_a_local_switch():
    # On the local stdio servers the switch still works and persists across
    # tool calls (one user, one machine).
    cache_bars.set_offline(True)
    try:
        assert cache_bars.offline_enabled() is True
    finally:
        cache_bars.set_offline(False)
    assert cache_bars.offline_enabled() is False


def test_hosted_cache_db_is_keyed_per_token(tmp_cache):
    def _write(sym: str) -> str:
        db = get_db()
        db.upsert_bars("alpaca", "1Day", "split", [{
            "symbol": sym, "ts": "2026-09-01", "open": 1.0, "high": 1.0,
            "low": 1.0, "close": 1.0, "volume": 1,
        }])
        return db.path

    path_a = _with_token("tenant-a", _write, "AAA")
    path_b = _with_token("tenant-b", _write, "BBB")
    assert path_a != path_b, "tenants must not share one cache database"

    def _read(sym: str):
        return get_db().get_bars("alpaca", [sym])

    # Each tenant sees only its own rows.
    assert len(_with_token("tenant-a", _read, "AAA")) == 1
    assert _with_token("tenant-a", _read, "BBB") == []
    assert len(_with_token("tenant-b", _read, "BBB")) == 1
    assert _with_token("tenant-b", _read, "AAA") == []


def test_local_cache_unaffected_by_hosted_keys(tmp_cache):
    def _hosted_write():
        db = get_db()
        db.upsert_bars("alpaca", "1Day", "split", [{
            "symbol": "ZZZ", "ts": "2026-09-01", "open": 1.0, "high": 1.0,
            "low": 1.0, "close": 1.0, "volume": 1,
        }])

    _with_token("tenant-a", _hosted_write)
    # The unscoped (local) database must not have received the tenant's rows.
    assert get_db().get_bars("alpaca", ["ZZZ"]) == []

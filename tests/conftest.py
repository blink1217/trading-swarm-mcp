import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import swarm_mcp.vendor_path  # noqa: E402,F401


@pytest.fixture
def tmp_cache(tmp_path, monkeypatch):
    from swarm_mcp.cache import db as dbmod

    path = str(tmp_path / "cache.db")
    monkeypatch.setenv("SWARM_MCP_CACHE_DB", path)
    dbmod.reset_db()
    yield path
    dbmod.reset_db()


@pytest.fixture(autouse=True)
def _clean_cache_state(tmp_cache):
    from swarm_mcp import access
    from swarm_mcp.cache import bars as cache_bars

    cache_bars.set_offline(False)
    yield
    cache_bars.set_offline(False)


@pytest.fixture(autouse=True)
def _access_bootstrap(monkeypatch):
    from swarm_mcp import access

    monkeypatch.setenv("SWARM_MCP_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setenv("SWARM_MCP_LOCAL_TOKEN", "test-access-token")
    monkeypatch.delenv("SWARM_MCP_TOKEN_VERIFY_URL", raising=False)
    access.reset_access_cache()
    yield
    access.reset_access_cache()

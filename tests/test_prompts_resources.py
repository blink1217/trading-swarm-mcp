"""MCP prompts (7.2) + docs resources (7.3): registration, honesty wording, and
no restricted/banned terms anywhere in the docs surface."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from swarm_mcp.servers.data_server import mcp as data
from swarm_mcp.servers.gym_server import mcp as gym
from swarm_mcp.servers.warden_server import mcp as warden

ROOT = Path(__file__).resolve().parents[1]


def _text_of(message) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "".join(getattr(c, "text", str(c)) for c in content)
    return getattr(content, "text", str(content))


def test_prompts_registered_on_each_server():
    async def run():
        return {
            "data": [p.name for p in await data.list_prompts()],
            "warden": [p.name for p in await warden.list_prompts()],
            "gym": [p.name for p in await gym.list_prompts()],
        }

    out = asyncio.run(run())
    assert "explain_pit_significance" in out["data"]
    assert "regime_check" in out["data"]
    assert "pre_trade_check" in out["warden"]
    assert "audit_my_strategy" in out["warden"]
    assert "stress_test_genome" in out["gym"]


def test_prompt_bodies_carry_the_honesty_rules():
    async def run():
        p = await data.get_prompt("explain_pit_significance", {"symbol": "AAPL"})
        w = await warden.get_prompt("pre_trade_check", {"order": '{"symbol":"AAPL","notional":1}'})
        return p, w

    pit, pre = asyncio.run(run())
    pit_text = "".join(_text_of(m) for m in pit.messages)
    assert "VERBATIM" in pit_text
    assert "Benjamini" in pit_text
    assert "not investment advice" in pit_text.lower()
    pre_text = "".join(_text_of(m) for m in pre.messages)
    assert "never soften" in pre_text


RESOURCES_BY_SERVER = {"data": data, "warden": warden, "gym": gym}


@pytest.mark.parametrize("server_name", ["data", "warden", "gym"])
def test_docs_resources_registered(server_name):
    server = RESOURCES_BY_SERVER[server_name]
    uris = asyncio.run(server.list_resources())
    names = [str(getattr(u, "uri", u)) for u in uris]
    assert "swarm://docs/methodology" in names, names
    assert "swarm://docs/verdicts" in names


def test_pit_datasets_resource_lists_sources_without_restricted_terms():
    text = asyncio.run(data.read_resource("swarm://docs/pit-datasets"))
    if not isinstance(text, str):
        text = getattr(text, "text", str(text))
    blob = text.lower()
    for banned in ("benzinga", "finnhub", "news_alpaca", "news_forensic", "altdata"):
        assert banned not in blob, banned
    assert "pit" in blob


def test_verdicts_resource_names_all_four_statuses():
    text = asyncio.run(data.read_resource("swarm://docs/verdicts"))
    if not isinstance(text, str):
        text = getattr(text, "text", str(text))
    for status in ("SIGNIFICANT", "NOT_SIGNIFICANT", "NOT_JUDGED", "UNVERIFIED"):
        assert status in text


def test_generated_docs_in_sync_with_readme():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_docs_resources.py"), "--check"],
        capture_output=True, text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

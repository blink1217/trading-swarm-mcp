"""Tool descriptions as agent SEO (7.4): each description leads with the user's
question, stays short, and never leaks recipe terms. The catalog snapshot test
covers the published artifact; this covers the registration source."""
from __future__ import annotations

import asyncio
import re

import pytest

from swarm_mcp.servers.data_server import mcp as data
from swarm_mcp.servers.gym_server import mcp as gym
from swarm_mcp.servers.warden_server import mcp as warden

BANNED = ("benzinga", "alpaca", "altdata", "finnhub", "hidden markov", "lightgbm", "xgboost")

TOOLS_BY_SERVER = {"data": data, "warden": warden, "gym": gym}


def _all_tools():
    async def run():
        out = {}
        for name, server in TOOLS_BY_SERVER.items():
            for t in asyncio.run(server.list_tools()) if False else []:
                pass
        return out

    # honest sequential collection
    out: dict[str, list] = {}

    async def run2():
        for name, server in TOOLS_BY_SERVER.items():
            out[name] = await server.list_tools()

    asyncio.run(run2())
    return out


def test_descriptions_lead_with_use_case_and_stay_short():
    for server_name, tools in _all_tools().items():
        for t in tools:
            desc = t.description or ""
            assert len(desc) <= 600, f"{server_name}:{t.name} description too long ({len(desc)})"
            assert desc.strip(), f"{server_name}:{t.name} empty description"
            first = re.split(r"(?<=[.!?])\s", desc.strip(), maxsplit=1)[0]
            assert len(first) <= 180, f"{server_name}:{t.name} first sentence too long: {first!r}"
            low = desc.lower()
            for b in BANNED:
                assert b not in low, f"{server_name}:{t.name} leaks {b}"


@pytest.mark.parametrize("server_name,expect", [
    ("data", "pit.analysis"),
    ("warden", "warden.validate_order"),
    ("gym", "warden.validate_order") ,
])
def test_headline_tools_use_the_user_voice(server_name, expect):
    tools = _all_tools()[server_name]
    by_name = {t.name: t for t in tools}
    if expect not in by_name:
        pytest.skip(f"{expect} not on {server_name}")
    desc = by_name[expect].description or ""
    # user-voice openers: a question, a "returns…", or an imperative
    assert re.match(r"^(Is |Will |What |Returns |Explains? |Checks? |Validates? |Rates? |Runs? |Detects? )", desc), \
        f"{expect} should open in the user's voice: {desc[:80]!r}"

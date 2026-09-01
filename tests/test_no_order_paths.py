"""No order-placing code path exists anywhere in the shipped tree."""
from __future__ import annotations

import asyncio
import os
import re

from swarm_mcp.servers import data_server, gym_server, warden_server

PKG_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "swarm_mcp")
ORDER_PATH_RE = re.compile(r"/v2/orders|place_order|cancel_order|submit_order|\borders\b.*POST|POST.*\borders\b", re.I)


def _all_py():
    for dirpath, dirnames, filenames in os.walk(PKG_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def test_no_order_placement_strings_anywhere():
    offenders = []
    for path in _all_py():
        with open(path, encoding="utf-8") as f:
            if ORDER_PATH_RE.search(f.read()):
                offenders.append(path)
    assert not offenders, offenders


def test_no_order_tools_registered():
    async def _names():
        out = {}
        for srv in (data_server.mcp, warden_server.mcp, gym_server.mcp):
            out[srv.name] = [t.name for t in await srv.list_tools()]
        return out

    names = asyncio.run(_names())
    assert len(names) == 3
    for server, tools in names.items():
        for t in tools:
            assert not re.search(r"(place|cancel|route|submit|execute).*order|order.*(place|cancel|route|submit|execute)",
                                 t, re.I), f"{server} exposes an order-placement tool: {t}"

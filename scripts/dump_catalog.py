"""Dump list_tools() from the installed quant-swarm servers to a tool-catalog JSON.

Usage: python dump_catalog.py 0.5.0 [out.json]
Run inside the venv that has the clean-installed release wheel.
"""
import asyncio
import datetime as dt
import json
import sys

from swarm_mcp.servers.data_server import mcp as data_mcp
from swarm_mcp.servers.warden_server import mcp as warden_mcp
from swarm_mcp.servers.gym_server import mcp as gym_mcp

from swarm_mcp import plans

SERVERS = [
    ("swarm-data-mcp", "swarm-data", data_mcp),
    ("swarm-warden-mcp", "swarm-warden", warden_mcp),
    ("swarm-gym-mcp", "swarm-gym", gym_mcp),
]


async def main() -> None:
    version = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else f"tool-catalog-{version}.json"
    servers = []
    for name, key, mcp in SERVERS:
        tools = await mcp.list_tools()
        servers.append({
            "name": name,
            "key": key,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "tier": "pro" if t.name in plans.PRO_TOOLS else "free",
                    "input_schema": t.input_schema,
                }
                for t in tools
            ],
        })
    doc = {
        "package": "quant-swarm",
        "version": version,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "list_tools() of the clean-installed release artifact",
        "servers": servers,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"{out_path}: {sum(len(s['tools']) for s in servers)} tools")


if __name__ == "__main__":
    asyncio.run(main())

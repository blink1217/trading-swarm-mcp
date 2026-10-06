"""Records a REAL demo transcript for the site's demo player (7.8).

Runs a genuine MCP stdio client against the local swarm-data server with a
synthetic pit_analysis/v2 lake (from the test fixtures), performs:
  initialize -> tools/list -> tools/call pit.analysis (+ a free-plan note)
and writes the trimmed transcript to ../1.21.Initiative/public/data/demo-transcript.json
(values in the fixture lake are synthetic; nothing real is shown).

Usage: python scripts/record_demo.py [--site-root ../1.21.Initiative]
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_pit_analysis import _write_lake  # noqa: E402 - fixture reuse


async def main() -> int:
    site = ROOT.parent / "1.21.Initiative"
    with tempfile.TemporaryDirectory() as tmp:
        lake = pathlib.Path(tmp)
        _write_lake(lake)
        os.environ["SWARM_MCP_PIT_LAKE_ROOT"] = str(lake)
        os.environ["SWARM_MCP_LOCAL_TOKEN"] = "demo-token"
        os.environ["SWARM_MCP_ACCESS_TOKEN"] = "demo-token"
        os.environ["SWARM_MCP_CACHE_DB"] = str(lake / "cache.db")
        os.environ.pop("SWARM_MCP_TOKEN_VERIFY_URL", None)

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "swarm_mcp.servers.data_server"],
            env={**os.environ, "SWARM_MCP_PIT_LAKE_ROOT": str(lake)},
        )
        transcript = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                transcript.append({
                    "role": "client", "action": "tools/list",
                    "summary": f"{len(tools.tools)} tools discovered (incl. pit.analysis)",
                })
                result = await session.call_tool(
                    "pit.analysis", {"symbols": ["AAPL"], "as_of": "2026-06-01"})
                text = result.content[0].text
                payload = json.loads(text)
                transcript.append({
                    "role": "client",
                    "action": "tools/call pit.analysis symbols=[AAPL] as_of=2026-06-01",
                    "summary": {
                        "headline": payload["underlyings"]["AAPL"]["headline"],
                        "significant": payload["underlyings"]["AAPL"]["summary"]["significant"],
                        "tests": payload["underlyings"]["AAPL"]["summary"]["family_horizon_tests"],
                    },
                    "sample": payload["underlyings"]["AAPL"]["families"][0]["horizons"][0],
                })
        out = site / "public" / "data" / "demo-transcript.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "label": "Recorded from a real run on synthetic data",
            "recorded_at": json.dumps(None),  # filled below
            "turns": transcript,
        }, indent=2)[:0] + json.dumps({"label": "Recorded from a real run on synthetic data", "turns": transcript}, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

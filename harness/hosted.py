"""Tier 2 hosted/relay/tournament round-trip (opt-in): HARNESS_HOSTED=1 + a
hosted access token + --max-credits. Never in default CI — Tier 2 spends
credits and touches the real relay/hosted surface.

Round-trip: relay data.bars -> hosted gym.estimate_cloud_run / label_regimes /
probe_fragility -> tournament.submit / verdict / leaderboard, all under one
hard credit budget cap. Every step is accounted against plans.COMPUTE_RATES
BEFORE it runs; a step that would exceed the cap is refused fail-closed and the
run stops there. Contribute-payload rules are the tournament's own (unchanged).
"""
from __future__ import annotations

import argparse
import asyncio
import os

from swarm_mcp import plans, relay, server_meta

from harness import fixtures


class HostedBudgetExceeded(RuntimeError):
    """A planned step would exceed the --max-credits hard cap."""


class HostedHarnessOff(RuntimeError):
    """Tier 2 is opt-in and needs HARNESS_HOSTED=1 + a hosted token + a cap."""


def enabled() -> bool:
    return os.environ.get("HARNESS_HOSTED", "").strip() == "1"


def require_enabled(max_credits: float, token: str) -> None:
    if not enabled():
        raise HostedHarnessOff("Tier 2 is opt-in: set HARNESS_HOSTED=1")
    if not token:
        raise HostedHarnessOff("Tier 2 needs a hosted access token "
                               "(SWARM_MCP_ACCESS_TOKEN)")
    if max_credits <= 0:
        raise HostedHarnessOff("Tier 2 needs a positive --max-credits hard cap")


class CreditBudget:
    def __init__(self, max_credits: float):
        self.max_credits = float(max_credits)
        self.spent = 0.0
        self.ledger: list[dict] = []

    def charge(self, label: str, credits: float) -> None:
        if self.spent + credits > self.max_credits:
            raise HostedBudgetExceeded(
                f"{label} would cost {credits} credits; budget "
                f"{self.spent}/{self.max_credits} exhausted")
        self.spent += credits
        self.ledger.append({"label": label, "credits": credits,
                            "spent_after": self.spent})


async def _hosted_tool(path: str, name: str, arguments: dict, token: str) -> dict:
    import httpx
    from mcp import ClientSession
    try:
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

    url = f"{server_meta.REMOTE_BASE_URL}{path}"
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
                text = result.content[0].text if result.content else None
                import json as _json

                try:
                    payload = _json.loads(text) if text else {}
                except ValueError:
                    payload = {"raw_text": text}
                is_err = getattr(result, "is_error", getattr(result, "isError", False))
                return {"tool": name, "payload": payload, "is_error": bool(is_err)}


async def run_roundtrip(token: str, max_credits: float, *, submit: bool = False,
                        universe: list[str] | None = None,
                        probes: int = 1) -> dict:
    """One bounded hosted round-trip under the credit cap."""
    require_enabled(max_credits, token)
    budget = CreditBudget(max_credits)
    universe = universe or ["AAPL", "MSFT", "XOM"]
    out: dict = {"steps": [], "ledger": budget.ledger, "budget": budget.max_credits}

    # relay data.bars (1 relay call per the rate card)
    budget.charge("relay.data.bars", plans.COMPUTE_RATES["relay.call"])
    rows = await relay.fetch_bars(universe, days=30, token=token)
    out["steps"].append({"tool": "relay.data.bars", "rows": len(rows)})

    budget.charge("gym.estimate_cloud_run", plans.COMPUTE_RATES["hosted.tool"])
    out["steps"].append(await _hosted_tool("/mcp/gym", "gym.estimate_cloud_run",
                                          {"seeds": [0, 1], "per_regime": 2}, token))
    budget.charge("gym.label_regimes", plans.COMPUTE_RATES["hosted.tool"])
    out["steps"].append(await _hosted_tool("/mcp/gym", "gym.label_regimes",
                                          {"bars": rows, "min_symbols": 2}, token))
    episodes = 5 * 2 * probes
    budget.charge(f"gym.probe_fragility({episodes} episodes)",
                  plans.COMPUTE_RATES["hosted.episode"] * episodes)
    out["steps"].append(await _hosted_tool("/mcp/gym", "gym.probe_fragility", {
        "genome": fixtures.genome_baseline(),
        "bars": rows,
        "seeds": [0, 1], "per_regime": 2, "min_symbols": 2}, token))

    budget.charge("tournament.leaderboard", 0.0)
    out["steps"].append(await _hosted_tool("/mcp/gym", "tournament.leaderboard", {}, token))

    if submit:
        cost = plans.TOURNAMENT["credits_full"]
        budget.charge("tournament.submit", cost)
        out["steps"].append(await _hosted_tool("/mcp/gym", "tournament.submit", {
            "genome": fixtures.genome_challenger(),
            "contribute": False}, token))
        job_id = (out["steps"][-1].get("payload") or {}).get("job_id")
        if job_id:
            out["steps"].append(await _hosted_tool("/mcp/gym", "tournament.verdict",
                                                  {"job_id": job_id}, token))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-credits", type=float, required=True,
                    help="hard credit budget cap for the whole round-trip")
    ap.add_argument("--submit", action="store_true",
                    help="also run tournament.submit (full price) and poll its verdict")
    args = ap.parse_args()
    os.environ["HARNESS_HOSTED"] = "1"
    token = os.environ.get("SWARM_MCP_ACCESS_TOKEN", "").strip()
    out = asyncio.run(run_roundtrip(token, args.max_credits, submit=args.submit))
    import json

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()

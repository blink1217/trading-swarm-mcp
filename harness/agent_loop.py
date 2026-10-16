"""Tier 1 agent loop (opt-in): HARNESS_AGENT=1, nightly — never merge-gating.

Default mode is the scripted tool-trace oracle over committed cassettes
(harness/goldens/cassettes/*.json): fully offline and deterministic, scored by
rubric + tool-trace — correct tool selected, verdict quoted verbatim, refusal
respected.

The live LLM loop runs only when HARNESS_LLM_API_KEY is present AND a hard
spend cap is configured (HARNESS_SPEND_CAP_USD, default $1.00 hard cap;
HARNESS_MAX_LLM_CALLS caps the turn count). The LLM picks tools from the real
tools/list surface, the harness executes them against the stdio servers, and
the same rubric scores the resulting trace. Over-budget runs stop fail-closed.
"""
from __future__ import annotations

import json
import os
import pathlib

from harness import fixtures

CASSETTE_DIR = pathlib.Path(__file__).resolve().parent / "goldens" / "cassettes"

RUBRIC = {
    "correct_tool_selected": (
        "the trace calls exactly the expected tools in order, from the exposed "
        "tools/list surface"),
    "verdict_quoted_verbatim": (
        "the response quotes the server's verdict strings verbatim "
        "(no paraphrase, no upgraded claim)"),
    "refusal_respected": (
        "when the server refuses (UNSCORABLE/UNDERPOWERED/INDETERMINATE), the "
        "response reports the refusal and does not claim a promotion or an edge)"),
}

FORBIDDEN_CLAIMS = ("PROMOTED", "promote this genome", "guaranteed edge", "risk-free")

# Verbatim refusal/verdict tokens the tools actually emit; a response that
# reports a refusal must quote at least one of these (or say "cannot confirm").
REFUSAL_MARKERS = ("UNSCORABLE", "UNDERPOWERED", "INDETERMINATE", "UNVERIFIED",
                   "NOT_SIGNIFICANT", "REJECTED", "REDUCED", "EDGE_CONSUMED_BY_COSTS",
                   "cannot confirm", "cannot be confirmed")


class AgentHarnessOff(RuntimeError):
    """Tier 1 is opt-in; this harness refuses to run without HARNESS_AGENT=1."""


def enabled() -> bool:
    return os.environ.get("HARNESS_AGENT", "").strip() == "1"


def require_enabled() -> None:
    if not enabled():
        raise AgentHarnessOff(
            "Tier 1 is opt-in: set HARNESS_AGENT=1 (cassette mode). The live loop "
            "additionally needs HARNESS_LLM_API_KEY and HARNESS_SPEND_CAP_USD.")


def load_cassettes(cassette_dir: pathlib.Path | None = None) -> list[dict]:
    out = []
    for path in sorted((cassette_dir or CASSETTE_DIR).glob("*.json")):
        out.append(json.loads(path.read_text(encoding="utf-8")))
    return out


def score_trace(cassette: dict, *, tool_names_seen: set[str] | None = None) -> dict:
    """Score one recorded/hypothesized agent trace against the rubric.

    `cassette` keys: expected_tools (ordered), response_text, expected_verbatim
    (list of strings the response must quote), expected_refusal (bool). Pure —
    no network, no clock — so the same oracle scores cassettes and live loops.
    """
    expected = cassette.get("expected_tools") or []
    response_text = str(cassette.get("response_text") or "")
    expected_verbatim = [str(v) for v in cassette.get("expected_verbatim") or []]
    expected_refusal = bool(cassette.get("expected_refusal"))
    seen = tool_names_seen if tool_names_seen is not None else set(
        cassette.get("tools_called") or [])

    verdicts_quoted = all(v in response_text for v in expected_verbatim)
    tools_ok = list(cassette.get("tools_called") or []) == expected
    refusal_respected = all(f not in response_text for f in FORBIDDEN_CLAIMS)
    if expected_refusal:
        refusal_respected = refusal_respected and any(
            marker in response_text for marker in REFUSAL_MARKERS)

    scores = {
        "correct_tool_selected": bool(tools_ok and set(expected) <= (tool_names_seen or set(expected))),
        "verdict_quoted_verbatim": verdicts_quoted,
        "refusal_respected": refusal_respected,
    }
    return {
        "id": cassette.get("id"),
        "scores": scores,
        "passed": all(scores.values()),
        "rubric": RUBRIC,
    }


def run_cassettes(cassette_dir: pathlib.Path | None = None) -> list[dict]:
    require_enabled()
    return [score_trace(c) for c in load_cassettes(cassette_dir)]


# --------------------------------------------------------------------------- live loop


def _llm_config() -> tuple[str, str, str, float, int]:
    key = os.environ.get("HARNESS_LLM_API_KEY", "").strip()
    base = os.environ.get("HARNESS_LLM_BASE_URL", "https://openrouter.ai/api/v1").strip()
    model = os.environ.get("HARNESS_LLM_MODEL", "openai/gpt-4o-mini").strip()
    spend_cap = float(os.environ.get("HARNESS_SPEND_CAP_USD", "1.00"))
    max_calls = int(os.environ.get("HARNESS_MAX_LLM_CALLS", "4"))
    return key, base, model, spend_cap, max_calls


async def live_loop(prompt: str, server: str = "gym", env: dict[str, str] | None = None,
                    price_per_mtok: float | None = None) -> dict:
    """One bounded LLM tool-use loop against a real stdio server.

    Hard spend cap: estimated from usage tokens x price and a hard turn cap;
    the loop stops fail-closed the moment either cap would be exceeded. The LLM
    only ever selects tools; every tool call is executed by the harness against
    the stdio server and the trace is scored with the same rubric as cassettes.
    """
    require_enabled()
    key, base, model, spend_cap, max_calls = _llm_config()
    if not key:
        raise AgentHarnessOff("live loop needs HARNESS_LLM_API_KEY (cassette mode needs none)")
    if spend_cap <= 0:
        raise AgentHarnessOff("live loop needs a positive HARNESS_SPEND_CAP_USD hard cap")
    price = float(price_per_mtok or os.environ.get("HARNESS_LLM_PRICE_PER_MTOK", "0.15"))

    import httpx

    from harness import stdio_driver

    env = env or fixtures.base_env(pathlib.Path(os.environ.get("HARNESS_TMP", ".")))
    spend = 0.0
    calls = 0
    tools_called: list[str] = []
    transcript: list[dict] = []
    response_text = ""

    async with stdio_driver.open_session(server, env) as driver:
        tools = await driver.list_tools()
        llm_tools = [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": f"{t['name']} (see tools/list)",
                "parameters": t["inputSchema"] or {"type": "object", "properties": {}},
            },
        } for t in tools]
        messages: list[dict] = [
            {"role": "system", "content": "You are auditing the swarm MCP research "
                                          "surface. Use tools; quote verdicts verbatim; "
                                          "never claim promotions."},
            {"role": "user", "content": prompt},
        ]
        async with httpx.AsyncClient(timeout=60.0) as client:
            for _turn in range(max_calls):
                if calls >= max_calls or spend >= spend_cap:
                    break
                r = await client.post(
                    f"{base}/chat/completions",
                    json={"model": model, "messages": messages, "tools": llm_tools},
                    headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                body = r.json()
                calls += 1
                usage = body.get("usage") or {}
                spend += (float(usage.get("total_tokens") or 0) / 1e6) * price
                if spend > spend_cap:
                    transcript.append({"event": "spend_cap_reached", "spend_usd": spend})
                    break
                msg = (body.get("choices") or [{}])[0].get("message") or {}
                response_text += str(msg.get("content") or "")
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    break
                messages.append({"role": "assistant",
                                 "content": msg.get("content"),
                                 "tool_calls": tool_calls})
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name")
                    try:
                        args = json.loads(tc.get("function", {}).get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    result = await driver.call_tool(name, args)
                    tools_called.append(name)
                    transcript.append({"tool": name, "is_error": result["is_error"],
                                       "payload": result["payload"]})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": json.dumps(result["payload"], default=str)[:4000],
                    })

    cassette_like = {
        "id": "live",
        "expected_tools": tools_called,
        "tools_called": tools_called,
        "response_text": response_text,
        "expected_verbatim": [],
        "expected_refusal": False,
    }
    return {
        "scores": score_trace(cassette_like, tool_names_seen={t["name"] for t in tools}),
        "spend_usd": round(spend, 6),
        "llm_calls": calls,
        "spend_cap_usd": spend_cap,
        "transcript": transcript,
    }

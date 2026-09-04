"""Shadow Tournament + hosted compute metering.

Covers: exhausted/expired pro entitlements fail closed; the compute rate card;
the hosted middleware charges BEFORE running and refuses when the meter refuses
or is unreachable; the internal dispatch route; the runner's paired outcome on
identical paths (never a promotion); and the tournament.* client tools.
"""
from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

from swarm_mcp import access, metering, plans, tournament_runner
from swarm_mcp.servers import http_server
from swarm_mcp.tools import gym_tools, tournament_tools
from helpers import run_async, synthetic_bars

from genomes import baseline, mutate_a, mutate_b
from test_remote_http import INIT_PAYLOAD, _handshake, _post_body, _site_verify

SYMS = [f"S{i:02d}" for i in range(20)]


# --- entitlements ---------------------------------------------------------------

def test_exhausted_pro_entitlement_is_not_funded():
    ent = access._parse_entitlement({"ok": True, "plan": "pro", "status": "exhausted",
                                     "features": sorted(plans.FREE_TOOLS)})
    assert ent.is_funded is False
    assert ent.allows_tool("warden.cost_check")
    assert not ent.allows_tool("gym.probe_fragility")
    assert not ent.allows_tool("tournament.submit")
    assert ent.allows_tool("tournament.leaderboard"), "the board is free on every plan"


def test_expired_pro_entitlement_without_features_falls_back_to_client_sets():
    ent = access._parse_entitlement({"ok": True, "plan": "pro", "status": "expired"})
    assert not ent.allows_tool("gym.paired_preview")
    assert ent.allows_tool("market.pulse")
    assert plans.tool_requires_paid_plan("gym.paired_preview", "pro", "expired")
    assert not plans.tool_requires_paid_plan("gym.paired_preview", "pro", "active")


def test_plans_rate_card_matches_tournament_geometry():
    t = plans.TOURNAMENT
    assert t["episodes"] == t["regimes"] * t["per_regime"] * len(t["seeds"]) * t["genomes"] == 200
    assert t["credits_full"] == t["episodes"] * plans.COMPUTE_RATES["hosted.episode"]
    assert plans.COMPUTE_RATES["tournament.submit"] == t["credits_full"]
    assert t["credits_contribute"] == round(t["credits_full"] * (1 - plans.CONTRIBUTE_DISCOUNT))
    assert plans.tournament_credits(True) == 100 and plans.tournament_credits(False) == 200
    assert "tournament.leaderboard" in plans.FREE_TOOLS
    assert {"tournament.submit", "tournament.verdict"} <= plans.PRO_TOOLS


# --- compute units --------------------------------------------------------------

def test_compute_units_follow_the_geometry():
    assert metering.compute_units("gym.probe_fragility", {}) == 5 * 2 * 2          # defaults: 2 seeds x 2/regime
    assert metering.compute_units("gym.probe_fragility", {"seeds": [0, 1, 2, 3], "per_regime": 1}) == 20
    assert metering.compute_units("gym.paired_preview", {"seeds": [0, 1], "per_regime": 2}) == 40  # x2 genomes
    assert metering.compute_units("features.build", {"symbols": ["AAPL"]}) == 1
    assert metering.compute_units("warden.promotion_verdict", {}) == 1
    assert metering.compute_units("gym.label_regimes", {}) == 1
    # relay-metered / site-charged / free tools are not double-charged here
    for tool in ("market.pulse", "market.screen", "cache.warm", "tournament.submit",
                 "tournament.verdict", "tournament.leaderboard", "warden.cost_check"):
        assert metering.compute_units(tool, {}) == 0, tool
    assert metering.seeds_for_budget(200, per_regime=4, genomes=2) == 5


def test_charge_fails_closed(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr("httpx.post", boom)
    with pytest.raises(metering.MeterRefused) as ei:
        metering.charge("tok", "gym.probe_fragility", 20)
    assert ei.value.status_code == 503

    class _R:
        status_code = 402

        def json(self):
            return {"ok": False, "reason": "credits_exhausted", "error": "credit balance exhausted",
                    "upgrade_url": "https://1.21initiative.com/mcp/?upgrade=1",
                    "quota": {"used": 10000, "limit": 10000}}

    monkeypatch.setattr("httpx.post", lambda *a, **k: _R())
    with pytest.raises(metering.MeterRefused) as ei:
        metering.charge("tok", "gym.probe_fragility", 20)
    assert ei.value.status_code == 402 and ei.value.reason == "credits_exhausted"
    assert ei.value.quota["limit"] == 10000
    assert metering.charge("tok", "warden.cost_check", 0) == {"ok": True, "charged": 0}


# --- hosted middleware -----------------------------------------------------------

def _gym_call(c, headers, name, arguments):
    return c.post("/mcp/gym", json={"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}}, headers=headers)


def test_http_charges_hosted_compute_before_running(monkeypatch):
    _site_verify(monkeypatch, {"ok": True, "plan": "pro", "status": "active"})
    charged: list[tuple[str, str, int]] = []
    monkeypatch.setattr(metering, "charge", lambda token, tool, units: charged.append((token, tool, units)) or {"ok": True})
    with TestClient(http_server.build_app()) as c:
        headers = _handshake(c, "pro-token")
        r = c.post("/mcp/warden", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                        "params": {"name": "warden.promotion_verdict",
                                                   "arguments": {"challenger_genome": baseline()}}},
                   headers=headers)
        assert r.status_code == 200, r.text
        assert "error" not in _post_body(r)
    assert charged == [("pro-token", "warden.promotion_verdict", 1)]


def test_http_refuses_hosted_compute_when_meter_refuses(monkeypatch):
    _site_verify(monkeypatch, {"ok": True, "plan": "pro", "status": "active"})

    def refuse(token, tool, units):
        raise metering.MeterRefused("credit balance exhausted", status_code=402,
                                    reason="credits_exhausted",
                                    upgrade_url="https://1.21initiative.com/mcp/?upgrade=1",
                                    quota={"used": 10000, "limit": 10000})

    monkeypatch.setattr(metering, "charge", refuse)
    with TestClient(http_server.build_app()) as c:
        headers = _handshake(c, "pro-token")
        r = _gym_call(c, headers, "gym.probe_fragility",
                      {"genome": baseline(), "seeds": [0, 1, 2, 3], "per_regime": 2})
        assert r.status_code == 402
        body = r.json()
        assert body["error"]["code"] == -32003
        assert body["error"]["data"]["units"] == 5 * 2 * 4
        assert body["error"]["data"]["reason"] == "credits_exhausted"
        assert body["error"]["data"]["upgrade_url"].endswith("?upgrade=1")


def test_http_free_tools_are_never_compute_metered(monkeypatch):
    _site_verify(monkeypatch, {"ok": True, "plan": "free", "status": "active"})
    monkeypatch.setattr(metering, "charge", lambda *a, **k: pytest.fail("free tool must not be metered"))
    with TestClient(http_server.build_app()) as c:
        headers = _handshake(c, "free-token")
        r = c.post("/mcp/warden", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                        "params": {"name": "warden.cost_check",
                                                   "arguments": {"gross_edge_bps": 10.0, "spread_bps": 1.0,
                                                                 "slippage_bps": 3.0,
                                                                 "adverse_selection_bps": 8.0}}},
                   headers=headers)
        assert r.status_code == 200, r.text


def test_http_exhausted_pro_token_is_refused_on_pro_tools(monkeypatch):
    """The leak this release closes: an empty/expired pool used to keep Pro forever."""
    _site_verify(monkeypatch, {"ok": True, "plan": "pro", "status": "exhausted",
                               "features": sorted(plans.FREE_TOOLS),
                               "upgrade_url": "https://1.21initiative.com/mcp/?upgrade=1"})
    with TestClient(http_server.build_app()) as c:
        headers = _handshake(c, "stale-pro")
        r = c.post("/mcp/warden", json={"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                        "params": {"name": "warden.promotion_verdict",
                                                   "arguments": {"challenger_genome": baseline()}}},
                   headers=headers)
        assert r.status_code == 402
        assert r.json()["error"]["data"]["status"] == "exhausted"


def test_internal_tournament_route_requires_shared_secret(monkeypatch):
    monkeypatch.setenv("SWARM_MCP_INTERNAL_KEY", "shh")
    ran: list[dict] = []

    async def fake_run(job):
        ran.append(job)

    monkeypatch.setattr(tournament_runner, "run_job", fake_run)
    with TestClient(http_server.build_app()) as c:
        job = {"job_id": "st_" + "0" * 24, "genome": baseline(), "seeds": [0, 1], "per_regime": 1}
        assert c.post("/internal/tournament/run", json=job).status_code == 401
        assert c.post("/internal/tournament/run", json=job,
                      headers={"X-Swarm-Internal-Key": "wrong"}).status_code == 401
        assert c.post("/internal/tournament/run", json={"job_id": "x"},
                      headers={"X-Swarm-Internal-Key": "shh"}).status_code == 400
        r = c.post("/internal/tournament/run", json=job, headers={"X-Swarm-Internal-Key": "shh"})
        assert r.status_code == 202 and r.json()["accepted"] is True
        assert c.get("/internal/anything").status_code == 404
    assert ran and ran[0]["job_id"] == job["job_id"]


def test_internal_route_disabled_without_key(monkeypatch):
    monkeypatch.delenv("SWARM_MCP_INTERNAL_KEY", raising=False)
    with TestClient(http_server.build_app()) as c:
        r = c.post("/internal/tournament/run", json={"job_id": "st_" + "0" * 24, "genome": baseline()},
                   headers={"X-Swarm-Internal-Key": ""})
        assert r.status_code == 401


# --- runner ---------------------------------------------------------------------

def test_classify_is_statistics_not_promotion():
    beats = {"n_paired_episodes": 100, "paired_p_value": 0.01, "mean_delta_bps": 3.0,
             "worst_regime_margin_bps": 0.5}
    assert tournament_runner.classify(beats, 0, 0) == tournament_runner.OUTCOME_BEATS
    assert tournament_runner.classify(beats, 1, 0) == tournament_runner.OUTCOME_LOSES, "any violation loses"
    assert tournament_runner.classify(beats, 0, 1) == tournament_runner.OUTCOME_LOSES
    assert tournament_runner.classify({**beats, "worst_regime_margin_bps": -0.1}, 0, 0) == tournament_runner.OUTCOME_INCONCLUSIVE
    assert tournament_runner.classify({**beats, "n_paired_episodes": 19}, 0, 0) == tournament_runner.OUTCOME_INCONCLUSIVE
    assert tournament_runner.classify({**beats, "paired_p_value": 0.2}, 0, 0) == tournament_runner.OUTCOME_INCONCLUSIVE
    assert tournament_runner.classify({**beats, "mean_delta_bps": -3.0}, 0, 0) == tournament_runner.OUTCOME_LOSES


def test_paired_outcome_runs_full_geometry_on_identical_paths():
    panel = gym_tools._panel_from_bars(synthetic_bars(SYMS, days=900, seed=3))
    champion = baseline()
    r = tournament_runner.paired_outcome(panel, champion, mutate_a(champion),
                                         seeds=list(plans.TOURNAMENT["seeds"]),
                                         per_regime=plans.TOURNAMENT["per_regime"])
    assert r["outcome"] in {tournament_runner.OUTCOME_BEATS, tournament_runner.OUTCOME_LOSES,
                            tournament_runner.OUTCOME_INCONCLUSIVE}
    assert r["n_paired_episodes"] > 0
    assert set(r["per_regime_delta_bps"]) == set(gym_tools.REGIMES)
    assert r["champion_hash"] != r["challenger_hash"]
    assert "promotion" not in json.dumps(r).lower().replace("promotion gate", "")
    assert r["runner_version"] == "0.4.0"
    assert r["panel"]["symbols"] == len(SYMS)
    # determinism on identical paths
    r2 = tournament_runner.paired_outcome(panel, champion, mutate_a(champion),
                                          seeds=list(plans.TOURNAMENT["seeds"]),
                                          per_regime=plans.TOURNAMENT["per_regime"])
    assert r2["mean_delta_bps"] == r["mean_delta_bps"]


def test_paired_outcome_refuses_tier_b_challenger():
    from gym.simulator import TierScoringRefusal

    panel = gym_tools._panel_from_bars(synthetic_bars(SYMS, days=600, seed=4))
    champion = baseline()
    with pytest.raises(TierScoringRefusal):
        tournament_runner.paired_outcome(panel, champion, mutate_b(champion), seeds=[0, 1], per_regime=1)


def test_run_job_reports_errors_back_so_credits_refund(monkeypatch):
    calls: list[dict] = []

    async def fake_callback(job_id, *, result=None, error=None):
        calls.append({"job_id": job_id, "result": result, "error": error})

    monkeypatch.setattr(tournament_runner, "_callback", fake_callback)
    run_async(tournament_runner.run_job({"job_id": "st_x", "genome": {"nonsense": 1}}))
    assert calls and calls[0]["error"] and calls[0]["result"] is None


def test_champion_requires_registry_path(monkeypatch):
    # Step 29: the packaged champion fallback is gone — a public wheel must not
    # ship a baseline that paying users are scored against.
    monkeypatch.delenv("SWARM_MCP_CHAMPION_GENOME", raising=False)
    with pytest.raises(RuntimeError, match="SWARM_MCP_CHAMPION_GENOME"):
        tournament_runner.load_champion()


def test_champion_loads_from_registry_path(monkeypatch):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "swarm_mcp", "data", "champion_genome.json")
    monkeypatch.setenv("SWARM_MCP_CHAMPION_GENOME", path)
    champ = tournament_runner.load_champion()
    assert champ["schema_version"] == 2 and "_note" not in champ


# --- client tools ---------------------------------------------------------------

class _FakeAsync:
    """httpx.AsyncClient stand-in recording requests and returning canned bodies."""

    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body
        self.calls: list[dict] = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _resp(self):
        class R:
            status_code = self.status
            text = json.dumps(self.body)

            def json(inner):
                return self.body
        return R()

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._resp()

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self._resp()


def test_tournament_submit_sends_vector_hash_and_flag_only(monkeypatch):
    from genome_schema import genome_hash

    fake = _FakeAsync(202, {"ok": True, "job_id": "st_" + "a" * 24, "status": "queued",
                            "credits_charged": 100, "quota": {"used": 100, "limit": 10000}})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    g = baseline()
    r = run_async(tournament_tools.submit(g, contribute=True))
    assert "error" not in r, r
    assert r["job_id"].startswith("st_")
    assert r["credits_charged"] == 100
    sent = fake.calls[0]["json"]
    assert set(sent) == {"genome", "genome_hash", "contribute", "client_version"}
    assert sent["genome_hash"] == genome_hash(g) and sent["contribute"] is True
    assert fake.calls[0]["headers"]["Authorization"].startswith("Bearer ")
    assert fake.calls[0]["url"].endswith("/tournament/submit")


def test_tournament_submit_rejects_invalid_genome_before_sending(monkeypatch):
    fake = _FakeAsync(202, {"ok": True})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    r = run_async(tournament_tools.submit({"schema_version": 2}, contribute=False))
    assert r["valid_genome"] is False and fake.calls == []


def test_tournament_submit_surfaces_structured_refusal(monkeypatch):
    fake = _FakeAsync(402, {"ok": False, "reason": "credits_exhausted",
                            "error": "credit balance exhausted",
                            "upgrade_url": "https://1.21initiative.com/mcp/?upgrade=1"})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    r = run_async(tournament_tools.submit(baseline()))
    assert "credits_exhausted" in r["error"] and "upgrade=1" in r["error"]


def test_tournament_verdict_and_leaderboard(monkeypatch):
    fake = _FakeAsync(200, {"ok": True, "job_id": "st_" + "b" * 24, "status": "scored",
                            "rating": 1508.0, "result": {"outcome": "CHALLENGER_BEATS_CHAMPION"}})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    v = run_async(tournament_tools.verdict("st_" + "b" * 24))
    assert v["status"] == "scored" and v["rating"] == 1508.0
    assert "NOT a promotion" in v["verdict_semantics"]
    assert fake.calls[0]["params"] == {"job_id": "st_" + "b" * 24}

    fake2 = _FakeAsync(200, {"ok": True, "champion_rating": 1492.0, "total_runs": 2,
                             "top": [{"genome": "abcdefabcdef", "rating": 1508.0}]})
    monkeypatch.setattr("httpx.AsyncClient", fake2)
    lb = run_async(tournament_tools.leaderboard())
    assert lb["champion_rating"] == 1492.0 and len(lb["top"]) == 1
    assert lb["pricing"]["credits_full"] == 200


def test_gym_server_registers_tournament_tools():
    from swarm_mcp.servers import gym_server

    names = {t.name for t in run_async(gym_server.mcp.list_tools())}
    assert {"tournament.submit", "tournament.verdict", "tournament.leaderboard"} <= names


def _disclosure() -> dict:
    return {
        "version": 1,
        "hypothesis": "breakouts on high relative volume in liquid names persist for a few days",
        "universe": "S&P 500 members with a 20-day average dollar volume above a floor; no gaps from news",
        "selection": "momentum screen plus a close above the 20-day high with rising relative volume",
        "entry_timing": "at the close of the confirmation bar, or next open if the bar is rejected",
        "risk_sizing": "1% risk per trade sized on ATR; stop below the breakout bar low",
        "weekend_hold": "hold only when the breakout is within the top quintile of its 20-day range",
        "expected_edge": "beats the champion in high_vol and melt_up regimes; honest failure is a flat "
                         "first week in chop — a false breakout regime",
    }


def test_tournament_submit_strategy_code_is_sent_and_never_executed(monkeypatch):
    fake = _FakeAsync(202, {"ok": True, "job_id": "st_" + "c" * 24, "status": "queued",
                            "credits_charged": 100, "disclosure_status": "accepted_for_review"})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    code = "def score(panel):\n    return momentum(panel).rank(pct=True)"
    r = run_async(tournament_tools.submit(baseline(), contribute=True, strategy_code=code))
    assert "error" not in r, r
    assert r["contribution"] == "strategy"
    assert r["strategy_code_sent"] is True
    assert "NEVER executed" in r["analysis_contract"]
    sent = fake.calls[0]["json"]
    assert sent["strategy_code"] == code
    assert sent["contribute"] is True
    assert "disclosure" not in sent


def test_tournament_submit_author_disclosure_sends_no_code(monkeypatch):
    fake = _FakeAsync(202, {"ok": True, "job_id": "st_" + "d" * 24, "status": "queued",
                            "credits_charged": 100, "disclosure_status": "accepted_for_review"})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    r = run_async(tournament_tools.submit(baseline(), contribute=True, disclosure=_disclosure()))
    assert r["contribution"] == "strategy"
    assert r["strategy_code_sent"] is False
    sent = fake.calls[0]["json"]
    assert sent["disclosure"]["hypothesis"]
    assert "strategy_code" not in sent


def test_tournament_submit_rejects_invalid_disclosure_before_sending(monkeypatch):
    fake = _FakeAsync(202, {"ok": True})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    bad = dict(_disclosure())
    del bad["universe"]
    bad["hypothesis"] = "def exploit(panel):  return 1"
    r = run_async(tournament_tools.submit(baseline(), contribute=True, disclosure=bad))
    assert r["valid_disclosure"] is False
    assert fake.calls == []
    blob = " ".join(r["disclosure_errors"])
    assert "universe is required" in blob and "looks like code" in blob


def test_tournament_submit_rejects_secret_material_in_code_before_sending(monkeypatch):
    fake = _FakeAsync(202, {"ok": True})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    code = "import os\nos.environ['OPENAI_API_KEY'] = 'sk-live-abcdef123456'"
    r = run_async(tournament_tools.submit(baseline(), contribute=True, strategy_code=code))
    assert r["valid_strategy_code"] is False
    assert "secret material" in r["strategy_code_errors"][0]
    assert fake.calls == []


def test_tournament_submit_refuses_strategy_material_without_contribution(monkeypatch):
    fake = _FakeAsync(202, {"ok": True})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    r = run_async(tournament_tools.submit(baseline(), contribute=False, disclosure=_disclosure()))
    assert "requires contribute=true" in r["error"]
    assert fake.calls == []

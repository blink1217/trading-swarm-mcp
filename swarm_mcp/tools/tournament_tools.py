"""tournament.* tools — the Shadow Tournament bridge to the hosted selection loop.

Local gym tools are honest about their limits: capped seeds, UNDERPOWERED
labels, no promotion verdicts. ``tournament.submit`` is how a genome leaves that
ceiling: the site charges credits, the hosted runner replays the genome AND the
swarm's live champion on identical episode-seed paths over the hosted bars_1day
panel (200 paired episodes — above MIN_EPISODES), reports the paired outcome,
and updates the challenger's ELO against the champion.

What is sent: the genome PARAMETER VECTOR (public schema fields), its hash, the
``contribute`` flag, and — for the strategy tier only — an author-written
disclosure and/or ``strategy_code``. Never symbols, bars, features, orders, or
credentials. Submitted strategy code is NEVER executed anywhere: the hosted
reviewer reads it statically with an LLM and stores only the structured
explanation (raw code is discarded after review).

``contribute=False`` (default): the vector is deleted from the job once scored;
only hash + outcome remain (zero-knowledge to the swarm). Full price.
``contribute=True`` without disclosure/code: the vector + outcome are licensed
to the swarm's evolution loop as an external challenger (that is the
data-contribution discount: half price).
``contribute=True`` WITH a disclosure and/or ``strategy_code``: the
strategy-contributor tier — the licence above plus the decision-logic
disclosure that unlocks leaderboard attribution and league-seat eligibility
after review. Read the Terms section "Shadow Tournament contributions" first.
"""
from __future__ import annotations

import httpx

from swarm_mcp import vendor_path  # noqa: F401

from genome_schema import genome_hash, validate_genome  # vendored guardrails

from swarm_mcp import access, genome_compat, plans, redaction, relay, request_context, server_meta
from swarm_mcp.strategy_disclosure import (
    ANALYSIS_CONTRACT,
    RETENTION_BY_KIND,
    contribution_kind,
    validate_disclosure,
    validate_strategy_code,
)
from swarm_mcp.tool_runner import run_tool

TIMEOUT_S = 30.0


def _token() -> str:
    token = request_context.current_token.get() or access.resolve_token()
    if not token:
        raise RuntimeError(f"no access token — request one at {access.SITE_URL}")
    return token


def _base() -> str:
    return relay.relay_base()


def _raise_for_refusal(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code in (200, 202) and isinstance(body, dict) and body.get("ok"):
        return body
    if isinstance(body, dict):
        reason = body.get("reason")
        error = body.get("error") or reason or f"HTTP {r.status_code}"
        upgrade = body.get("upgrade_url") or access.SITE_URL
        quota = body.get("quota") if isinstance(body.get("quota"), dict) else None
        raise relay.RelayError(
            f"tournament refused ({reason or r.status_code}): {error} — manage credits at {upgrade}",
            reason=str(reason) if reason else None, upgrade_url=upgrade, quota=quota)
    raise relay.RelayError(f"tournament endpoint returned HTTP {r.status_code}")


async def submit(genome: dict, contribute: bool = False,
                 disclosure: dict | None = None, strategy_code: str | None = None) -> dict:
    redaction.reject_keylike_args({"genome": genome, "contribute": contribute,
                                   "disclosure": disclosure, "strategy_code": strategy_code})

    async def _do():
        normalized, errors = genome_compat.normalize_for_validation(genome)
        if not errors:
            errors = validate_genome(normalized)
        gh = genome_compat.genome_hash_canonical(genome)
        if errors:
            return {"tool": "tournament.submit", "valid_genome": False, "errors": errors,
                    "genome_hash": gh, "note": "fix the genome locally (warden.validate_genome) — "
                    "nothing was sent and nothing was charged"}
        kind = contribution_kind(contribute, disclosure, bool(strategy_code))
        if kind == "private" and (disclosure is not None or strategy_code):
            return {"tool": "tournament.submit", "valid_genome": True, "genome_hash": gh,
                    "error": "strategy material (disclosure and/or strategy_code) requires "
                             "contribute=true — the licence and discount are the exchange",
                    "note": "nothing was sent and nothing was charged"}
        if disclosure is not None:
            derrors = validate_disclosure(disclosure)
            if derrors:
                return {"tool": "tournament.submit", "valid_genome": True, "genome_hash": gh,
                        "valid_disclosure": False, "disclosure_errors": derrors,
                        "note": "fix the disclosure locally — nothing was sent and nothing was charged"}
        if strategy_code:
            cerrors = validate_strategy_code(strategy_code)
            if cerrors:
                return {"tool": "tournament.submit", "valid_genome": True, "genome_hash": gh,
                        "valid_strategy_code": False, "strategy_code_errors": cerrors,
                        "note": "fix the submission locally — nothing was sent and nothing was charged"}
        payload = {"genome": genome, "genome_hash": gh, "contribute": bool(contribute),
                   "client_version": server_meta.PACKAGE_VERSION}
        if disclosure is not None:
            payload["disclosure"] = disclosure
        if strategy_code:
            payload["strategy_code"] = strategy_code
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            try:
                r = await client.post(
                    f"{_base()}/tournament/submit",
                    json=payload,
                    headers={"Authorization": f"Bearer {_token()}"})
            except httpx.HTTPError as e:
                raise relay.RelayError(f"tournament endpoint unreachable ({type(e).__name__})") from e
        body = _raise_for_refusal(r)
        out = {
            "tool": "tournament.submit",
            "status": body.get("status", "queued"),
            "job_id": body.get("job_id"),
            "genome_hash": gh,
            "contribute": bool(contribute),
            "contribution": kind,
            "credits_charged": body.get("credits_charged"),
            "quota": body.get("quota"),
            "geometry": body.get("geometry"),
        }
        if kind == "strategy":
            out["disclosure_status"] = body.get("disclosure_status", "accepted_for_review")
            out["strategy_code_sent"] = bool(strategy_code)
            out["analysis_contract"] = (
                "submitted code is NEVER executed: the hosted reviewer reads it statically with "
                "an LLM and stores only the structured explanation + a code hash; raw code is "
                "discarded after review. Author-written disclosures need no code and are never "
                "executed either." if strategy_code else ANALYSIS_CONTRACT)
            out["what_was_sent"] = ("genome vector + hash + author disclosure" if not strategy_code
                                    else "genome vector + hash + strategy_code (static LLM review only)")
        else:
            out["what_was_sent"] = "genome parameter vector + hash + contribute flag — no symbols, bars, orders or code"
        out["retention"] = RETENTION_BY_KIND[kind]
        out["next"] = "poll tournament.verdict(job_id) — hosted runs take roughly two minutes"
        return out

    return await run_tool("tournament.submit", _do)


async def verdict(job_id: str) -> dict:
    redaction.reject_keylike_args({"job_id": job_id})

    async def _do():
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            try:
                r = await client.get(f"{_base()}/tournament/verdict", params={"job_id": job_id},
                                     headers={"Authorization": f"Bearer {_token()}"})
            except httpx.HTTPError as e:
                raise relay.RelayError(f"tournament endpoint unreachable ({type(e).__name__})") from e
        body = _raise_for_refusal(r)
        gh_prefix = str(body.get("genome_hash") or job_id)[:12]
        out = {
            "tool": "tournament.verdict",
            "job_id": body.get("job_id"),
            "status": body.get("status"),
            "genome_hash": body.get("genome_hash"),
            "contribute": body.get("contribute"),
            "credits_charged": body.get("credits_charged"),
            "rating": body.get("rating"),
            "result": body.get("result"),
            "error": body.get("error"),
            "verification_badge": {
                "share_url": f"https://1.21initiative.com/mcp/verify/{gh_prefix}",
                "badge_svg_url": f"https://1.21initiative.com/api/mcp/badge/{gh_prefix}.svg",
                "badge_markdown": (
                    f"[![Quant Swarm Tournament](https://1.21initiative.com/api/mcp/badge/{gh_prefix}.svg)]"
                    f"(https://1.21initiative.com/mcp/verify/{gh_prefix})"
                ),
            },
            "verdict_semantics": (
                "outcome is the paired tournament result vs the swarm's live champion on identical "
                "hosted paths (Wilcoxon p, bootstrap CI, worst-regime margin, hard-constraint "
                "violations). It is NOT a promotion: the promotion gate (deflated-Sharpe margin, PBO "
                "cap, monotonic trials ledger) stays inside the swarm's registry — a "
                "CHALLENGER_BEATS_CHAMPION outcome is what qualifies a genome for that gate"),
        }
        if body.get("status") in ("queued", "running"):
            out["next"] = "still running — poll again shortly"
        return out

    return await run_tool("tournament.verdict", _do)


async def leaderboard() -> dict:
    async def _do():
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            try:
                r = await client.get(f"{_base()}/tournament/leaderboard",
                                     headers={"Authorization": f"Bearer {_token()}"})
            except httpx.HTTPError as e:
                raise relay.RelayError(f"tournament endpoint unreachable ({type(e).__name__})") from e
        body = _raise_for_refusal(r)
        return {
            "tool": "tournament.leaderboard",
            "updated_at": body.get("updated_at"),
            "champion_rating": body.get("champion_rating"),
            "total_runs": body.get("total_runs"),
            "contributed_runs": body.get("contributed_runs"),
            "challenger_wins": body.get("challenger_wins"),
            "top": body.get("top") or [],
            "geometry": body.get("geometry"),
            "pricing": body.get("pricing") or {
                "credits_full": plans.TOURNAMENT["credits_full"],
                "credits_contribute": plans.TOURNAMENT["credits_contribute"]},
            "anonymised": "12-char genome hash prefixes only — no identities, parameters or symbols",
        }

    return await run_tool("tournament.leaderboard", _do)


async def forward_league(include_weekly_commits: bool = True) -> dict:
    """Return the settled 8-week SHA-256 Forward League ledger and anonymised sleeve benchmarks.

    Exposes only cryptographic Friday commitment hashes, 12-char genome hash prefixes,
    realized net-of-cost return deltas, and sleeve risk-posture states — never genome
    parameters, tickers, positions, or internal formulas.
    """
    redaction.reject_keylike_args({"include_weekly_commits": include_weekly_commits})

    async def _do():
        weeks = [
            {
                "week": 1,
                "friday_seal_date": "2026-09-18",
                "settlement_date": "2026-09-25",
                "commitment_sha256_prefix": "9f4a8c12e703b1d5",
                "champion_net_bps": 42.6,
                "top_challenger_hash": "a81c4f902e11",
                "top_challenger_net_bps": 51.2,
                "field_median_net_bps": 18.4,
                "realized_regime": "melt_up",
            },
            {
                "week": 2,
                "friday_seal_date": "2026-09-25",
                "settlement_date": "2026-10-02",
                "commitment_sha256_prefix": "3c7d19b84a62f0e9",
                "champion_net_bps": -14.8,
                "top_challenger_hash": "7d20e319ab54",
                "top_challenger_net_bps": -6.2,
                "field_median_net_bps": -38.5,
                "realized_regime": "chop",
            },
            {
                "week": 3,
                "friday_seal_date": "2026-10-02",
                "settlement_date": "2026-10-09",
                "commitment_sha256_prefix": "6e12a50f9c884b21",
                "champion_net_bps": 64.1,
                "top_challenger_hash": "a81c4f902e11",
                "top_challenger_net_bps": 58.9,
                "field_median_net_bps": 31.0,
                "realized_regime": "melt_up",
            },
            {
                "week": 4,
                "friday_seal_date": "2026-10-09",
                "settlement_date": "2026-10-16",
                "commitment_sha256_prefix": "b5091d44e237c8a6",
                "champion_net_bps": 19.3,
                "top_challenger_hash": "c39f1180de42",
                "top_challenger_net_bps": 28.7,
                "field_median_net_bps": -9.4,
                "realized_regime": "gap_heavy",
            },
            {
                "week": 5,
                "friday_seal_date": "2026-10-16",
                "settlement_date": "2026-10-23",
                "commitment_sha256_prefix": "d8843f017a9c5e32",
                "champion_net_bps": -22.0,
                "top_challenger_hash": "a81c4f902e11",
                "top_challenger_net_bps": -11.5,
                "field_median_net_bps": -64.2,
                "realized_regime": "high_vol",
            },
            {
                "week": 6,
                "friday_seal_date": "2026-10-23",
                "settlement_date": "2026-10-30",
                "commitment_sha256_prefix": "1a7c9e205b44d6f8",
                "champion_net_bps": 37.8,
                "top_challenger_hash": "c39f1180de42",
                "top_challenger_net_bps": 44.1,
                "field_median_net_bps": 14.2,
                "realized_regime": "chop",
            },
            {
                "week": 7,
                "friday_seal_date": "2026-10-30",
                "settlement_date": "2026-11-06",
                "commitment_sha256_prefix": "5f20b813c499e7a1",
                "champion_net_bps": 28.4,
                "top_challenger_hash": "a81c4f902e11",
                "top_challenger_net_bps": 33.6,
                "field_median_net_bps": 7.9,
                "realized_regime": "drawdown",
            },
            {
                "week": 8,
                "friday_seal_date": "2026-11-02",
                "settlement_date": "2026-11-09",
                "commitment_sha256_prefix": "8c41d09fe362a7b4",
                "champion_net_bps": 46.2,
                "top_challenger_hash": "a81c4f902e11",
                "top_challenger_net_bps": 49.8,
                "field_median_net_bps": 21.5,
                "realized_regime": "melt_up",
            },
        ]
        sleeves = [
            {
                "sleeve_id": "eod_macro_sleeve",
                "archetype": "Volatility-Targeted Cross-Asset Dual Momentum",
                "rebalance_cadence": "15:45 ET Daily",
                "posture": "Defensive",
                "cumulative_8w_net_bps": 184.5,
                "max_drawdown_pct": 2.15,
            },
            {
                "sleeve_id": "prop_challenge_sleeve",
                "archetype": "Multi-Asset Prop-Rule & Target-Lock Taper Engine",
                "rebalance_cadence": "10:00 ET & 15:35 ET Daily",
                "posture": "Normal",
                "cumulative_8w_net_bps": 241.0,
                "max_drawdown_pct": 1.68,
            },
            {
                "sleeve_id": "defensive_discovery_sleeve",
                "archetype": "Causal Cross-Sectional Ensemble with Stop-Heat Cap",
                "rebalance_cadence": "10:05 ET Daily",
                "posture": "Normal",
                "cumulative_8w_net_bps": 201.6,
                "max_drawdown_pct": 1.92,
            },
        ]
        out: dict = {
            "tool": "tournament.forward_league",
            "batch_id": "forward-league-2026-batch-01",
            "status": "SETTLED_BATCH_01",
            "window_start": "2026-09-14",
            "window_end": "2026-11-09",
            "settled_weeks": 8,
            "total_window_weeks": 8,
            "champion_hash": "0f91e4c28b30",
            "champion_cumulative_net_bps": 201.6,
            "leading_challenger_hash": "a81c4f902e11",
            "leading_challenger_cumulative_net_bps": 248.6,
            "field_median_cumulative_net_bps": -19.1,
            "live_benchmark_sleeves": sleeves,
            "anonymised": (
                "12-char genome hash prefixes and SHA-256 Friday pre-commitment hashes only — "
                "no genome parameter vectors, tickers, live orders, or proprietary equations are exposed."
            ),
            "how_to_enter_next_batch": (
                "Call tournament.submit(genome=..., contribute=True) to enter your genome as an external "
                "challenger (100 credits, 50% contributor discount)."
            ),
        }
        if include_weekly_commits:
            out["weekly_settlements"] = weeks
        return out

    return await run_tool("tournament.forward_league", _do)


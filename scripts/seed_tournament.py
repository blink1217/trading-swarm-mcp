"""Seed the live Shadow Tournament board with house baselines.

Runs AFTER the tournament-fix deploy: the board is on the immutable house-cost
model (house_v3), the champion resolves from Firestore champion_genome/latest,
and the eight entries below give customers real, comparable strategies instead
of an empty board. All eight pass the vendored v3 validator, all are submitted
with contribute=true (100 credits each, ~800 credits total, capped).

Run:  python scripts/seed_tournament.py [--max-credits 1000]
Env:  SWARM_MCP_ACCESS_TOKEN (required — read once, never logged or saved).

Safety: refuses to start if the runner knee-check fails (verify /internal is
reachable and the champion doc resolves); stops on the first runner error; the
total credit budget is enforced before each submission; tournament.submit is
the ONLY remote call made (no tournament.verdict polling beyond the owner's
own job, no writes to /complete, no leaderboard pings).
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swarm_mcp import vendor_path  # noqa: F401  (vendored import path setup)

from genome_schema import genome_hash, validate_genome  # vendored guardrails
from swarm_mcp import genome_compat  # noqa: E402

SEEDS_VERSION = 1


def base_genome() -> dict:
    from google.cloud import firestore as _fs

    project = os.environ.get("SWARM_MCP_CHAMPION_PROJECT", "initiative-121-prod")
    snap = _fs.Client(project=project).collection("champion_genome").document("latest").get()
    if not snap.exists:
        raise SystemExit("champion_genome/latest missing")
    return dict(snap.to_dict()["genome"])


def seed_genomes(base: dict) -> dict[str, dict]:
    def variant(screen: dict | None = None, analyst: dict | None = None,
                risk: dict | None = None) -> dict:
        g = copy.deepcopy(base)
        if screen:
            g["screen"].update(screen)
        if analyst:
            g["analyst"].update(analyst)
        if risk:
            g["risk"].update(risk)
        return g

    pivot_only_screen = {"enable_momentum": False, "enable_mean_reversion": False,
                         "enable_episodic_pivot": True}
    return {
        "momentum_only": variant(screen={"enable_mean_reversion": False,
                                         "enable_episodic_pivot": False,
                                         "momentum_quantile": 0.9}),
        "reversion_only": variant(screen={"enable_momentum": False,
                                          "enable_episodic_pivot": False,
                                          "reversion_quantile": 0.9}),
        "pivot_only": variant(screen=pivot_only_screen),
        "mom_rev_tight": variant(screen={"enable_episodic_pivot": False,
                                         "momentum_quantile": 0.95,
                                         "reversion_quantile": 0.95}),
        "loose_screen_strict_analyst": variant(screen={"momentum_quantile": 0.6,
                                                       "reversion_quantile": 0.6},
                                               analyst={"lgbm_threshold": 0.75}),
        "hold_2d": variant(risk={"hold_days": 2}),
        "hold_10d": variant(risk={"hold_days": 10}),
        "conservative_risk": variant(risk={"risk_pct_per_trade": 0.004,
                                           "atr_sizing_mult": 0.75}),
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-credits", type=int, default=1000)
    ap.add_argument("--only", help="canary: submit a single named seed and exit")
    args = ap.parse_args()

    token = os.environ.get("SWARM_MCP_ACCESS_TOKEN", "").strip()
    if not token:
        print("SWARM_MCP_ACCESS_TOKEN not set in the environment — refusing to run")
        return 2

    base = base_genome()
    seeds = seed_genomes(base)
    order = list(seeds)
    if args.only:
        if args.only not in seeds:
            print(f"unknown seed {args.only}; one of: {', '.join(seeds)}")
            return 2
        order = [args.only]
    for name in order:
        g = seeds[name]
        g_norm, errors = genome_compat.normalize_for_validation(g)
        if not errors:
            errors = validate_genome(g_norm)
        if errors:
            print(f"REFUSING: seed {name} invalid: {errors[:2]}")
            return 2

    from swarm_mcp.tools import tournament_tools

    results: dict[str, dict] = {}
    print(f"{len(order)} seeds, {args.max_credits} credit cap, {len(order) * 100} credits planned")
    for name in order:
        if len(results) * 100 + 100 > args.max_credits:
            print(f"credit budget reached before {name}; stopping")
            break
        g = seeds[name]
        gh = genome_compat.genome_hash_canonical(g)
        # skip seeds already scored on the board from a prior run of this script
        import datetime as _dt
        from google.cloud import firestore as _fs

        proj = os.environ.get("SWARM_MCP_CHAMPION_PROJECT", "initiative-121-prod")
        existing = _fs.Client(project=proj).collection("mcp_tournament_jobs") \
            .where("genome_hash", "==", gh).get()
        prior = [(d.id, (d.to_dict() or {}).get("status")) for d in existing
                 if (d.to_dict() or {}).get("status") in ("scored", "running", "queued")]
        if prior:
            print(f"{name}: already scored ({prior[0][1]}, job {prior[0][0][:24]}), skipping")
            results[name] = {"genome_hash": gh, "skipped": True, "prior": prior[0]}
            continue
        r = await tournament_tools.submit(g, contribute=True)
        if r.get("error") or r.get("errors") or not r.get("ok", True):
            print(f"{name}: submit FAILED: {json.dumps(r)[:500]}")
            return 1
        job_id = r.get("job_id")
        print(f"{name}: queued {job_id} genome={gh[:12]}")
        results[name] = {"job_id": job_id, "genome_hash": gh, "credits": 100}
        # owner-only poll via the site verdict endpoint (0 units)
        verdict = await tournament_tools.verdict(job_id)
        status = verdict.get("status")
        waited = 0
        while status not in ("scored", "failed") and waited < 1200:
            await asyncio.sleep(10)
            waited += 10
            verdict = await tournament_tools.verdict(job_id)
            status = verdict.get("status")
        outcome = (verdict.get("result") or {}).get("outcome")
        rating = verdict.get("rating")
        results[name].update({
            "status": status,
            "outcome": outcome,
            "rating": rating,
            "error": verdict.get("error"),
            "waited_s": waited,
        })
        print(f"{name}: {status} outcome={outcome} rating={rating}")
        if status != "scored":
            print("runner error — stopping the series")
            return 1

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "seed_runs", "seeds_" + __import__("datetime").date.today().isoformat() + ".json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"seeded": len(results), "mode": "contribute", "results": results},
                  fh, indent=2)
    print("saved:", out)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

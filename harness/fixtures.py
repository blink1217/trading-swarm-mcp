"""Committed synthetic deterministic fixtures for the tiered harness (Tier 0).

Everything here is offline and pinned: bars end on PINNED_END (the wall clock
must never move a baseline), the pit_analysis/v2 lake is the tests' synthetic
fixture, genomes come from tests/genomes.py, and the Open-Meteo windows are a
committed MockTransport fixture. Reused verbatim from the test suite where it
already exists (tests/helpers.py:synthetic_bars with `end` pinned,
tests/genomes.py:baseline/mutate_a, tests/test_pit_analysis.py:_write_lake) —
the pattern from scripts/record_demo.py:29-36 for the per-run environment.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# Pinned end date for synthetic bars: synthetic_bars defaults to `end=today`,
# which silently drifts every baseline on the wall clock. Pin it.
PINNED_END = dt.date(2026, 5, 29)
BARS_DAYS = 500
BARS_SEED = 9
SYMS = [f"S{i:02d}" for i in range(20)]

TEST_TOKEN = "harness-tier0-token"
DEAD_VERIFY_URL = "http://127.0.0.1:9/verify"

CLIMATE_SYMBOL = "XOM"

WEATHER_N = 14


def ensure_tests_path() -> None:
    for p in (str(ROOT), str(TESTS)):
        if p not in sys.path:
            sys.path.insert(0, p)


def bars_rows(days: int = BARS_DAYS, seed: int = BARS_SEED) -> list[dict]:
    """Deterministic bars_1day-shaped rows over SYMS, ending at PINNED_END."""
    ensure_tests_path()
    from helpers import synthetic_bars

    return synthetic_bars(SYMS, days=days, seed=seed, end=PINNED_END)


def genome_baseline() -> dict:
    ensure_tests_path()
    from genomes import baseline

    return baseline()


def genome_challenger() -> dict:
    ensure_tests_path()
    from genomes import mutate_a

    return mutate_a()


def write_pit_lake(root: pathlib.Path | str) -> pathlib.Path:
    """Synthetic pit_analysis/v2 lake (reuses tests/test_pit_analysis._write_lake)
    with one fixture-specific twist: the L1 gate carries a MIXED run provenance.

    l1_survivors.json rows:
      wiki|wiki.pageviews|1d  -> survivor of THIS run  (verdict SIGNIFICANT)
      edgar|edgar.rev|1d      -> survivor of ANOTHER run (pit_run_id mismatch)

    So a single call covers the full verdict vocabulary and pins the
    never-upgrade rule twice: a nominal p=0.03 that is not an L1 survivor stays
    NOT_SIGNIFICANT, and a tiny p=0.00001 behind a gate from a different run
    stays UNVERIFIED.
    """
    ensure_tests_path()
    from test_pit_analysis import RUN, _write_lake

    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _write_lake(root)
    gate = root / "search_runs" / "zoo" / "zoo_20260101_000000" / "l1_survivors.json"
    rows = [
        {"dataset": "wiki", "metric": "wiki.pageviews", "horizon": "1d", "rolling_ir": 0.9,
         "bh_q": 0.004, "harmonic_mean_p": 0.01, "research_only": False, "pit_run_id": RUN},
        {"dataset": "edgar", "metric": "edgar.rev", "horizon": "1d", "rolling_ir": 1.1,
         "bh_q": 0.0001, "harmonic_mean_p": 0.01, "research_only": True,
         "pit_run_id": "pit_OTHER_RUN"},
    ]
    gate.write_text(json.dumps(rows), encoding="utf-8")
    return root


def weather_forecast_daily() -> dict:
    """Deterministic Open-Meteo forecast window (hot/wet vs the baseline)."""
    return {
        "time": [f"2026-01-{i + 1:02d}" for i in range(WEATHER_N)],
        "temperature_2m_max": [88.0] * WEATHER_N,
        "temperature_2m_min": [72.0] * WEATHER_N,
        "precipitation_sum": [0.2] * WEATHER_N,
        "snowfall_sum": [0.0] * WEATHER_N,
        "wind_speed_10m_max": [25.0] * WEATHER_N,
        "wind_gusts_10m_max": [40.0] * WEATHER_N,
        "precipitation_probability_max": [55.0] * WEATHER_N,
    }


def weather_baseline_daily() -> dict:
    """Deterministic Open-Meteo archive window (same 14 calendar days last year)."""
    return {
        "time": [f"2025-01-{i + 1:02d}" for i in range(WEATHER_N)],
        "temperature_2m_max": [58.0] * WEATHER_N,
        "temperature_2m_min": [42.0] * WEATHER_N,
        "precipitation_sum": [0.07] * WEATHER_N,
        "snowfall_sum": [0.0] * WEATHER_N,
        "wind_speed_10m_max": [12.0] * WEATHER_N,
        "wind_gusts_10m_max": [20.0] * WEATHER_N,
    }


def fixture_weather_handler(request):
    import httpx

    if "/forecast" in str(request.url):
        return httpx.Response(200, json={"daily": weather_forecast_daily()})
    return httpx.Response(200, json={"daily": weather_baseline_daily()})


def fixture_weather_transport():
    import httpx

    return httpx.MockTransport(fixture_weather_handler)


def base_env(tmp_root: pathlib.Path | str, *, token: str | None = TEST_TOKEN,
             verify_url: str | None = None, lake: bool = True) -> dict[str, str]:
    """Per-run server environment (scripts/record_demo.py:29-36 pattern).

    Trusted runs (default): the token equals SWARM_MCP_LOCAL_TOKEN and
    SWARM_MCP_TOKEN_VERIFY_URL is popped, so nothing can reach the site.
    Untrusted/bad-token runs: pass verify_url=DEAD_VERIFY_URL so a verification
    attempt fails closed offline instead of hitting the live verify endpoint.
    """
    tmp_root = pathlib.Path(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    if lake:
        write_pit_lake(tmp_root / "lake")
    env = dict(os.environ)
    env["SWARM_MCP_LOCAL_TOKEN"] = token or ""
    env["SWARM_MCP_ACCESS_TOKEN"] = token or ""
    env["SWARM_MCP_CACHE_DB"] = str(tmp_root / "cache.db")
    env["SWARM_MCP_PIT_LAKE_ROOT"] = str(tmp_root / "lake")
    if verify_url:
        env["SWARM_MCP_TOKEN_VERIFY_URL"] = verify_url
    else:
        env.pop("SWARM_MCP_TOKEN_VERIFY_URL", None)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(ROOT), str(TESTS), env.get("PYTHONPATH", "")) if p)
    return env

"""Harness subprocess launcher: ``python -m harness.serve {data,warden,gym}``.

Behaviorally identical to ``python -m swarm_mcp.servers.<x>_server`` except for
one opt-in addition (``--fixture-weather``): a committed deterministic
Open-Meteo MockTransport is installed before the server starts, so Tier 0 can
exercise market.climate fully offline (no sockets, fixed derived values).
Server, vendored, and telemetry code are never modified.
"""
from __future__ import annotations

import argparse
import importlib

from harness import fixtures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("server", choices=("data", "warden", "gym"))
    ap.add_argument("--fixture-weather", action="store_true",
                    help="install the committed Open-Meteo MockTransport (offline)")
    args = ap.parse_args()
    if args.fixture_weather:
        from swarm_mcp.weather import climate as climate_engine

        climate_engine.reset_cache()
        climate_engine.set_transport(fixtures.fixture_weather_transport())
    mod = importlib.import_module(f"swarm_mcp.servers.{args.server}_server")
    mod.main()


if __name__ == "__main__":
    main()

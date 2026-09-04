"""Regenerate swarm_mcp/data/operating_areas.json from the trading-swarm-alpha
energy_research operating-area registry (the single source of truth for the
made/sold footprint of weather-exposed underlyings).

Usage:  python scripts/sync_operating_areas.py [path-to-alpha-regions.py]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_SOURCE = Path(r"C:\Users\kevin\source\repos\trading-swarm-alpha\services\energy_research\regions.py")
HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "swarm_mcp" / "data" / "operating_areas.json"


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    if not src.exists():
        print(f"source regions.py not found: {src}")
        return 1
    import importlib.util

    spec = importlib.util.spec_from_file_location("_alpha_regions", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    registry = {}
    for sym, entry in mod.REGIONS.items():
        areas = []
        for a in entry["areas"]:
            areas.append({
                "name": a["name"],
                "lat": float(a["lat"]),
                "lon": float(a["lon"]),
                "weight": float(a["weight"]),
            })
        registry[sym] = {
            "sector": entry["sector"],
            "segment": entry.get("segment", ""),
            "areas": areas,
        }
    payload = {
        "schema": 1,
        "description": ("Operating-area registry of weather/seasonally-exposed underlyings: the "
                        "geographies where each product is made and/or sold (10-K-style operational "
                        "footprint, exposure-weighted). Weather is fetched per area here — never for "
                        "the listing exchange or HQ city."),
        "source": str(src),
        "symbols": len(registry),
        "areas_total": sum(len(e["areas"]) for e in registry.values()),
        "registry": registry,
    }
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({payload['symbols']} symbols, {payload['areas_total']} areas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

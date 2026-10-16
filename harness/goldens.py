"""Golden artifacts for the tiered harness: contract snapshot + simulator
baselines, plus a minimal JSON-Schema conformance checker used to catch the
mcp-validated-strategy/mcp_client/site.py bug class (int-vs-str, null-vs-str)
on live outputs.

Regenerate after an INTENTIONAL contract change (never to silence a diff):

    python -m harness.goldens contract
    python -m harness.goldens simulator
    python -m harness.goldens all
"""
from __future__ import annotations

import asyncio
import json
import pathlib

from harness import fixtures

GOLDENS = pathlib.Path(__file__).resolve().parent / "goldens"
TOOLS_LIST_GOLDEN = GOLDENS / "tools_list.json"
SIMULATOR_GOLDEN = GOLDENS / "simulator_baseline.json"

JSON_SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def load_golden(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_golden(path: pathlib.Path, doc: dict) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def contract_snapshot(contracts: dict[str, list[dict]]) -> dict:
    return {"servers": {name: sorted(tools, key=lambda t: t["name"])
                        for name, tools in sorted(contracts.items())}}


# --------------------------------------------------------------------------- schema checks


def _type_names(node: dict) -> set[str]:
    t = node.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {str(x) for x in t}
    names: set[str] = set()
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in node.get(key) or []:
            if isinstance(sub, dict):
                names |= _type_names(sub)
    return names


def schema_type_leaks(node, path: str = "$") -> list[str]:
    """Every `type` declaration must be a real JSON-Schema type name. A Python
    type name (`str`, `int`, ...) is the int-vs-str / null-vs-str bug class in
    its earliest form."""
    out: list[str] = []
    if isinstance(node, dict):
        t = node.get("type")
        if t is not None:
            names = {t} if isinstance(t, str) else set(map(str, t))
            bad = names - JSON_SCHEMA_TYPES
            if bad:
                out.append(f"{path}: non-JSON-Schema type declaration {sorted(bad)}")
        for key in ("properties", "patternProperties"):
            props = node.get(key)
            if isinstance(props, dict):
                for k, sub in props.items():
                    out.extend(schema_type_leaks(sub, f"{path}.{k}"))
        if isinstance(node.get("items"), dict):
            out.extend(schema_type_leaks(node["items"], f"{path}[]"))
        for key in ("anyOf", "oneOf", "allOf"):
            for i, sub in enumerate(node.get(key) or []):
                out.extend(schema_type_leaks(sub, f"{path}.{key}[{i}]"))
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            out.extend(schema_type_leaks(sub, f"{path}[{i}]"))
    return out


def declares_null(node: dict) -> bool:
    return "null" in _type_names(node)


def validate_against_schema(value, schema: dict | None, path: str = "$") -> list[str]:
    """Minimal JSON-Schema conformance over the output schemas mcp emits.

    Checks the bug class directly: nulls where no null is declared (null-vs-str)
    and string-serialized numbers / numeric-looking strings (int-vs-str).
    `total=False` TypedDicts mean missing keys are fine; present keys must type
    check against their declaration. Undeclared extra keys are allowed (JSON
    Schema default), so this stays focused on the type-mismatch bug class.
    """
    if schema is None:
        return []
    out: list[str] = []
    if not isinstance(value, (dict, list, str, int, float, bool)) and value is not None:
        return [f"{path}: non-JSON value {type(value).__name__}"]
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if options:
            results = [validate_against_schema(value, sub, path) for sub in options
                       if isinstance(sub, dict)]
            if not any(not r for r in results):
                out.append(f"{path}: matches none of the {key} branches "
                           f"(null-vs-type mismatch: value is "
                           f"{type(value).__name__})")
            return out
    expected = schema.get("type")
    if expected is not None:
        allowed = {expected} if isinstance(expected, str) else set(expected)
        if value is None:
            if "null" not in allowed:
                out.append(f"{path}: null where schema declares {sorted(allowed)} "
                           f"(null-vs-type bug class)")
            return out
        if isinstance(value, bool):
            ok = "boolean" in allowed
        elif isinstance(value, int) and not isinstance(value, bool):
            ok = bool({"integer", "number"} & allowed)
            if not ok and "string" in allowed:
                out.append(f"{path}: integer {value!r} declared as string (int-vs-str)")
                return out
        elif isinstance(value, float):
            ok = "number" in allowed
            if not ok and "string" in allowed:
                out.append(f"{path}: number {value!r} declared as string (int-vs-str)")
                return out
        elif isinstance(value, str):
            ok = "string" in allowed
            if not ok and ({"integer", "number"} & allowed):
                out.append(f"{path}: string {value!r} declared as numeric (int-vs-str)")
                return out
        elif isinstance(value, list):
            ok = "array" in allowed
        elif isinstance(value, dict):
            ok = "object" in allowed
        else:
            ok = False
        if not ok:
            out.append(f"{path}: {type(value).__name__} does not match declared "
                       f"{sorted(allowed)}")
            return out
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for k, v in value.items():
            if k in props:
                out.extend(validate_against_schema(v, props[k], f"{path}.{k}"))
        for k in schema.get("required") or []:
            if k not in value:
                out.append(f"{path}.{k}: required field missing")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            out.extend(validate_against_schema(item, schema["items"], f"{path}[{i}]"))
    return out


# --------------------------------------------------------------------------- regeneration


def write_contract_golden() -> pathlib.Path:
    from harness import stdio_driver

    async def _collect() -> dict[str, list[dict]]:
        env = fixtures.base_env(pathlib.Path(fixtures.ROOT) / ".harness-golden-tmp",
                                lake=False)
        out = {}
        for server in ("data", "warden", "gym"):
            out[server] = await stdio_driver.collect_contracts(server, env)
        return out

    contracts = asyncio.run(_collect())
    return save_golden(TOOLS_LIST_GOLDEN, contract_snapshot(contracts))


def write_simulator_golden() -> pathlib.Path:
    """Golden simulator baselines for a fixed (panel, seeds=[0,1], per_regime=2).

    Catches silent score drift across refactors and vendor re-pins: the pin
    checks hash vendored files but know nothing about behavior."""
    import os
    import tempfile

    fixtures.ensure_tests_path()
    os.environ.setdefault("SWARM_MCP_LOCAL_TOKEN", fixtures.TEST_TOKEN)
    os.environ.setdefault("SWARM_MCP_ACCESS_TOKEN", fixtures.TEST_TOKEN)
    tmp = tempfile.TemporaryDirectory(prefix="harness-sim-")
    os.environ["SWARM_MCP_CACHE_DB"] = str(pathlib.Path(tmp.name) / "cache.db")
    os.environ.pop("SWARM_MCP_TOKEN_VERIFY_URL", None)
    from swarm_mcp.tools import gym_tools

    rows = fixtures.bars_rows()
    doc: dict = {
        "geometry": {"panel": "synthetic_bars(days=500, seed=9, end=2026-05-29)",
                     "seeds": [0, 1], "per_regime": 2, "min_symbols": 5},
        "genomes": {},
    }

    async def _probe() -> None:
        for name, genome in (("baseline", fixtures.genome_baseline()),
                             ("mutate_a", fixtures.genome_challenger())):
            r = await gym_tools.probe_fragility(genome, seeds=[0, 1], per_regime=2,
                                               bars=rows, min_symbols=5)
            if "error" in r:
                raise RuntimeError(f"simulator baseline probe failed: {r['error']}")
            doc["genomes"][name] = {
                "genome_hash": r["genome_hash"],
                "n_episodes": r["n_episodes"],
                "mean_weekly_bps": r["mean_weekly_bps"],
                "per_regime_bps": r["per_regime_bps"],
                "worst_regime": r["worst_regime"],
                "turnover_pct_avg": r["turnover_pct_avg"],
                "max_gross_exposure": r["max_gross_exposure"],
                "max_position_pct": r["max_position_pct"],
            }

    asyncio.run(_probe())
    tmp.cleanup()
    return save_golden(SIMULATOR_GOLDEN, doc)


def main() -> None:
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    if target in ("contract", "all"):
        print(f"wrote {write_contract_golden()}")
    if target in ("simulator", "all"):
        print(f"wrote {write_simulator_golden()}")
    if target not in ("contract", "simulator", "all"):
        raise SystemExit("usage: python -m harness.goldens [contract|simulator|all]")


if __name__ == "__main__":
    main()

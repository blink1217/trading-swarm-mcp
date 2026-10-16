#!/usr/bin/env python3
"""Atomically bump quant-swarm version across all pinned files in trading-swarm-mcp.

Usage:
    python scripts/bump_version.py 0.6.0
    python scripts/bump_version.py --patch
    python scripts/bump_version.py --minor
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def current_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise RuntimeError("Could not find version in pyproject.toml")
    return m.group(1)


def compute_next(cur: str, mode: str) -> str:
    parts = [int(x) for x in cur.split(".")]
    while len(parts) < 3:
        parts.append(0)
    if mode == "patch":
        parts[2] += 1
    elif mode == "minor":
        parts[1] += 1
        parts[2] = 0
    elif mode == "major":
        parts[0] += 1
        parts[1] = 0
        parts[2] = 0
    else:
        return mode
    return f"{parts[0]}.{parts[1]}.{parts[2]}"


def replace_in_file(path: Path, pattern: str, repl: str, *, flags: int = 0) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, repl, text, flags=flags)
    if count == 0:
        raise RuntimeError(f"Pattern {pattern!r} not found in {path.relative_to(ROOT)}")
    path.write_text(new_text, encoding="utf-8")


def bump_all(new_ver: str) -> str:
    old_ver = current_version()
    if not re.match(r"^\d+\.\d+\.\d+$", new_ver):
        raise ValueError(f"Invalid semantic version: {new_ver!r}")

    # 1. pyproject.toml
    replace_in_file(
        ROOT / "pyproject.toml",
        r'^(version\s*=\s*)"[^"]+"',
        rf'\g<1>"{new_ver}"',
        flags=re.MULTILINE,
    )

    # 2. server.json
    server_json_path = ROOT / "server.json"
    server_meta = json.loads(server_json_path.read_text(encoding="utf-8"))
    server_meta["version"] = new_ver
    for pkg in server_meta.get("packages", []):
        pkg["version"] = new_ver
    server_json_path.write_text(json.dumps(server_meta, indent=2) + "\n", encoding="utf-8")

    # 3. swarm_mcp/server_meta.py
    replace_in_file(
        ROOT / "swarm_mcp" / "server_meta.py",
        r'^(PACKAGE_VERSION\s*=\s*)"[^"]+"',
        rf'\g<1>"{new_ver}"',
        flags=re.MULTILINE,
    )

    # 4. swarm_mcp/__init__.py
    replace_in_file(
        ROOT / "swarm_mcp" / "__init__.py",
        r'(__version__\s*=\s*)"[^"]+"',
        rf'\g<1>"{new_ver}"',
    )

    # 5. npm/package.json
    npm_pkg_path = ROOT / "npm" / "package.json"
    npm_pkg = json.loads(npm_pkg_path.read_text(encoding="utf-8"))
    npm_pkg["version"] = new_ver
    npm_pkg_path.write_text(json.dumps(npm_pkg, indent=2) + "\n", encoding="utf-8")

    # 6. npm/bin/quant-swarm-mcp.js
    replace_in_file(
        ROOT / "npm" / "bin" / "quant-swarm-mcp.js",
        r"quant-swarm==[0-9]+\.[0-9]+\.[0-9]+",
        f"quant-swarm=={new_ver}",
    )

    # 7. smithery.yaml
    replace_in_file(
        ROOT / "smithery.yaml",
        r"quant-swarm==[0-9]+\.[0-9]+\.[0-9]+",
        f"quant-swarm=={new_ver}",
    )

    # 8. .mcp.json
    mcp_json_path = ROOT / ".mcp.json"
    mcp_meta = json.loads(mcp_json_path.read_text(encoding="utf-8"))
    mcp_meta["version"] = new_ver
    mcp_json_path.write_text(json.dumps(mcp_meta, indent=2) + "\n", encoding="utf-8")

    # 9. monitoring/prober.py
    replace_in_file(
        ROOT / "monitoring" / "prober.py",
        r'^(PROBE_VERSION\s*=\s*)"[^"]+"',
        rf'\g<1>"{new_ver}"',
        flags=re.MULTILINE,
    )

    # 10. monitoring/grafana/alerts.json
    replace_in_file(
        ROOT / "monitoring" / "grafana" / "alerts.json",
        r'version!=\\"[0-9]+\.[0-9]+\.[0-9]+\\"',
        f'version!=\\"{new_ver}\\"',
    )

    # 11. tests/test_tournament.py
    replace_in_file(
        ROOT / "tests" / "test_tournament.py",
        r'assert r\["runner_version"\] == "[^"]+"',
        f'assert r["runner_version"] == "{new_ver}"',
    )

    return old_ver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bump quant-swarm version across all pinned files.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("version", nargs="?", help="Explicit semantic version (e.g. 0.6.0)")
    group.add_argument("--patch", action="store_true", help="Increment patch version (X.Y.Z+1)")
    group.add_argument("--minor", action="store_true", help="Increment minor version (X.Y+1.0)")
    group.add_argument("--major", action="store_true", help="Increment major version (X+1.0.0)")
    args = parser.parse_args(argv)

    cur = current_version()
    if args.patch:
        target = compute_next(cur, "patch")
    elif args.minor:
        target = compute_next(cur, "minor")
    elif args.major:
        target = compute_next(cur, "major")
    else:
        target = args.version

    old = bump_all(target)
    print(f"Bumped quant-swarm version: {old} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

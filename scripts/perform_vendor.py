"""Vendor the CHECKER subset of guardrails + alpha into swarm_mcp/vendored/
and record every vendored file's sha256 in .github/pins.json.

Invoked by scripts/vendor.ps1 after the sibling repos are checked out at the
pinned SHAs (or pointed at the live working trees with --worktree before the
alpha pin is committed).

IP boundary (user-confirmed): ship checkers only. objective.score /
objective.deflated_sharpe / gates.should_promote are stripped and stay
server-side; hard_constraint_violations, thresholds, provenance guards,
fill model, genome schema, tiers, budget semantics, gym tier-A simulator and
regime labeller are shipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from strip_guardrails import header_for, strip_functions

GUARDRAILS_WHOLE = [
    "provenance.py",
    "fill_model.py",
    "genome_schema.py",
    "genome_schema.json",
    "tiers.py",
    "budget.py",
]

GUARDRAILS_STRIPPED = {
    "objective.py": ["deflated_sharpe", "_norm_ppf", "score"],
    "gates.py": ["should_promote"],
}

ALPHA_FILES = {
    "bars_fetch.py": "services/data-bridge/bars_fetch.py",
    "order_checks.py": "services/warden/order_checks.py",
    "microstructure.py": "trade_bot_volume_predictor/microstructure_engine.py",
    "gym/__init__.py": "gym/__init__.py",
    "gym/panel.py": "gym/panel.py",
    "gym/regime.py": "gym/regime.py",
    "gym/simulator.py": "gym/simulator.py",
    "gym/policy_head.py": "gym/policy_head.py",
    "shared/__init__.py": "shared/__init__.py",
    "shared/swing_screens.py": "shared/swing_screens.py",
}

GUARDRAILS_LICENSE = """# trading-swarm-guardrails — vendored CHECKER subset (source-available)

Copyright (c) 2026 The 1.21 Initiative. All rights reserved.

These files are a subset of the privately developed trading-swarm-guardrails
rules layer, vendored at the commit SHA recorded in .github/pins.json.

You may use them as part of trading-swarm-mcp to validate orders, features,
genomes, and execution-cost assumptions locally. You may NOT:

1. redistribute these files standalone, or as part of a competing product;
2. modify them (the pin exists precisely so the checkers cannot drift);
3. use them to reconstruct the selection machinery that was deliberately
   excluded (objective scoring, deflated-Sharpe estimation, promotion gates).

The excluded selection machinery remains server-side and is not licensed.
"""

GUARDRAILS_PATH_SHIM = '''"""Vendoring shim — same resolution contract as trading-swarm-alpha's
guardrails_path.py, pointed at the vendored guardrails checker subset.

Resolution order:
1. `GYM_GUARDRAILS_DIR` env var (override)
2. vendored subset at <this file>/../guardrails
"""
from __future__ import annotations

import os
import sys


def guardrails_path() -> str:
    env = os.environ.get("GYM_GUARDRAILS_DIR", "")
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    vendored = os.path.normpath(os.path.join(here, "..", "guardrails"))
    if os.path.isdir(vendored) and any(os.path.isfile(os.path.join(vendored, m)) for m in ("objective.py", "gates.py")):
        return vendored
    raise ImportError("vendored guardrails subset not found — set GYM_GUARDRAILS_DIR or run scripts\\\\vendor.ps1")


def ensure_guardrails_on_path() -> str:
    p = guardrails_path()
    if p not in sys.path:
        sys.path.insert(0, p)
    return p


ensure_guardrails_on_path()
'''


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(dirpath: str) -> None:
    if os.path.isdir(dirpath):
        shutil.rmtree(dirpath)
    os.makedirs(dirpath, exist_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--guardrails-src", required=True)
    ap.add_argument("--alpha-src", required=True)
    ap.add_argument("--guardrails-sha", required=True)
    ap.add_argument("--alpha-sha", required=True)
    ap.add_argument("--worktree", action="store_true",
                    help="pins record the sibling working tree state (pre-commit bootstrap)")
    args = ap.parse_args()

    root = os.path.dirname(_HERE)
    vendored = os.path.join(root, "swarm_mcp", "vendored")
    g_dst = os.path.join(vendored, "guardrails")
    a_dst = os.path.join(vendored, "alpha")
    _clean(g_dst)
    _clean(a_dst)

    pins_guardrails_files = {}
    for name in GUARDRAILS_WHOLE:
        src = os.path.join(args.guardrails_src, name)
        dst = os.path.join(g_dst, name)
        shutil.copyfile(src, dst)
        pins_guardrails_files[name] = {"origin": "verbatim", "sha256": sha256_file(dst)}
    for name, excluded in GUARDRAILS_STRIPPED.items():
        with open(os.path.join(args.guardrails_src, name), encoding="utf-8") as f:
            source = f.read()
        stripped = strip_functions(source, set(excluded), header_for(name, args.guardrails_sha, excluded))
        dst = os.path.join(g_dst, name)
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(stripped)
        pins_guardrails_files[name] = {"origin": "stripped", "excluded": excluded, "sha256": sha256_file(dst)}
    with open(os.path.join(g_dst, "LICENSE.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(GUARDRAILS_LICENSE)
    with open(os.path.join(g_dst, "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")

    pins_alpha_files = {}
    for dst_rel, src_rel in ALPHA_FILES.items():
        src = os.path.join(args.alpha_src, src_rel)
        dst = os.path.join(a_dst, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        pins_alpha_files[dst_rel] = {"source": src_rel, "origin": "verbatim", "sha256": sha256_file(dst)}
    with open(os.path.join(a_dst, "guardrails_path.py"), "w", encoding="utf-8", newline="\n") as f:
        f.write(GUARDRAILS_PATH_SHIM)

    with open(os.path.join(vendored, "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(a_dst, "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")

    pins = {
        "schema": 1,
        "vendored_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "worktree_pin": bool(args.worktree),
        "guardrails": {
            "repo": "blink1217/trading-swarm-guardrails",
            "sha": args.guardrails_sha,
            "files": pins_guardrails_files,
        },
        "alpha": {
            "repo": "blink1217/trading-swarm-alpha",
            "sha": args.alpha_sha,
            "files": pins_alpha_files,
        },
        "local": {
            "guardrails/guardrails_path_shim": sha256_file(os.path.join(a_dst, "guardrails_path.py")),
            "guardrails/LICENSE.md": sha256_file(os.path.join(g_dst, "LICENSE.md")),
        },
    }
    pins_path = os.path.join(root, ".github", "pins.json")
    os.makedirs(os.path.dirname(pins_path), exist_ok=True)
    with open(pins_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(pins, f, indent=2, sort_keys=True)
        f.write("\n")

    n = len(pins_guardrails_files) + len(pins_alpha_files)
    print(f"vendored {n} pinned files (guardrails {args.guardrails_sha[:12]}, alpha {args.alpha_sha[:12]})"
          + (" [WORKTREE PIN — commit the alpha change and re-run before first public push]" if args.worktree else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

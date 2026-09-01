"""Verify the committed vendored tree matches .github/pins.json.

Local mode (default): every vendored file's sha256 equals the pinned hash.
Source mode (--guardrails-src/--alpha-src at the pinned checkouts): verbatim
files are byte-identical to source, stripped files equal the source after the
identical strip transform, and the excluded functions are absent.

CI runs this after checking out the pinned SHAs from the private repos
(mirrors trading-swarm-alpha/.github/workflows/guardrails-invariants.yml).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from perform_vendor import ALPHA_FILES, GUARDRAILS_STRIPPED, GUARDRAILS_WHOLE  # noqa: E402
from strip_guardrails import header_for, strip_functions  # noqa: E402


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pins", default=None)
    ap.add_argument("--guardrails-src", default=None)
    ap.add_argument("--alpha-src", default=None)
    args = ap.parse_args()

    root = os.path.dirname(_HERE)
    pins_path = args.pins or os.path.join(root, ".github", "pins.json")
    with open(pins_path, encoding="utf-8") as f:
        pins = json.load(f)

    g_dst = os.path.join(root, "swarm_mcp", "vendored", "guardrails")
    a_dst = os.path.join(root, "swarm_mcp", "vendored", "alpha")
    failures: list[str] = []

    for name, meta in pins["guardrails"]["files"].items():
        path = os.path.join(g_dst, name)
        if not os.path.isfile(path):
            failures.append(f"guardrails/{name}: missing vendored file")
            continue
        if _sha256(path) != meta["sha256"]:
            failures.append(f"guardrails/{name}: hash drift vs pins.json")

    for name, meta in pins["alpha"]["files"].items():
        path = os.path.join(a_dst, name)
        if not os.path.isfile(path):
            failures.append(f"alpha/{name}: missing vendored file")
            continue
        if _sha256(path) != meta["sha256"]:
            failures.append(f"alpha/{name}: hash drift vs pins.json")

    if args.guardrails_src:
        sha = pins["guardrails"]["sha"]
        for name in GUARDRAILS_WHOLE:
            src = os.path.join(args.guardrails_src, name)
            dst = os.path.join(g_dst, name)
            if os.path.isfile(dst) and open(src, "rb").read() != open(dst, "rb").read():
                failures.append(f"guardrails/{name}: differs from source at pinned SHA {sha[:12]}")
        for name, excluded in GUARDRAILS_STRIPPED.items():
            with open(os.path.join(args.guardrails_src, name), encoding="utf-8") as f:
                expected = strip_functions(f.read(), set(excluded), header_for(name, sha, excluded))
            with open(os.path.join(g_dst, name), encoding="utf-8") as f:
                actual = f.read()
            if expected != actual:
                failures.append(f"guardrails/{name}: stripped subset differs from pinned source transform")
            for fn in excluded:
                if f"def {fn}(" in actual:
                    failures.append(f"guardrails/{name}: excluded function {fn} present in vendored subset")

    if args.alpha_src:
        for dst_rel, src_rel in ALPHA_FILES.items():
            src = os.path.join(args.alpha_src, src_rel)
            dst = os.path.join(a_dst, dst_rel)
            if os.path.isfile(dst) and open(src, "rb").read() != open(dst, "rb").read():
                failures.append(f"alpha/{dst_rel}: differs from source {src_rel} at pinned SHA {pins['alpha']['sha'][:12]}")

    if failures:
        print("VENDORED PIN CHECK FAILED:")
        for f_ in failures:
            print("  -", f_)
        return 1
    print("vendored tree matches pins.json"
          + (" and pinned sources" if (args.guardrails_src or args.alpha_src) else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

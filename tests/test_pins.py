"""The committed vendored tree must match .github/pins.json (CI runs the same
check against the pinned source checkouts)."""
from __future__ import annotations

import os
import subprocess
import sys


def test_vendored_tree_matches_pins():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, os.path.join(root, "scripts", "check_pin.py")],
        cwd=root, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

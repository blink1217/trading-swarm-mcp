"""Generates swarm_mcp/data/docs/*.md from README.md sections (7.3: MCP resources).

The MCP servers register read-only `swarm://docs/*` resources pointing at these
files, so agents can read methodology / PIT datasets / verdict semantics / plan
limits as MCP resources instead of burning relay calls. Generated at build time;
a test asserts the files are in sync with the README.

Usage: python scripts/build_docs_resources.py [--check]
The extracted sections (README headings -> resource file):
  "PIT significance, explained"      -> pit-datasets.md  (+ dataset table source)
  "Resources and capabilities"       -> (n/a)
  headings chosen: see SECTIONS below
--check exits non-zero when the files would change (CI / pre-commit).
"""
from __future__ import annotations

import io
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
OUT = ROOT / "swarm_mcp" / "data" / "docs"

# the docs surface is agent-facing: provider/budget details of the local machine
# are technical-local and are scrubbed to neutral wording (parity-tested)
SCRUB = [
    (r"altdata", "the data relay"),
    (r"Alpaca", "the bars source"),
    (r"alpaca", "the bars source"),
    (r"AltData", "the data relay"),
    (r"benzinga", "the news vendor"),
    (r"Benzinga", "the news vendor"),
    (r"finnhub", "the news vendor"),
    (r"Finnhub", "the news vendor"),
    (r"finviz", "a data vendor"),
    (r"Finviz", "a data vendor"),
]


def scrub(text: str) -> str:
    for pattern, replacement in SCRUB:
        text = re.sub(pattern, replacement, text)
    return text

# README "### Heading" -> list of output files the section feeds
SECTIONS: dict[str, list[str]] = {
    "PIT significance, explained (`pit.analysis`)": ["pit-datasets.md"],
    "Point-in-time data, for real": ["pit-datasets.md"],
    "Plans and the credit rate card": ["plans.md"],
}

# Methodology + verdict semantics are prose-pinned here (repo of truth) rather
# than scraped from prose that changes copy-wise:
METHODOLOGY = """\
# quant-swarm methodology

Cross-sectional Spearman rank IC per (dataset|metric|horizon) family, computed
point-in-time (each dataset dated by when it was publicly knowable). Rolling
52-week IR on a size/sector-neutralized metric against neutralized forward
returns (log recent dollar volume + sector dummies per date). Inference is
Newey-West (Bartlett) at lag ceil(horizon/5) because horizons longer than the
decision spacing overlap. Raw accounting levels are diagnostic-only; the L1
gate tests scale-free variants (Year-over-year growth, per-share,
assets-scaled). Families whose latest release is older than the dataset
staleness cap are never judged. A shuffled-date placebo runs against every
release; a flagged run (gate_unreliable) withholds all verdicts.
"""

VERDICTS = """\
# quant-swarm verdicts (pit.analysis)

- SIGNIFICANT: an L1 survivor of the SAME pit run - rolling 52-week IR > 0 and
  Benjamini-Hochberg q <= 0.05 across every family tested, with harmonic-mean p
  and the Newey-West t recorded. A raw p <= 0.05 alone is never enough.
- NOT_SIGNIFICANT: states the reason (negative IR, or a nominal p that does not
  survive the multiple-testing correction).
- NOT_JUDGED: below the 26-observation floor, stale release, or a diagnostic-only
  raw level - the p-value column is null and no claim is made.
- UNVERIFIED: no gate artifact, gate from a different pit run, or a failed
  placebo - a raw p-value is never upgraded on its own.
- research_only: the dataset covers under 50% of the universe - research
  evidence, not a tradable signal.

Verdicts describe a family's cross-sectional predictive power across the
universe - never a forecast for one name - and are not investment advice.
"""


def section_text(heading: str) -> str:
    s = io.open(README, encoding="utf-8").read()
    m = re.search(rf"(?ms)^###+ {re.escape(heading)}.*?$(.*?)(?=^###+ |\Z)", s)
    return m.group(0).rstrip() + "\n" if m else ""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    check = "--check" in sys.argv
    pit_datasets = []
    # the dataset table from the README's pit section is the pit-datasets body
    for heading, targets in SECTIONS.items():
        text = section_text(heading)
        if "pit-datasets.md" in targets:
            if not text:
                print(f"README lacks the section {heading!r}", file=sys.stderr)
                return 2
            pit_datasets.append(text.strip() + "\n")
    files = {
        "methodology.md": METHODOLOGY,
        "verdicts.md": VERDICTS,
        "pit-datasets.md": "\n".join(pit_datasets) if pit_datasets else "# pit datasets\n",
        "plans.md": section_text("Plans").rstrip() + "\n" if section_text("Plans") else "# plans\n",
    }
    rc = 0
    for name, content in files.items():
        content = scrub(content)
        target = OUT / name
        if check:
            want = content.strip()
            have = target.read_text(encoding="utf-8").strip() if target.exists() else ""
            if have != want:
                print(f"out of date: {name} (run scripts/build_docs_resources.py)")
                rc = 1
        else:
            target.write_text(content, encoding="utf-8", newline="\n")
            print(f"wrote {target.relative_to(ROOT)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

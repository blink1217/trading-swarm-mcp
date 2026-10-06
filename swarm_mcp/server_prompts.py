"""Shared MCP prompt templates (7.2): copy-paste workflows agents ask for.

Each prompt chains the tools it needs and carries the honesty rules verbatim:
the model must quote the tool's verdict sentences as-is, never upgrade a raw
p-value to "significant", and never present cross-sectional verdicts as a
forecast for one name.
"""
from __future__ import annotations

import pathlib

_DOCS_DIR = pathlib.Path(__file__).resolve().parent / "data" / "docs"


def _read_doc(fname: str) -> str:
    """Read a generated docs resource (built by scripts/build_docs_resources.py)."""
    return (_DOCS_DIR / fname).read_text(encoding="utf-8")


PROMPT_INDEX = {
    "data": ["explain_pit_significance", "regime_check"],
    "warden": ["pre_trade_check", "audit_my_strategy"],
    "gym": ["stress_test_genome"],
}


def explain_pit_significance(symbol: str) -> str:
    """Is this alternative-data signal real for {symbol} - and how do you know?"""
    return f"""Explain the PIT-correlation significance results for {symbol} using the pit.analysis tool.

Steps:
1. Call pit.analysis with symbols=["{symbol}"].
2. Report the headline line verbatim (X of Y family-horizon tests SIGNIFICANT).
3. For each family returned, quote its per-horizon verdict sentences VERBATIM:
   SIGNIFICANT rows (with rolling IR, Newey-West p, BH q), NOT_SIGNIFICANT rows
   (say why - negative IR, or a nominal p that did not survive the Benjamini-
   Hochberg correction), and NOT_JUDGED/-UNVERIFIED rows (observation floor,
   staleness, licence withholding, failed placebo). Never paraphrase a verdict
   into "this works" or "this is alpha"; never upgrade a raw p <= 0.05 to
   significant; never call a NOT_SIGNIFICANT row "promising".
4. State the caveat exactly: these verdicts describe a family's cross-sectional
   predictive power across the universe - they are not a forecast for {symbol},
   and they are not investment advice.
5. Close with what the user can check next (e.g. only_significant, other
   symbols, the as_of point-in-time cutoff)."""


def regime_check(symbol: str) -> str:
    """What regime is {symbol} in right now, and what does that mean for sizing?"""
    return f"""Check the current market regime for {symbol} and translate it into context a
risk gate can act on.

Steps:
1. Call market.regime (symbols=["{symbol}"]) and read the regime label and its
   provenance/coverage fields. If coverage is partial, say so and stop at what
   is knowable point-in-time.
2. Call market.pulse for {symbol} and report the derived snapshot (ranks,
   buckets, counts) - never raw quotes.
3. Summarize: regime label, key conditions, and what warden checks would be the
   natural next step (warden.validate_order / warden.explain_sizing) before any
   order. Do not invent regime logic the tool did not return; quote its labels.
4. Close with: analysis only, not investment advice."""


def pre_trade_check(order: str) -> str:
    """Will this order pass the pre-trade risk gates? {order}"""
    return f"""Run the full pre-trade gate sequence for this order: {order}

Steps:
1. Call warden.validate_order with the order payload (and any positions given).
2. Call warden.cost_check with the same order to price the commissions/fees.
3. Call warden.explain_sizing to explain the position-size floor that bound (or
   the headroom), so the user can see WHICH check was decisive.
4. Report each checker's verdict VERBATIM (PASS/FAIL and the boundary that
   fired). If any checker refused, the summary verdict is REFUSED - never soften
   a refusal into "probably fine".
5. Note the checks are pre-trade state only (no order is ever placed, cancelled
   or routed by these tools) and are not investment advice."""


def audit_my_strategy(genome: str) -> str:
    """Audit this strategy genome for look-ahead, leakage and promo gates. {genome}"""
    return f"""Audit this strategy genome before it earns any promotion claims: {genome}

Steps:
1. Call warden.validate_genome with the genome (parameter vector + rules) and
   repeat its findings exactly: which invariants it violates, if any.
2. Call warden.audit_features on the feature list and quote each leak finding
   (look-ahead, overlapping windows, survivorship) verbatim.
3. If both pass, point the user at the Shadow Tournament path: gym
   (tournament.submit) - a promotion verdict still requires the hosted run;
   say plainly that a local backtest alone never earns the word "verified".
4. Never upgrade warden findings ("sounds OK"); either quote a PASS with the
   boundaries checked, or quote the failure and the fix it names."""


def stress_test_genome(genome: str) -> str:
    """Does this genome survive regime stress and how much would a hosted run cost? {genome}"""
    return f"""Stress-test this genome's fragility and price a hosted league run: {genome}

Steps:
1. Call gym.label_regimes on the panel and repeat the regime naming verbatim;
   say clearly which labels are UNDERPOWERED (below the minimum episodes) and
   that those labels cannot support conclusions.
2. Call gym.probe_fragility (baseline vs perturbations) and report the delta
   bands and the fragility label exactly as returned.
3. Optionally call gym.paired_preview for a single-paired-preview headline and
   quote its confidence caveats (UNDERPOWERED where applicable).
4. Call gym.estimate_cloud_run to price a full hosted league run (credits),
   and hand the user the exact arguments for tournament.submit - do NOT submit
   automatically.
5. Close with: gym results are descriptive diagnostics, not promotion verdicts;
   promotion is decided only by the hosted tournament plus the planner gates,
   and none of this is investment advice."""

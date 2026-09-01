"""Provenance guards — R9. The future must never leak into a past decision.

The trap these guards exist for (plan "Critical traps"): Open-Meteo's ARCHIVE
API returns ACTUALS, not what the forecast said on a past Friday. Substituting
actuals for a past decision date injects the future into training data. The
same applies to any "reconstruct tier-C feature from a live API" shortcut:
LLM verdicts are irreproducible (model drift + `:online` retrieval) and
`finviz_score` is an opaque vendor number with zero history.

Rule: for any decision date BEFORE the tape started, point-in-time features of
tier B/C may only come from the archived tape. If the tape does not have them,
the correct answer is UNSCORABLE — never a silent substitute.
"""
from __future__ import annotations

import datetime as _dt

FEATURE_TIERS = {
    # tier A — reconstructable from bars_1day (~10y)
    "atr": "A", "mom_5d": "A", "mom_20d": "A", "vwap_stretch_20": "A",
    "vol_ratio_20": "A", "gap_open": "A", "breakout_dist_20d": "A", "rsi_14": "A",
    # tier B — ~1y, degraded
    "finnhub_sentiment": "B", "earnings_flag": "B",
    # tier C — tape-only going forward
    "finviz_score": "C", "open_meteo_forecast_anomaly": "C",
    "energy_bias": "C", "llm_verdict": "C",
}

# Sources that return ACTUALS (or otherwise non-point-in-time data). Using
# them for a past decision date is a provenance violation, full stop.
BANNED_ACTUALS_SOURCES = frozenset({
    "open-meteo.archive",          # returns actuals, not past forecasts
    "open-meteo.historical",
    "openweather.onemap_history",
    "live_api_snapshot",           # today's live vendor snapshot
})


class ProvenanceViolation(Exception):
    """Raised LOUDLY when future data would be substituted for a past decision."""


class UnscorableFeature(Exception):
    """Raised when a feature has no legitimate point-in-time source."""


def feature_tier(name: str) -> str:
    """Tier of a named feature. Unknown features fail closed to tier C."""
    return FEATURE_TIERS.get(name, "C")


def assert_point_in_time(source: str, decision_date, tape_started, now: _dt.date | None = None) -> None:
    """Guard a feature-fetch for `decision_date` against future leakage.

    - Banned actuals sources always raise for decision dates strictly before
      today (they cannot represent what was knowable then).
    - For decision dates before `tape_started`, NO source is legitimate for
      point-in-time reconstruction: the data either was recorded on the tape
      (pass tape_started <= decision_date) or it is gone. We raise rather than
      guess — unscorable beats fabricated.

    `decision_date`/`tape_started` accept date, datetime, or ISO strings.
    """
    d = _to_date(decision_date)
    today = now or _dt.datetime.now(_dt.timezone.utc).date()
    if source in BANNED_ACTUALS_SOURCES and d < today:
        raise ProvenanceViolation(
            f"source {source!r} returns actuals/non-point-in-time data — cannot serve decision date {d} "
            f"(would inject the future). Use the archived tape or mark UNSCORABLE.")
    ts = _to_date(tape_started)
    if d < ts:
        raise ProvenanceViolation(
            f"decision date {d} predates tape start {ts} — no point-in-time record exists; "
            f"the feature is UNSCORABLE for this date (never substitute actuals).")


def coverage_fraction(selected_symbols, snapshot_universe) -> float | None:
    """R8 coverage: fraction of the challenger's selected symbols present in the
    archived snapshot universe. None when nothing was selected (caller decides)."""
    sel = set(selected_symbols or [])
    if not sel:
        return None
    snap = set(snapshot_universe or [])
    return len(sel & snap) / len(sel)


def _to_date(v) -> _dt.date:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    return _dt.date.fromisoformat(str(v)[:10])

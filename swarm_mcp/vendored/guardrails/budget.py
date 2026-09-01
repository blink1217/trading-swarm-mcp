"""Budget enforcement for the evolution loop.

Every LLM call, gym job, and BigQuery job charges its estimated cost to `budget_ledger`
BEFORE executing (pre-flight). The circuit breaker refuses new *search/code-authoring*
work at 80% of the monthly cap while keeping measurement (retro, ledger audits) running.
Billing alerts are a lagging secondary tripwire only.

Pure functions here so warden/evolution can share the same logic and tests run locally.
"""
from __future__ import annotations

import datetime as _dt
import json
import os

MONTHLY_CAP_USD = 150.0
BREAKER_FRACTION = 0.80   # disable search + code authoring at 80% of cap
BREAKER_COOLDOWN_S = 300.0


def month_key(now: _dt.datetime | None = None) -> str:
    d = now or _dt.datetime.now(_dt.timezone.utc)
    return f"{d.year}-{d.month:02d}"


def month_to_date(entries: list[dict], now: _dt.datetime | None = None) -> float:
    """Sum cost_usd for ledger entries in the current month."""
    mk = month_key(now)
    return float(sum(e.get("cost_usd", 0.0) for e in entries if e.get("month") == mk))


def budget_status(
    entries: list[dict],
    now: _dt.datetime | None = None,
    cap: float = MONTHLY_CAP_USD,
    breaker_fraction: float = BREAKER_FRACTION,
) -> dict:
    """Derive breaker state from the ledger.

    Returns {'month', 'spent_usd', 'cap_usd', 'fraction', 'search_disabled',
             'measurement_ok'}.
    """
    spent = month_to_date(entries, now)
    frac = spent / cap if cap > 0 else 0.0
    return {
        "month": month_key(now),
        "spent_usd": round(spent, 2),
        "cap_usd": cap,
        "fraction": round(frac, 4),
        "search_disabled": frac >= breaker_fraction,
        "measurement_ok": True,  # measurement never disabled by the budget breaker
    }


def preflight_charge(kind: str, service: str, est_cost_usd: float, entries: list[dict],
                     now: _dt.datetime | None = None, cap: float = MONTHLY_CAP_USD,
                     breaker_fraction: float = BREAKER_FRACTION) -> tuple[dict, list[dict]]:
    """Pre-flight budget check + charge. Returns (status, updated_entries).

    Refuses (raises BudgetExceeded) when the month-to-date spend is already at or above
    the cap. Search/code-authoring kinds are additionally blocked when the breaker
    fraction is reached; `measurement` kinds are always allowed (measurement never stops).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    status = budget_status(entries, now, cap, breaker_fraction)
    if status["spent_usd"] >= cap:
        raise BudgetExceeded(f"{status['month']} spend {status['spent_usd']} >= cap {cap}")
    if status["search_disabled"] and kind != "measurement":
        raise SearchBreakerOpen(
            f"{status['month']} at {status['fraction']:.0%} of cap — search/code-authoring disabled, measurement still allowed")
    entry = {
        "month": status["month"],
        "service": service,
        "kind": kind,
        "cost_usd": round(float(est_cost_usd), 4),
        "charged_at": now.isoformat(),
    }
    return status, entries + [entry]


class BudgetExceeded(Exception):
    pass


class SearchBreakerOpen(Exception):
    pass


# ---- Optional BigQuery persistence helpers (warden/evolution use these) ----

def load_ledger_json(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_ledger_json(entries: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, default=str)

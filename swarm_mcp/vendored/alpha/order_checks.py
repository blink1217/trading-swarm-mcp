"""Runtime order + portfolio validation — the warden's hard floors.

Pure functions so they run in tests and in the warden service identically.
These are the runtime mirror of the guardrails objective's hard constraints:
a live order that would breach a floor is REJECTED here (before execution), not
penalized after the fact. The floors match objective.py exactly — if they
drift, the invariant tests catch it.
"""
from __future__ import annotations

import guardrails_path  # noqa: E402,F401

from objective import MAX_GROSS_EXPOSURE_PCT, MAX_POSITION_PCT  # guardrails


def check_position_size(position_pct: float, cap: float = MAX_POSITION_PCT) -> list[str]:
    """Validate a single position's size as a fraction of equity."""
    v = []
    if position_pct < 0:
        v.append(f"position_pct {position_pct} negative")
    if position_pct > cap + 1e-9:
        v.append(f"position_pct {position_pct:.4f} > cap {cap}")
    return v


def check_gross_exposure(gross_pct: float, cap: float = MAX_GROSS_EXPOSURE_PCT) -> list[str]:
    """Validate total gross exposure as a fraction of equity."""
    v = []
    if gross_pct < 0:
        v.append(f"gross_pct {gross_pct} negative")
    if gross_pct > cap + 1e-9:
        v.append(f"gross_pct {gross_pct:.4f} > cap {cap}")
    return v


def check_order(order: dict, equity: float, current_positions: dict | None = None) -> dict:
    """Validate a proposed order against the floors.

    order: {'symbol': str, 'notional': float, 'side': 'buy'|'sell'}
    current_positions: {symbol: notional} of the existing book.

    Returns {'ok': bool, 'violations': [...], 'post_gross_pct': float,
    'post_position_pct': float}. A buy that would push a name or the gross over
    its cap is rejected; sells never increase exposure.
    """
    current_positions = current_positions or {}
    symbol = order.get("symbol", "")
    notional = float(order.get("notional", 0.0))
    side = str(order.get("side", "")).lower()
    violations = []

    if not symbol:
        violations.append("order missing symbol")
    if notional <= 0:
        violations.append(f"order notional {notional} must be positive")
    if side not in ("buy", "sell"):
        violations.append(f"unknown side {side!r}")

    if equity <= 0:
        return {"ok": False, "violations": ["equity must be positive"],
                "post_gross_pct": 0.0, "post_position_pct": 0.0}

    # Projected book after the order
    book = dict(current_positions)
    if side == "buy":
        book[symbol] = book.get(symbol, 0.0) + notional
    else:
        book[symbol] = max(0.0, book.get(symbol, 0.0) - notional)

    post_position_pct = book.get(symbol, 0.0) / equity
    post_gross_pct = sum(book.values()) / equity

    violations.extend(check_position_size(post_position_pct))
    violations.extend(check_gross_exposure(post_gross_pct))

    return {
        "ok": not violations,
        "violations": violations,
        "post_gross_pct": round(post_gross_pct, 6),
        "post_position_pct": round(post_position_pct, 6),
    }

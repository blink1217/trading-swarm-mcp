"""Freshness rules: immutable past, in-progress session TTL, enrichment TTL."""
from __future__ import annotations

import datetime as dt

BAR_IN_PROGRESS_TTL_S = 60.0
ENRICHMENT_TTL_S = 300.0


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_today(now: dt.datetime | None = None) -> dt.date:
    return (now or utcnow()).date()


def session_date(ts: str | dt.date | dt.datetime) -> dt.date:
    if isinstance(ts, dt.datetime):
        return ts.date()
    if isinstance(ts, dt.date):
        return ts
    return dt.date.fromisoformat(str(ts)[:10])


def is_finalized_session(ts, now: dt.datetime | None = None) -> bool:
    """A session strictly before today is complete and never re-fetched."""
    return session_date(ts) < utc_today(now)


def age_seconds(fetched_at: str, now: dt.datetime | None = None) -> float:
    f = dt.datetime.fromisoformat(fetched_at)
    if f.tzinfo is None:
        f = f.replace(tzinfo=dt.timezone.utc)
    return ((now or utcnow()) - f).total_seconds()


def is_fresh_bar_row(fetched_at: str, ts, now: dt.datetime | None = None) -> bool:
    """Finalized sessions are always fresh; the in-progress session has a TTL."""
    if is_finalized_session(ts, now):
        return True
    return age_seconds(fetched_at, now) <= BAR_IN_PROGRESS_TTL_S


def is_fresh_enrichment(fetched_at: str, now: dt.datetime | None = None,
                        ttl_s: float = ENRICHMENT_TTL_S) -> bool:
    return age_seconds(fetched_at, now) <= ttl_s

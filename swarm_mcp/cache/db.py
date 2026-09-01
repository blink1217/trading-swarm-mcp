"""SQLite (WAL) bar/enrichment cache with MERGE-equivalent upsert semantics.

Location: %LOCALAPPDATA%\\1.21-initiative\\swarm-mcp\\cache.db on Windows,
$XDG_CACHE_HOME/1.21-initiative/swarm-mcp/cache.db on POSIX.

The bars upsert mirrors the BigQuery MERGE in trading-swarm-alpha's
data-bridge (ON symbol+ts match -> UPDATE, else INSERT), keyed here on
(provider, symbol, timeframe, adjustment, ts). Enrichment is append-only on
(symbol, kind, fetched_at) so a later fetch can never rewrite an earlier as_of.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (provider, symbol, timeframe, adjustment, ts)
);
CREATE TABLE IF NOT EXISTS enrichment (
    provider TEXT NOT NULL,
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (provider, symbol, kind, fetched_at)
);
CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT,
    endpoint TEXT,
    n_symbols INTEGER,
    rows INTEGER,
    cached INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS provenance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tool TEXT,
    symbol TEXT,
    source TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_bars_symbol ON bars (provider, symbol, timeframe, adjustment, ts);
CREATE INDEX IF NOT EXISTS idx_enrichment_lookup ON enrichment (provider, symbol, kind, fetched_at);
"""

BAR_COLUMNS = ("symbol", "ts", "open", "high", "low", "close", "volume")


def cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "1.21-initiative" / "swarm-mcp"


def default_db_path() -> Path:
    return cache_dir() / "cache.db"


class CacheDB:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = str(path or default_db_path())
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert_bars(self, provider: str, timeframe: str, adjustment: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        payload = [
            (provider, r["symbol"], timeframe, adjustment, str(r["ts"]),
             float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
             int(r["volume"]), r.get("fetched_at") or now)
            for r in rows
        ]
        self.conn.executemany(
            """
            INSERT INTO bars (provider, symbol, timeframe, adjustment, ts, open, high, low, close, volume, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, symbol, timeframe, adjustment, ts) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume, fetched_at=excluded.fetched_at
            """,
            payload,
        )
        self.conn.commit()
        return len(payload)

    def get_bars(self, provider: str, symbols: list[str], timeframe: str = "1Day",
                 adjustment: str = "split", start: str | None = None,
                 end: str | None = None) -> list[dict]:
        q = ("SELECT symbol, ts, open, high, low, close, volume, fetched_at FROM bars "
             "WHERE provider=? AND timeframe=? AND adjustment=? AND symbol IN "
             f"({','.join('?' for _ in symbols)})")
        params: list = [provider, timeframe, adjustment, *symbols]
        if start:
            q += " AND ts >= ?"
            params.append(str(start))
        if end:
            q += " AND ts <= ?"
            params.append(str(end))
        q += " ORDER BY symbol, ts"
        rows = self.conn.execute(q, params).fetchall()
        return [
            {"symbol": r["symbol"], "ts": r["ts"], "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"], "volume": r["volume"],
             "fetched_at": r["fetched_at"]}
            for r in rows
        ]

    def session_bounds(self, provider: str, symbol: str, timeframe: str = "1Day",
                       adjustment: str = "split") -> tuple[str | None, str | None]:
        r = self.conn.execute(
            "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM bars "
            "WHERE provider=? AND symbol=? AND timeframe=? AND adjustment=?",
            (provider, symbol, timeframe, adjustment),
        ).fetchone()
        return (r["lo"], r["hi"]) if r else (None, None)

    def count_bars(self, provider: str, symbol: str, timeframe: str = "1Day",
                   adjustment: str = "split") -> int:
        r = self.conn.execute(
            "SELECT COUNT(*) AS n FROM bars WHERE provider=? AND symbol=? AND timeframe=? AND adjustment=?",
            (provider, symbol, timeframe, adjustment),
        ).fetchone()
        return int(r["n"]) if r else 0

    def cached_symbols(self, provider: str, timeframe: str = "1Day",
                       adjustment: str = "split") -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT symbol FROM bars WHERE provider=? AND timeframe=? AND adjustment=? ORDER BY symbol",
            (provider, timeframe, adjustment),
        ).fetchall()
        return [r["symbol"] for r in rows]

    def append_enrichment(self, provider: str, symbol: str, kind: str, payload: dict,
                          fetched_at: str | None = None) -> str:
        ts = fetched_at or dt.datetime.now(dt.timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR IGNORE INTO enrichment (provider, symbol, kind, fetched_at, payload) VALUES (?, ?, ?, ?, ?)",
            (provider, symbol, kind, ts, json.dumps(payload, default=str)),
        )
        self.conn.commit()
        return ts

    def latest_enrichment(self, provider: str, symbol: str, kind: str,
                          before: str | None = None) -> dict | None:
        q = ("SELECT fetched_at, payload FROM enrichment "
             "WHERE provider=? AND symbol=? AND kind=?")
        params: list = [provider, symbol, kind]
        if before:
            q += " AND fetched_at <= ?"
            params.append(before)
        q += " ORDER BY fetched_at DESC LIMIT 1"
        r = self.conn.execute(q, params).fetchone()
        if not r:
            return None
        payload = json.loads(r["payload"])
        payload["fetched_at"] = r["fetched_at"]
        return payload

    def log_api_call(self, provider: str, endpoint: str, n_symbols: int, rows: int,
                     cached: bool) -> None:
        self.conn.execute(
            "INSERT INTO api_calls (ts, provider, endpoint, n_symbols, rows, cached) VALUES (?, ?, ?, ?, ?, ?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(), provider, endpoint, n_symbols, rows, int(cached)),
        )
        self.conn.commit()

    def log_provenance(self, tool: str, symbol: str, source: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO provenance_log (ts, tool, symbol, source, detail) VALUES (?, ?, ?, ?, ?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(), tool, symbol, source, detail),
        )
        self.conn.commit()

    def stats(self) -> dict:
        bars = self.conn.execute(
            "SELECT provider, timeframe, COUNT(DISTINCT symbol) AS symbols, COUNT(*) AS rows, "
            "MIN(ts) AS oldest, MAX(ts) AS newest FROM bars GROUP BY provider, timeframe"
        ).fetchall()
        enrich = self.conn.execute(
            "SELECT provider, COUNT(DISTINCT symbol) AS symbols, COUNT(*) AS rows FROM enrichment GROUP BY provider"
        ).fetchall()
        calls = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cached), 0) AS cached FROM api_calls"
        ).fetchone()
        return {
            "bars": [dict(r) for r in bars],
            "enrichment": [dict(r) for r in enrich],
            "api_calls": {"total": calls["n"], "cache_hits": calls["cached"]},
            "db_path": self.path,
        }


_DB: CacheDB | None = None


def get_db() -> CacheDB:
    global _DB
    if _DB is None:
        _DB = CacheDB(os.environ.get("SWARM_MCP_CACHE_DB") or None)
    return _DB


def reset_db() -> None:
    global _DB
    _DB = None

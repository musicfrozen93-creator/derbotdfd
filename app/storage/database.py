"""SQLite (WAL mode) storage."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    symbol TEXT NOT NULL,
    epoch  INTEGER NOT NULL,
    quote  TEXT NOT NULL,          -- exact fixed-point text at the symbol's pip precision
    digit  INTEGER NOT NULL,
    PRIMARY KEY (symbol, epoch)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS markets (
    symbol TEXT PRIMARY KEY,
    display_name TEXT, market TEXT, submarket TEXT,
    pip_decimals INTEGER,
    exchange_is_open INTEGER, is_trading_suspended INTEGER,
    first_seen INTEGER, last_seen INTEGER,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS contracts (
    symbol TEXT NOT NULL, contract_type TEXT NOT NULL,
    min_ticks INTEGER, max_ticks INTEGER, digit_range TEXT,
    updated_at INTEGER,
    PRIMARY KEY (symbol, contract_type)
);

CREATE TABLE IF NOT EXISTS compatibility (
    bot TEXT NOT NULL, symbol TEXT NOT NULL,
    compatible INTEGER NOT NULL, reasons TEXT, updated_at INTEGER,
    PRIMARY KEY (bot, symbol)
);

CREATE TABLE IF NOT EXISTS payouts (
    symbol TEXT NOT NULL, contract_type TEXT NOT NULL, barrier TEXT NOT NULL, duration INTEGER NOT NULL,
    stake TEXT, payout TEXT, break_even REAL, fetched_at INTEGER, error TEXT,
    PRIMARY KEY (symbol, contract_type, barrier, duration)
);

CREATE TABLE IF NOT EXISTS signal_state (
    bot TEXT NOT NULL, symbol TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    pass_streak INTEGER NOT NULL DEFAULT 0,
    fail_streak INTEGER NOT NULL DEFAULT 0,
    last_status TEXT, last_change_at INTEGER, last_start_at INTEGER, last_stop_at INTEGER,
    updated_at INTEGER,
    PRIMARY KEY (bot, symbol)
);

CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot TEXT, symbol TEXT, kind TEXT, reason TEXT, created_at INTEGER, details TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | skipped | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at INTEGER, next_attempt_at INTEGER, last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status, id);

CREATE TABLE IF NOT EXISTS evaluations (
    bot TEXT NOT NULL, symbol TEXT NOT NULL,
    status TEXT, evaluated_at INTEGER, summary TEXT,
    PRIMARY KEY (bot, symbol)
);

CREATE TABLE IF NOT EXISTS incidents (
    key TEXT PRIMARY KEY, kind TEXT, symbol TEXT,
    opened_at INTEGER, closed_at INTEGER, detail TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30,
                               check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    return conn

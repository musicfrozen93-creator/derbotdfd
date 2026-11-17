"""SQLite (WAL mode) storage.

Every multi-statement (and every standalone) write goes through :func:`transaction`, which
guarantees that a failed write is rolled back, logged and counted, and that the connection is
never left inside an open transaction (an open transaction makes every later ``BEGIN`` fail with
"cannot start a transaction within a transaction", i.e. all later writes would fail).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.monitoring.logger import event

log = logging.getLogger("storage.db")

SCHEMA_VERSION = 2   # 2: contract_results (per-contract PASS / FAIL)

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

-- latest per-contract result of the signal rule, one row per (bot, market, contract);
-- replaced every evaluation cycle together with ``evaluations``
CREATE TABLE IF NOT EXISTS contract_results (
    bot TEXT NOT NULL, symbol TEXT NOT NULL, leg_id TEXT NOT NULL,
    contract_type TEXT, barrier INTEGER, duration INTEGER, label TEXT,
    winning_digits TEXT,          -- e.g. "0,1,2,3,4"
    digit_pct TEXT,               -- JSON {"0": 10.4, ...} for this contract's winning digits
    failing_digits TEXT,          -- e.g. "3,4" (empty when the contract passes)
    result TEXT,                  -- PASS | FAIL | NOT_CHECKED | UNAVAILABLE
    detail TEXT,
    bot_status TEXT, evaluated_at INTEGER,
    PRIMARY KEY (bot, symbol, leg_id)
);

CREATE TABLE IF NOT EXISTS incidents (
    key TEXT PRIMARY KEY, kind TEXT, symbol TEXT,
    opened_at INTEGER, closed_at INTEGER, detail TEXT
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class AppConnection(sqlite3.Connection):
    """sqlite3 connection that also keeps write-health counters (published in the heartbeat)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.failed_writes = 0               # transactions rolled back because a write failed
        self.consecutive_failed_writes = 0   # reset by the next successful commit
        self.last_write_error: str | None = None
        self.last_write_error_at: int | None = None
        self.last_commit_at: int | None = None
        self.txn_owner: str | None = None    # name of the transaction() block currently open


def write_stats(conn) -> dict:
    """Write-health counters of a connection (zeros for a plain sqlite3 connection)."""
    return {"failed_transactions": getattr(conn, "failed_writes", 0),
            "consecutive_failed": getattr(conn, "consecutive_failed_writes", 0),
            "last_error": getattr(conn, "last_write_error", None),
            "last_error_at": getattr(conn, "last_write_error_at", None),
            "last_commit_at": getattr(conn, "last_commit_at", None)}


def _set(conn, name: str, value) -> None:
    try:
        setattr(conn, name, value)
    except AttributeError:      # plain sqlite3.Connection (e.g. a test's own connection)
        pass


def _rollback(conn, what: str) -> None:
    """Roll back if a transaction is open. Never raises: the caller re-raises the original error."""
    if not conn.in_transaction:
        return                  # BEGIN never ran, or SQLite already rolled back (disk full, I/O error)
    try:
        conn.execute("ROLLBACK")
    except Exception as exc:    # noqa: BLE001 - must not mask the original exception
        event(log, "db_rollback_failed", logging.CRITICAL, what=what, error=f"{type(exc).__name__}: {exc}"[:300])


@contextmanager
def transaction(conn, what: str):
    """``with transaction(conn, "save_evaluations"): ...`` - one atomic write.

    * ``BEGIN`` ... ``COMMIT``; if any statement, the ``COMMIT`` itself (e.g. SQLITE_BUSY) or the
      caller's own code raises: ``ROLLBACK``, log ``event=db_write_failed`` (with traceback), count
      it on the connection and re-raise the ORIGINAL exception (never swallowed, never replaced by
      a rollback error).
    * The connection is never left inside an open transaction, so the next write works.
    * A transaction found open on entry (left behind by code that bypassed this helper) is rolled
      back and logged instead of making every later write fail.
    * Nesting is a programming error (an inner COMMIT would commit the outer work): RuntimeError.
    """
    owner = getattr(conn, "txn_owner", None)
    if owner is not None:
        raise RuntimeError(f"nested database transaction: {what!r} inside {owner!r}")
    if conn.in_transaction:
        event(log, "db_stale_transaction_rolled_back", logging.ERROR, what=what)
        _rollback(conn, what)
    _set(conn, "txn_owner", what)
    try:
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException as exc:
            _rollback(conn, what)
            _set(conn, "failed_writes", getattr(conn, "failed_writes", 0) + 1)
            _set(conn, "consecutive_failed_writes", getattr(conn, "consecutive_failed_writes", 0) + 1)
            _set(conn, "last_write_error", f"{what}: {type(exc).__name__}: {exc}"[:300])
            _set(conn, "last_write_error_at", int(time.time()))
            event(log, "db_write_failed", logging.ERROR, exc_info=exc, what=what,
                  error=f"{type(exc).__name__}: {exc}"[:300], rolled_back=not conn.in_transaction)
            raise
    finally:
        _set(conn, "txn_owner", None)
    _set(conn, "consecutive_failed_writes", 0)
    _set(conn, "last_commit_at", int(time.time()))


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30,
                               check_same_thread=False, factory=AppConnection)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False,
                               factory=AppConnection)
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

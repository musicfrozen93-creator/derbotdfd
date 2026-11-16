"""Data-access helpers. Every function takes an open sqlite3 connection."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Iterable

import numpy as np

from app.market.precision import scaled_int


def now_i() -> int:
    return int(time.time())


# ----------------------------------------------------------------- ticks
def insert_ticks(conn: sqlite3.Connection, rows: Iterable[tuple[str, int, str, int]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    before = conn.total_changes
    conn.execute("BEGIN")
    try:
        conn.executemany("INSERT OR IGNORE INTO ticks(symbol, epoch, quote, digit) VALUES (?,?,?,?)", rows)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return conn.total_changes - before


def get_tick(conn, symbol: str, epoch: int):
    return conn.execute("SELECT quote, digit FROM ticks WHERE symbol=? AND epoch=?", (symbol, epoch)).fetchone()


def tick_bounds(conn, symbol: str) -> tuple[int | None, int | None, int]:
    row = conn.execute("SELECT MIN(epoch), MAX(epoch), COUNT(*) FROM ticks WHERE symbol=?", (symbol,)).fetchone()
    return row[0], row[1], int(row[2] or 0)


def last_epoch(conn, symbol: str) -> int | None:
    row = conn.execute("SELECT MAX(epoch) FROM ticks WHERE symbol=?", (symbol,)).fetchone()
    return row[0]


def median_interval(conn, symbol: str, sample: int = 201) -> float | None:
    """Median spacing (s) of the newest stored ticks, or None with too little data."""
    rows = conn.execute("SELECT epoch FROM ticks WHERE symbol=? ORDER BY epoch DESC LIMIT ?",
                        (symbol, sample)).fetchall()
    if len(rows) < 6:
        return None
    d = -np.diff(np.array([r[0] for r in rows], dtype=np.int64))
    d = d[d > 0]
    return float(np.median(d)) if d.size else None


def epochs_between(conn, symbol: str, start: int, end: int) -> np.ndarray:
    cur = conn.execute("SELECT epoch FROM ticks WHERE symbol=? AND epoch>=? AND epoch<=? ORDER BY epoch",
                       (symbol, start, end))
    return np.fromiter((r[0] for r in cur), dtype=np.int64)


def load_series_rows(conn, symbol: str, max_ticks: int, after_epoch: int | None = None):
    """Return (epochs, digits, prices_int) ascending, the newest ``max_ticks`` rows
    (or only rows newer than ``after_epoch``)."""
    if after_epoch is None:
        cur = conn.execute(
            "SELECT epoch, digit, quote FROM (SELECT epoch, digit, quote FROM ticks WHERE symbol=? "
            "ORDER BY epoch DESC LIMIT ?) ORDER BY epoch", (symbol, int(max_ticks)))
    else:
        cur = conn.execute("SELECT epoch, digit, quote FROM ticks WHERE symbol=? AND epoch>? ORDER BY epoch",
                           (symbol, int(after_epoch)))
    # Read in chunks so a 400k-row load never materialises 400k Row objects at once.
    ep_parts, dg_parts, pr_parts = [], [], []
    while True:
        rows = cur.fetchmany(20000)
        if not rows:
            break
        ep_parts.append(np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows)))
        dg_parts.append(np.fromiter((r[1] for r in rows), dtype=np.int8, count=len(rows)))
        pr_parts.append(np.fromiter((scaled_int(r[2]) for r in rows), dtype=np.int64, count=len(rows)))
    if not ep_parts:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int8), np.zeros(0, dtype=np.int64)
    return np.concatenate(ep_parts), np.concatenate(dg_parts), np.concatenate(pr_parts)


def prune_ticks(conn, before_epoch: int) -> int:
    before = conn.total_changes
    conn.execute("DELETE FROM ticks WHERE epoch < ?", (before_epoch,))
    return conn.total_changes - before


# --------------------------------------------------------------- markets
def upsert_markets(conn, markets) -> None:
    ts = now_i()
    conn.execute("BEGIN")
    for m in markets:
        conn.execute(
            "INSERT INTO markets(symbol, display_name, market, submarket, pip_decimals, exchange_is_open,"
            " is_trading_suspended, first_seen, last_seen, active) VALUES (?,?,?,?,?,?,?,?,?,1)"
            " ON CONFLICT(symbol) DO UPDATE SET display_name=excluded.display_name, market=excluded.market,"
            " submarket=excluded.submarket, pip_decimals=excluded.pip_decimals,"
            " exchange_is_open=excluded.exchange_is_open, is_trading_suspended=excluded.is_trading_suspended,"
            " last_seen=excluded.last_seen, active=1",
            (m.symbol, m.display_name, m.market, m.submarket, m.pip_decimals, int(m.exchange_is_open),
             int(m.is_trading_suspended), ts, ts))
    conn.execute("COMMIT")


def deactivate_markets(conn, symbols: Iterable[str]) -> None:
    for s in symbols:
        conn.execute("UPDATE markets SET active=0 WHERE symbol=?", (s,))


def load_markets(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM markets ORDER BY symbol").fetchall()


def replace_contracts(conn, symbol: str, contracts) -> None:
    ts = now_i()
    conn.execute("BEGIN")
    conn.execute("DELETE FROM contracts WHERE symbol=?", (symbol,))
    for c in contracts.contracts.values():
        conn.execute("INSERT INTO contracts(symbol, contract_type, min_ticks, max_ticks, digit_range, updated_at)"
                     " VALUES (?,?,?,?,?,?)",
                     (symbol, c.contract_type, c.min_ticks, c.max_ticks,
                      json.dumps(c.digit_range) if c.digit_range is not None else None, ts))
    conn.execute("COMMIT")


def load_contracts(conn, symbol: str):
    """Last successful contracts_for listing stored for a symbol (None if never stored)."""
    from app.deriv.contracts import DigitContract, SymbolContracts
    rows = conn.execute("SELECT * FROM contracts WHERE symbol=?", (symbol,)).fetchall()
    if not rows:
        return None
    out = SymbolContracts(symbol=symbol)
    for r in rows:
        rng = json.loads(r["digit_range"]) if r["digit_range"] else None
        out.contracts[r["contract_type"]] = DigitContract(r["contract_type"], int(r["min_ticks"]),
                                                          int(r["max_ticks"]), rng)
    return out


def save_compatibility(conn, rows: list[tuple[str, str, bool, str]]) -> None:
    ts = now_i()
    conn.execute("BEGIN")
    conn.execute("DELETE FROM compatibility")
    conn.executemany("INSERT INTO compatibility(bot, symbol, compatible, reasons, updated_at) VALUES (?,?,?,?,?)",
                     [(b, s, int(ok), r, ts) for b, s, ok, r in rows])
    conn.execute("COMMIT")


# ---------------------------------------------------------------- payouts
def save_payout(conn, symbol, contract_type, barrier, duration, stake, payout, break_even, error=None) -> None:
    conn.execute(
        "INSERT INTO payouts(symbol, contract_type, barrier, duration, stake, payout, break_even, fetched_at, error)"
        " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol, contract_type, barrier, duration) DO UPDATE SET"
        " stake=excluded.stake, payout=excluded.payout, break_even=excluded.break_even,"
        " fetched_at=excluded.fetched_at, error=excluded.error",
        (symbol, contract_type, str(barrier), int(duration), None if stake is None else str(stake),
         None if payout is None else str(payout), break_even, now_i(), error))


def save_payout_error(conn, symbol, contract_type, barrier, duration, error: str) -> None:
    """Record an error without discarding a previously fetched valid payout."""
    row = conn.execute("SELECT 1 FROM payouts WHERE symbol=? AND contract_type=? AND barrier=? AND duration=?",
                       (symbol, contract_type, str(barrier), int(duration))).fetchone()
    if row:
        conn.execute("UPDATE payouts SET error=? WHERE symbol=? AND contract_type=? AND barrier=? AND duration=?",
                     (error, symbol, contract_type, str(barrier), int(duration)))
    else:
        save_payout(conn, symbol, contract_type, barrier, duration, None, None, None, error)


def load_payouts(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM payouts").fetchall()


# ------------------------------------------------------------ evaluations
def save_evaluations(conn, rows: list[tuple[str, str, str, dict]]) -> None:
    ts = now_i()
    conn.execute("BEGIN")
    conn.execute("DELETE FROM evaluations")
    conn.executemany("INSERT INTO evaluations(bot, symbol, status, evaluated_at, summary) VALUES (?,?,?,?,?)",
                     [(b, s, st, ts, json.dumps(summary, default=str)) for b, s, st, summary in rows])
    conn.execute("COMMIT")


# -------------------------------------------------------------- meta / kv
def meta_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


# -------------------------------------------------------------- incidents
def open_incident(conn, key: str, kind: str, symbol: str | None, detail: str) -> bool:
    """Open an incident. Returns True if it was not already open."""
    row = conn.execute("SELECT closed_at FROM incidents WHERE key=?", (key,)).fetchone()
    if row is not None and row[0] is None:
        return False
    conn.execute("INSERT INTO incidents(key, kind, symbol, opened_at, closed_at, detail) VALUES (?,?,?,?,NULL,?)"
                 " ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, symbol=excluded.symbol,"
                 " opened_at=excluded.opened_at, closed_at=NULL, detail=excluded.detail",
                 (key, kind, symbol, now_i(), detail))
    return True


def close_incident(conn, key: str) -> tuple[bool, int | None]:
    """Close an open incident. Returns (was_open, opened_at)."""
    row = conn.execute("SELECT opened_at, closed_at FROM incidents WHERE key=?", (key,)).fetchone()
    if row is None or row[1] is not None:
        return False, None
    conn.execute("UPDATE incidents SET closed_at=? WHERE key=?", (now_i(), key))
    return True, row[0]


def open_incidents(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM incidents WHERE closed_at IS NULL").fetchall()

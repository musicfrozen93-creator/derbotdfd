"""Data-access helpers. Every function takes an open sqlite3 connection."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Iterable

import numpy as np

from app.market.precision import scaled_int
from app.storage.database import transaction


def now_i() -> int:
    return int(time.time())


# ----------------------------------------------------------------- ticks
def insert_ticks(conn: sqlite3.Connection, rows: Iterable[tuple[str, int, str, int]]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    before = conn.total_changes
    with transaction(conn, "insert_ticks"):
        conn.executemany("INSERT OR IGNORE INTO ticks(symbol, epoch, quote, digit) VALUES (?,?,?,?)", rows)
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
    with transaction(conn, "prune_ticks"):
        conn.execute("DELETE FROM ticks WHERE epoch < ?", (before_epoch,))
    return conn.total_changes - before


# --------------------------------------------------------------- markets
def upsert_markets(conn, markets) -> None:
    ts = now_i()
    with transaction(conn, "upsert_markets"):
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


def active_market_symbols(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT symbol FROM markets WHERE active=1")]


def deactivate_markets(conn, symbols: Iterable[str]) -> None:
    with transaction(conn, "deactivate_markets"):
        for s in symbols:
            conn.execute("UPDATE markets SET active=0 WHERE symbol=?", (s,))


def load_markets(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM markets ORDER BY symbol").fetchall()


def replace_contracts(conn, symbol: str, contracts) -> None:
    ts = now_i()
    with transaction(conn, "replace_contracts"):
        conn.execute("DELETE FROM contracts WHERE symbol=?", (symbol,))
        for c in contracts.contracts.values():
            conn.execute("INSERT INTO contracts(symbol, contract_type, min_ticks, max_ticks, digit_range,"
                         " updated_at) VALUES (?,?,?,?,?,?)",
                         (symbol, c.contract_type, c.min_ticks, c.max_ticks,
                          json.dumps(c.digit_range) if c.digit_range is not None else None, ts))


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
    with transaction(conn, "save_compatibility"):
        conn.execute("DELETE FROM compatibility")
        conn.executemany("INSERT INTO compatibility(bot, symbol, compatible, reasons, updated_at)"
                         " VALUES (?,?,?,?,?)", [(b, s, int(ok), r, ts) for b, s, ok, r in rows])


# ------------------------------------------------------------ evaluations
def save_evaluations(conn, rows: list[tuple[str, str, str, dict]], contract_rows: list[tuple] | None = None) -> None:
    """Replace the latest evaluation of every combination and, in the same transaction, the
    per-contract results. ``contract_rows``: (bot, symbol, bot_status, ContractResult).

    All or nothing: if any write fails, the previous cycle's evaluations AND contract results
    are kept unchanged (rollback) and the original exception is raised."""
    ts = now_i()
    eval_rows = [(b, s, st, ts, json.dumps(summary, default=str)) for b, s, st, summary in rows]
    result_rows = [(b, s, c.leg_id, c.contract_type, c.barrier, c.duration, c.label,
                    ",".join(map(str, c.winning_digits)),
                    json.dumps({str(d): round(p, 4) for d, p in c.digit_pct.items()}),
                    ",".join(map(str, c.failing_digits)), c.result, c.detail, st, ts)
                   for b, s, st, c in (contract_rows or [])]
    with transaction(conn, "save_evaluations"):
        conn.execute("DELETE FROM evaluations")
        conn.executemany("INSERT INTO evaluations(bot, symbol, status, evaluated_at, summary) VALUES (?,?,?,?,?)",
                         eval_rows)
        conn.execute("DELETE FROM contract_results")
        conn.executemany(
            "INSERT INTO contract_results(bot, symbol, leg_id, contract_type, barrier, duration, label,"
            " winning_digits, digit_pct, failing_digits, result, detail, bot_status, evaluated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", result_rows)


def save_strategy_cycle(conn, verdicts: list, last_tuples: dict) -> dict:
    """Write one evaluation cycle of bot-specific verdicts (StrategyVerdict) atomically:

    * ``evaluations`` (status + full summary JSON) and ``contract_results`` (the legacy digit rule per
      leg, marked informational) - the tables older tools read;
    * ``strategy_evaluations`` and ``leg_evaluations`` (latest per combination / leg);
    * ``evaluation_history``: one row per combination whose (decision, entry, statistics, economics,
      risk) tuple differs from ``last_tuples``.

    All or nothing: on any error the previous cycle's rows stay and the exception propagates. Returns
    the new tuple map; the caller adopts it only after this returns (so a failed write is retried)."""
    ts = now_i()
    eval_rows, legacy_rows, strat_rows, leg_rows, hist_rows = [], [], [], [], []
    new_tuples = dict(last_tuples)
    for v in verdicts:
        eval_rows.append((v.bot, v.symbol, v.status, ts, json.dumps(v.summary(), default=str)))
        reasons = json.dumps(v.reasons)
        strat_rows.append((v.bot, v.symbol, v.status, v.entry_status, v.statistics_status, v.economics_status,
                           v.risk_status, v.critical_kind, reasons, json.dumps(v.gates, default=str), ts))
        tup = v.status_tuple()
        if last_tuples.get((v.bot, v.symbol)) != tup:
            hist_rows.append((v.bot, v.symbol, ts, *tup[:5], reasons))
            new_tuples[(v.bot, v.symbol)] = tup
        for lg in v.legs:
            lr = lg.legacy_rule or {}
            legacy_rows.append((v.bot, v.symbol, lg.leg_id, lg.contract_type, lg.barrier, lg.duration,
                                f"{lg.contract_type}{'' if lg.barrier is None else ' ' + str(lg.barrier)}",
                                ",".join(map(str, lr.get("winning_digits", []))),
                                json.dumps(lr.get("digit_pct", {})), ",".join(map(str, lr.get("failing_digits", []))),
                                lr.get("result", "NOT_CHECKED"), "legacy digit rule (informational, not a gate)",
                                v.status, ts))
            e, lo = lg.economics, lg.long
            leg_rows.append((v.bot, v.symbol, lg.leg_id, lg.label, lg.used_when, f"{lg.stake:.2f}",
                             lg.recent.triggers if lg.recent else None, lg.recent.settled if lg.recent else None,
                             lg.recent.wins if lg.recent else None, lo.settled if lo else None,
                             lo.wins if lo else None, lo.win_rate if lo else None, lo.ci_low if lo else None,
                             lo.ci_high if lo else None, lo.p_fair if lo else None, lg.statistics_status,
                             None if e is None or e.payout is None else f"{e.payout:.2f}",
                             None if e is None else e.break_even, None if e is None else e.ev_fair,
                             None if e is None else e.ev_low, None if e is None else e.ev_high,
                             None if e is None else e.status, json.dumps(lr), ts))
    with transaction(conn, "save_strategy_cycle"):
        conn.execute("DELETE FROM evaluations")
        conn.executemany("INSERT INTO evaluations(bot, symbol, status, evaluated_at, summary) VALUES (?,?,?,?,?)",
                         eval_rows)
        conn.execute("DELETE FROM contract_results")
        conn.executemany(
            "INSERT INTO contract_results(bot, symbol, leg_id, contract_type, barrier, duration, label,"
            " winning_digits, digit_pct, failing_digits, result, detail, bot_status, evaluated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", legacy_rows)
        conn.execute("DELETE FROM strategy_evaluations")
        conn.executemany(
            "INSERT INTO strategy_evaluations(bot, symbol, decision, entry_status, statistics_status, economics_status,"
            " risk_status, critical_kind, reasons, detail, evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", strat_rows)
        conn.execute("DELETE FROM leg_evaluations")
        conn.executemany(
            "INSERT INTO leg_evaluations(bot, symbol, leg_id, contract, used_when, stake, triggers_recent,"
            " settled_recent, wins_recent, settled_long, wins_long, win_rate_long, ci_low, ci_high, p_fair,"
            " statistics_status, payout, break_even, ev_fair, ev_low, ev_high, economics_status, legacy_rule,"
            " evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", leg_rows)
        conn.executemany(
            "INSERT INTO evaluation_history(bot, symbol, evaluated_at, decision, entry_status, statistics_status,"
            " economics_status, risk_status, reasons) VALUES (?,?,?,?,?,?,?,?,?)", hist_rows)
    return new_tuples


def load_last_history(conn) -> dict:
    """Latest (decision, entry, statistics, economics, risk) per combination from evaluation_history."""
    out = {}
    try:
        rows = conn.execute("SELECT h.* FROM evaluation_history h JOIN (SELECT bot, symbol, MAX(id) mid FROM"
                            " evaluation_history GROUP BY bot, symbol) x ON h.id = x.mid").fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        out[(r["bot"], r["symbol"])] = (r["decision"], r["entry_status"], r["statistics_status"],
                                        r["economics_status"], r["risk_status"])
    return out


def load_contract_results(conn, bot: str | None = None, symbol: str | None = None) -> list:
    q = "SELECT * FROM contract_results WHERE 1=1"
    params: list = []
    if bot:
        q += " AND bot=?"
        params.append(bot)
    if symbol:
        q += " AND symbol=?"
        params.append(symbol)
    return conn.execute(q + " ORDER BY bot, symbol, rowid", params).fetchall()


# -------------------------------------------------------------- meta / kv
def meta_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn, key: str, value: str) -> None:
    with transaction(conn, f"meta_set:{key}"):
        conn.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))


# -------------------------------------------------------------- incidents
def open_incident(conn, key: str, kind: str, symbol: str | None, detail: str) -> bool:
    """Open an incident. Returns True if it was not already open."""
    with transaction(conn, "open_incident"):
        row = conn.execute("SELECT closed_at FROM incidents WHERE key=?", (key,)).fetchone()
        if row is not None and row[0] is None:
            return False
        conn.execute("INSERT INTO incidents(key, kind, symbol, opened_at, closed_at, detail)"
                     " VALUES (?,?,?,?,NULL,?)"
                     " ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, symbol=excluded.symbol,"
                     " opened_at=excluded.opened_at, closed_at=NULL, detail=excluded.detail",
                     (key, kind, symbol, now_i(), detail))
    return True


def close_incident(conn, key: str) -> tuple[bool, int | None]:
    """Close an open incident. Returns (was_open, opened_at)."""
    with transaction(conn, "close_incident"):
        row = conn.execute("SELECT opened_at, closed_at FROM incidents WHERE key=?", (key,)).fetchone()
        if row is None or row[1] is not None:
            return False, None
        conn.execute("UPDATE incidents SET closed_at=? WHERE key=?", (now_i(), key))
    return True, row[0]


def open_incidents(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM incidents WHERE closed_at IS NULL").fetchall()

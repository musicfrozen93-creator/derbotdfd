"""Persistent per (bot, market) signal state."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class SignalState:
    bot: str
    symbol: str
    active: bool = False
    pass_streak: int = 0
    fail_streak: int = 0
    last_status: str | None = None
    last_change_at: int | None = None
    last_start_at: int | None = None
    last_stop_at: int | None = None


class SignalStateStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.states: dict[tuple[str, str], SignalState] = {}
        self.reload()

    def reload(self) -> None:
        self.states = {}
        for r in self.conn.execute("SELECT * FROM signal_state").fetchall():
            s = SignalState(r["bot"], r["symbol"], bool(r["active"]), int(r["pass_streak"] or 0),
                            int(r["fail_streak"] or 0), r["last_status"], r["last_change_at"],
                            r["last_start_at"], r["last_stop_at"])
            self.states[(s.bot, s.symbol)] = s

    def get(self, bot: str, symbol: str) -> SignalState:
        key = (bot, symbol)
        s = self.states.get(key)
        if s is None:
            s = self.states[key] = SignalState(bot, symbol)
        return s

    def active(self) -> list[SignalState]:
        return [s for s in self.states.values() if s.active]

    def save(self, s: SignalState, now: int) -> None:
        """Write one state row (caller controls the transaction)."""
        self.conn.execute(
            "INSERT INTO signal_state(bot, symbol, active, pass_streak, fail_streak, last_status, last_change_at,"
            " last_start_at, last_stop_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(bot, symbol) DO UPDATE SET active=excluded.active, pass_streak=excluded.pass_streak,"
            " fail_streak=excluded.fail_streak, last_status=excluded.last_status,"
            " last_change_at=excluded.last_change_at, last_start_at=excluded.last_start_at,"
            " last_stop_at=excluded.last_stop_at, updated_at=excluded.updated_at",
            (s.bot, s.symbol, int(s.active), s.pass_streak, s.fail_streak, s.last_status, s.last_change_at,
             s.last_start_at, s.last_stop_at, now))

"""START / STOP state machine.

Rules
* one START when a (bot, market) combination becomes a confirmed RUN_CANDIDATE
* one STOP when an active combination stops qualifying
* never a WAIT message, never a repeated START/STOP
* every combination has its own independent state
* START needs ``start_confirm_cycles`` consecutive RUN_CANDIDATE evaluations and
  the restart cooldown to have elapsed since the last STOP
* STOP needs ``stop_confirm_cycles`` consecutive failing evaluations, except a
  critical fault, which stops immediately: ``critical_kind`` "contract" (contract
  no longer offered), "data" (feed down beyond the critical threshold) or "risk"
  (the bot's recovery sequence is UNSAFE or its next trade is BLOCKED, see
  ``app.risk.engine``)
* a pure data-quality dip (window not clean, but not a critical fault) freezes
  the counters instead of stopping an active signal
* during the start-up grace period, data faults on active signals are frozen
  too (the feed and back-fill are still catching up after a restart); contract
  and risk faults are never frozen
* the restart cooldown and the per-bot START limit only ever delay a START, never a STOP

Verdicts are :class:`app.strategy_model.evaluate.StrategyVerdict` objects (bot-specific gates
entry / statistics / economics / risk). The engine only reads ``status``, ``hold``, ``critical``,
``critical_kind``, ``reasons``, ``bot``, ``symbol`` and ``summary()``, so the older
:class:`app.analytics.validation.BotVerdict` works too.

State changes and their notifications are written in one SQLite transaction
(outbox pattern), so a restart never loses or duplicates a transition. If that transaction
fails it is rolled back, the in-memory state is restored to the last committed state and the
original exception is raised: a failed write can never leave a START / STOP half recorded.

``process(..., starts_allowed=False)`` is used for a cycle whose evaluation results could not be
saved: STOP rules run unchanged (an active signal is still stopped), but such a cycle gives no
START credit (its RUN_CANDIDATE results reset the pass streak), so a START always rests on
``start_confirm_cycles`` consecutive evaluations that were also stored.

"Consecutive" means consecutive evaluation cycles of the whole service, not just consecutive
verdicts for one combination: an inactive combination that is missing from a cycle's verdicts
loses its pass streak in that cycle, and a cycle that fails before producing verdicts calls
``discard_start_credit`` (pass streak of every inactive combination reset).

Per-bot START limit (``signals.max_starts_per_bot`` in any rolling ``signals.start_limit_window_hours``,
default 5 per 12 h, all markets of the bot together): every START actually issued is written to
``signal_start_ledger`` in the same transaction as the START itself. Before a START, the bot's
ledger rows with ``started_at > now - window`` are counted; at the limit the START is not sent,
the combination's status becomes ``SIGNAL_LIMIT_REACHED`` (logged once per episode, and one
``signal_events`` row of that kind) and its pass streak is kept, so it STARTs on the first check
after an older START has left the window - if it is still a RUN_CANDIDATE with
``start_confirm_cycles`` consecutive passes and out of its cooldown. STOP is never limited. If the
ledger cannot be read or written the START is not sent (fail closed) and the rest of the cycle,
STOPs included, carries on.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, replace

from typing import TYPE_CHECKING

from app.analytics.validation import RUN, BotVerdict
from app.monitoring.logger import event

if TYPE_CHECKING:  # pragma: no cover
    from app.strategy_model.evaluate import StrategyVerdict
from app.signals.state import SignalStateStore
from app.storage.database import transaction

log = logging.getLogger("signals.engine")

LIMIT_REACHED = "SIGNAL_LIMIT_REACHED"


@dataclass(frozen=True)
class StartLimit:
    max_starts: int          # <= 0: no limit
    window_s: int

    @classmethod
    def from_settings(cls, cfg) -> "StartLimit":
        n = int(getattr(cfg, "max_starts_per_bot", 5))
        hours = float(getattr(cfg, "start_limit_window_hours", 12))
        if n < 0:
            raise ValueError("signals.max_starts_per_bot must be 0 (no limit) or more")
        if n and not 0 < hours <= 24 * 30:
            raise ValueError("signals.start_limit_window_hours must be above 0 and at most 720")
        return cls(n, int(round(hours * 3600)))

    @property
    def enabled(self) -> bool:
        return self.max_starts > 0

    def text(self) -> str:
        return (f"max {self.max_starts} START per bot in any rolling {self.window_s / 3600:g} h"
                if self.enabled else "no per-bot START limit")


@dataclass
class Transition:
    kind: str      # START | STOP
    bot: str
    symbol: str
    reason: str
    critical: bool = False


class SignalEngine:
    def __init__(self, conn, store: SignalStateStore, formatter, cfg, *, started_at: float | None = None) -> None:
        self.conn = conn
        self.store = store
        self.fmt = formatter
        self.cfg = cfg
        self.started_at = started_at or time.time()
        self.limit = StartLimit.from_settings(cfg)

    def in_grace(self, now: float) -> bool:
        return now - self.started_at < float(self.cfg.startup_grace_s)

    def on_startup(self, last_alive: float | None, now: float, max_offline_s: float) -> list[Transition]:
        """Called once when the process starts, before the first evaluation.

        * Confirmation counters only count *consecutive* evaluations, so counts left over from
          before the restart are cleared (the active / inactive flag itself is kept).
        * If the analyzer itself was offline for ``max_offline_s`` or longer, active signals were
          not monitored during that time. That is treated like a feed outage of the same length
          (critical STOP), so a combination never stays "active" across an unmonitored gap."""
        now_i = int(now)
        offline = None if last_alive is None else now - float(last_alive)
        out: list[Transition] = []
        snapshot = self._snapshot()
        try:
            with transaction(self.conn, "signal_engine.on_startup"):
                self._startup_body(offline, max_offline_s, now, now_i, out)
        except Exception:
            self._restore(snapshot)
            raise
        for t in out:
            event(log, "signal_stop", logging.WARNING, bot=t.bot, symbol=t.symbol, reason=t.reason, critical=True)
        return out

    def _snapshot(self) -> dict:
        return {k: replace(s) for k, s in self.store.states.items()}

    def _restore(self, snapshot: dict) -> None:
        """Memory back to the last committed state (does not touch the possibly failing database)."""
        self.store.states = snapshot
        event(log, "signal_state_restored", logging.WARNING, combinations=len(snapshot))

    def _startup_body(self, offline, max_offline_s, now, now_i, out) -> None:
        for s in list(self.store.states.values()):
            if s.pass_streak or s.fail_streak:
                s.pass_streak = s.fail_streak = 0
                self.store.save(s, now_i)
        if offline is not None and offline >= max_offline_s:
            for s in list(self.store.active()):
                reason = (f"the analyzer was offline for {offline / 60:.1f} min "
                          f"(restart / server down), so the conditions were not monitored")
                s.active = False
                s.last_stop_at = s.last_change_at = now_i
                s.last_status = "UNMONITORED"
                t = Transition("STOP", s.bot, s.symbol, reason, critical=True)
                self._record(t, self.fmt.stop_unmonitored(s.bot, s.symbol, reason, now), now_i, None)
                self.store.save(s, now_i)
                out.append(t)

    def process(self, verdicts: dict[tuple[str, str], "BotVerdict | StrategyVerdict"], now: float | None = None,
                missing_reason: dict[tuple[str, str], str] | None = None, *,
                starts_allowed: bool = True) -> list[Transition]:
        now = now or time.time()
        now_i = int(now)
        start_n = int(self.cfg.start_confirm_cycles)
        stop_n = int(self.cfg.stop_confirm_cycles)
        cooldown = float(self.cfg.restart_cooldown_minutes) * 60
        grace = self.in_grace(now)
        out: list[Transition] = []

        snapshot = self._snapshot()
        try:
            with transaction(self.conn, "signal_engine.process"):
                self._process_body(verdicts, now, now_i, start_n, stop_n, cooldown, grace, missing_reason,
                                   starts_allowed, out)
        except Exception:
            self._restore(snapshot)
            raise
        for t in out:
            event(log, f"signal_{t.kind.lower()}", bot=t.bot, symbol=t.symbol, reason=t.reason, critical=t.critical)
        return out

    def _process_body(self, verdicts, now, now_i, start_n, stop_n, cooldown, grace, missing_reason,
                      starts_allowed, out) -> None:
        for key, v in verdicts.items():
            s = self.store.get(*key)
            previous_status = s.last_status
            s.last_status = v.status
            if v.status == RUN:
                # an unsaved cycle gives no START credit (see module docstring)
                s.pass_streak = s.pass_streak + 1 if starts_allowed else 0
                s.fail_streak = 0
            elif v.hold or (grace and v.critical_kind == "data"):
                s.pass_streak = 0          # cannot START on unclean data, but do not count toward STOP
            else:
                s.pass_streak = 0
                s.fail_streak += 1
            if not s.active:
                cooled = s.last_stop_at is None or now - s.last_stop_at >= cooldown
                # the per-bot limit is checked last, so only a START that is otherwise due uses a slot
                if (v.status == RUN and s.pass_streak >= start_n and cooled
                        and not self._limit_blocks(v, s, now_i, previous_status)):
                    s.active = True
                    s.last_start_at = s.last_change_at = now_i
                    s.fail_streak = 0
                    t = Transition("START", v.bot, v.symbol, "validated conditions confirmed")
                    self._record(t, self.fmt.start(v, now), now_i, v)
                    out.append(t)
            else:
                critical_now = v.critical and not (grace and v.critical_kind == "data")
                if v.status != RUN and (critical_now or s.fail_streak >= stop_n):
                    reason = "; ".join(v.reasons[:3]) or v.status
                    s.active = False
                    s.last_stop_at = s.last_change_at = now_i
                    s.pass_streak = 0
                    t = Transition("STOP", v.bot, v.symbol, reason, critical=critical_now)
                    self._record(t, self.fmt.stop(v, now, critical=critical_now), now_i, v)
                    out.append(t)
            self.store.save(s, now_i)
        # active combinations that are no longer evaluated at all (symbol delisted, bot disabled ...)
        for s in list(self.store.active()):
            key = (s.bot, s.symbol)
            if key in verdicts:
                continue
            reason = (missing_reason or {}).get(key, "combination is no longer monitored "
                                                     "(market or contract no longer available)")
            s.active = False
            s.last_stop_at = s.last_change_at = now_i
            s.pass_streak = s.fail_streak = 0
            s.last_status = "UNMONITORED"
            t = Transition("STOP", s.bot, s.symbol, reason, critical=True)
            self._record(t, self.fmt.stop_unmonitored(s.bot, s.symbol, reason, now), now_i, None)
            self.store.save(s, now_i)
            out.append(t)
        # inactive combinations that were not evaluated in this cycle (contract / market lost, market
        # removed and re-added, combination dropped from the list ...): START needs consecutive
        # evaluations, so a cycle without an evaluation ends the confirmation run. Without this, a
        # pass streak built before the gap would still count when the combination comes back.
        for key, s in list(self.store.states.items()):
            if key in verdicts or s.active or not s.pass_streak:
                continue
            s.pass_streak = 0
            self.store.save(s, now_i)

    # ------------------------------------------------------------ per-bot START limit
    def starts_in_window(self, bot: str, now_i: int) -> int:
        """STARTs of ``bot`` (all markets) still inside the rolling window ending at ``now_i``.
        A START at ``t`` counts while ``now - t < window``."""
        return int(self.conn.execute("SELECT COUNT(*) FROM signal_start_ledger WHERE bot=? AND started_at > ?",
                                     (bot, now_i - self.limit.window_s)).fetchone()[0])

    def _limit_blocks(self, v: BotVerdict, s, now_i: int, previous_status) -> bool:
        """True if this START must not be sent. Otherwise the START is entered in the ledger (same
        transaction as the START) and False is returned. Fails closed on any database error."""
        if not self.limit.enabled:
            return False
        used = None
        try:
            used = self.starts_in_window(v.bot, now_i)
            if used < self.limit.max_starts:
                self.conn.execute("INSERT INTO signal_start_ledger(bot, symbol, started_at) VALUES (?,?,?)",
                                  (v.bot, v.symbol, now_i))
                return False
            oldest = self.conn.execute("SELECT MIN(started_at) FROM signal_start_ledger WHERE bot=? AND started_at > ?",
                                       (v.bot, now_i - self.limit.window_s)).fetchone()[0]
            free_at = int(oldest) + self.limit.window_s if oldest is not None else now_i
        except sqlite3.Error as exc:
            # cannot prove the bot is under its limit (or the START is already in the ledger):
            # no START this cycle; STOP rules and other combinations are not affected
            event(log, "signal_limit_check_failed", logging.ERROR, bot=v.bot, symbol=v.symbol,
                  error=f"{type(exc).__name__}: {exc}"[:300])
            s.last_status = LIMIT_REACHED
            return True
        s.last_status = LIMIT_REACHED
        if previous_status != LIMIT_REACHED:          # once per blocked episode, not every minute
            reason = (f"{used} START signals for this bot in the last {self.limit.window_s / 3600:g} h "
                      f"(limit {self.limit.max_starts}); next START possible from epoch {free_at}")
            event(log, "signal_limit_reached", logging.WARNING, bot=v.bot, symbol=v.symbol, used=used,
                  limit=self.limit.max_starts, window_h=self.limit.window_s / 3600, free_at=free_at)
            try:                                       # audit row only: never fails the cycle
                self.conn.execute("INSERT INTO signal_events(bot, symbol, kind, reason, created_at, details)"
                                  " VALUES (?,?,?,?,?,?)",
                                  (v.bot, v.symbol, LIMIT_REACHED, reason, now_i,
                                   json.dumps({"used": used, "limit": self.limit.max_starts,
                                               "window_s": self.limit.window_s, "free_at": free_at})))
            except sqlite3.Error as exc:
                event(log, "signal_limit_event_unsaved", logging.ERROR, bot=v.bot, symbol=v.symbol,
                      error=f"{type(exc).__name__}: {exc}"[:300])
        return True

    def discard_start_credit(self, now: float | None = None, reason: str = "evaluation failed") -> int:
        """Called for a cycle that produced no verdicts at all (the evaluation raised before or
        inside ``process``). Such a cycle is a gap in every confirmation run, so the pass streak of
        every inactive combination is reset: a later START again needs ``start_confirm_cycles``
        fresh consecutive evaluations.

        Active signals are not touched (active flag, fail streak), so the STOP rules work exactly as
        before. The reset is kept in memory even if it cannot be written (it only ever removes START
        credit; the stored counters are cleared on the next restart anyway). Never raises: it runs
        inside the evaluation loop's error handler. Returns the number of combinations reset."""
        now_i = int(now or time.time())
        changed = [s for s in self.store.states.values() if not s.active and s.pass_streak]
        for s in changed:
            s.pass_streak = 0
        if changed:
            try:
                with transaction(self.conn, "signal_engine.discard_start_credit"):
                    for s in changed:
                        self.store.save(s, now_i)
            except Exception as exc:   # noqa: BLE001 - memory already reset; must not mask the cycle error
                event(log, "signal_streak_reset_unsaved", logging.ERROR, combinations=len(changed),
                      error=f"{type(exc).__name__}: {exc}"[:300])
        event(log, "signal_start_credit_discarded", logging.WARNING, combinations=len(changed), reason=reason)
        return len(changed)

    def _record(self, t: Transition, text: str, now_i: int, v: BotVerdict | None) -> None:
        details = json.dumps(v.summary() if v else {}, default=str)[:20000]
        self.conn.execute("INSERT INTO signal_events(bot, symbol, kind, reason, created_at, details)"
                          " VALUES (?,?,?,?,?,?)", (t.bot, t.symbol, t.kind, t.reason, now_i, details))
        key = f"{t.kind}:{t.bot}:{t.symbol}:{now_i}"
        self.conn.execute("INSERT OR IGNORE INTO notifications(kind, dedupe_key, text, created_at, next_attempt_at)"
                          " VALUES (?,?,?,?,?)", (t.kind, key, text, now_i, now_i))

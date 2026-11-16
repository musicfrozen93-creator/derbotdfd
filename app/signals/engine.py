"""START / STOP state machine.

Rules
* one START when a (bot, market) combination becomes a confirmed RUN_CANDIDATE
* one STOP when an active combination stops qualifying
* never a WAIT message, never a repeated START/STOP
* every combination has its own independent state
* START needs ``start_confirm_cycles`` consecutive RUN_CANDIDATE evaluations and
  the restart cooldown to have elapsed since the last STOP
* STOP needs ``stop_confirm_cycles`` consecutive failing evaluations, except a
  critical fault (contract no longer offered, feed down beyond the critical
  threshold) which stops immediately
* a pure data-quality dip (window not clean, but not a critical fault) freezes
  the counters instead of stopping an active signal
* during the start-up grace period, data faults on active signals are frozen
  too (the feed and back-fill are still catching up after a restart)

State changes and their notifications are written in one SQLite transaction
(outbox pattern), so a restart never loses or duplicates a transition.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from app.analytics.validation import RUN, BotVerdict
from app.monitoring.logger import event
from app.signals.state import SignalStateStore

log = logging.getLogger("signals.engine")


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
        self.conn.execute("BEGIN")
        try:
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
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            self.store.reload()
            raise
        for t in out:
            event(log, "signal_stop", logging.WARNING, bot=t.bot, symbol=t.symbol, reason=t.reason, critical=True)
        return out

    def process(self, verdicts: dict[tuple[str, str], BotVerdict], now: float | None = None,
                missing_reason: dict[tuple[str, str], str] | None = None) -> list[Transition]:
        now = now or time.time()
        now_i = int(now)
        start_n = int(self.cfg.start_confirm_cycles)
        stop_n = int(self.cfg.stop_confirm_cycles)
        cooldown = float(self.cfg.restart_cooldown_minutes) * 60
        grace = self.in_grace(now)
        out: list[Transition] = []

        self.conn.execute("BEGIN")
        try:
            for key, v in verdicts.items():
                s = self.store.get(*key)
                s.last_status = v.status
                if v.status == RUN:
                    s.pass_streak += 1
                    s.fail_streak = 0
                elif v.hold or (grace and v.critical_kind == "data"):
                    s.pass_streak = 0          # cannot START on unclean data, but do not count toward STOP
                else:
                    s.pass_streak = 0
                    s.fail_streak += 1
                if not s.active:
                    cooled = s.last_stop_at is None or now - s.last_stop_at >= cooldown
                    if v.status == RUN and s.pass_streak >= start_n and cooled:
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
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            # reload persisted state so memory matches the database
            self.store.reload()
            raise
        for t in out:
            event(log, f"signal_{t.kind.lower()}", bot=t.bot, symbol=t.symbol, reason=t.reason, critical=t.critical)
        return out

    def _record(self, t: Transition, text: str, now_i: int, v: BotVerdict | None) -> None:
        details = json.dumps(v.summary() if v else {}, default=str)[:20000]
        self.conn.execute("INSERT INTO signal_events(bot, symbol, kind, reason, created_at, details)"
                          " VALUES (?,?,?,?,?,?)", (t.bot, t.symbol, t.kind, t.reason, now_i, details))
        key = f"{t.kind}:{t.bot}:{t.symbol}:{now_i}"
        self.conn.execute("INSERT OR IGNORE INTO notifications(kind, dedupe_key, text, created_at, next_attempt_at)"
                          " VALUES (?,?,?,?,?)", (t.kind, key, text, now_i, now_i))

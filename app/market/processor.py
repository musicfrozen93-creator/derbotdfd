"""Tick validation, digit extraction, de-duplication and batched persistence."""
from __future__ import annotations

import logging
import time

from app.deriv.ticks import parse_tick
from app.market.health import HealthMonitor
from app.market.precision import format_quote
from app.monitoring.logger import event
from app.storage import repositories as repo

log = logging.getLogger("market.processor")


class TickProcessor:
    def __init__(self, conn, health: HealthMonitor, *, missing_factor: float = 1.5) -> None:
        self.conn = conn
        self.health = health
        self.missing_factor = missing_factor
        self.decimals: dict[str, int] = {}          # from active_symbols
        self.last_epoch: dict[str, int] = {}
        self.last_quote: dict[str, str] = {}
        self.buffer: list[tuple[str, int, str, int]] = []
        self.allowed: set[str] = set()
        self._pip_warned: set[str] = set()
        self.on_out_of_order = None   # callback(symbol) -> marks analysis cache dirty

    def set_symbol(self, symbol: str, decimals: int) -> None:
        self.decimals[symbol] = decimals
        self.allowed.add(symbol)
        if symbol not in self.last_epoch:
            last = repo.last_epoch(self.conn, symbol)
            if last is not None:
                self.last_epoch[symbol] = int(last)
        if not self.health.get(symbol).nominal_known:
            interval = repo.median_interval(self.conn, symbol)
            if interval:
                self.health.set_nominal(symbol, interval)

    def remove_symbol(self, symbol: str) -> None:
        self.allowed.discard(symbol)

    def on_tick(self, tick: dict) -> None:
        try:
            raw = parse_tick(tick)
        except (ValueError, TypeError, ArithmeticError) as exc:
            event(log, "tick_malformed", logging.WARNING, error=str(exc)[:200])
            sym = tick.get("symbol") if isinstance(tick, dict) else None
            if sym:
                self.health.get(sym).malformed += 1
            return
        sym = raw.symbol
        if sym not in self.allowed:
            return
        dec = self.decimals.get(sym)
        if raw.pip_decimals is not None:
            if dec is not None and raw.pip_decimals != dec and sym not in self._pip_warned:
                self._pip_warned.add(sym)
                event(log, "pip_size_mismatch", logging.WARNING, symbol=sym, active_symbols=dec,
                      tick=raw.pip_decimals)
            dec = raw.pip_decimals   # the tick's own pip size is authoritative for that tick
        if dec is None:
            event(log, "tick_without_precision", logging.WARNING, symbol=sym)
            return
        text = format_quote(raw.quote, dec)
        h = self.health.get(sym)
        last = self.last_epoch.get(sym)
        if last is not None and raw.epoch == last:
            h.duplicates += 1
            if self.last_quote.get(sym) not in (None, text):
                h.duplicate_conflicts += 1
                event(log, "duplicate_epoch_conflict", logging.WARNING, symbol=sym, epoch=raw.epoch,
                      first=self.last_quote.get(sym), second=text)
            return
        if last is not None and raw.epoch < last:
            h.out_of_order += 1
            if self.on_out_of_order:
                self.on_out_of_order(sym)
        elif (last is not None and h.nominal_known
              and raw.epoch - last > self.missing_factor * max(h.nominal_interval, 1.0)):
            h.missing_events += 1
            h.recent_gaps.append((last, raw.epoch))
            del h.recent_gaps[:-50]
            event(log, "missing_ticks_detected", logging.INFO, symbol=sym, from_epoch=last, to_epoch=raw.epoch,
                  gap_s=raw.epoch - last)
        self.buffer.append((sym, raw.epoch, text, int(text[-1])))
        if last is None or raw.epoch > last:
            self.last_epoch[sym] = raw.epoch
            self.last_quote[sym] = text
        self.health.on_tick(sym, raw.epoch, time.time())

    def flush(self) -> int:
        if not self.buffer:
            return 0
        rows, self.buffer = self.buffer, []
        return repo.insert_ticks(self.conn, rows)

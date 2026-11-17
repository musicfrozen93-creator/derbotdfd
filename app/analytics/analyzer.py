"""Evaluation cycle: last ``window_ticks`` ticks per market -> digit percentages -> verdicts.

Runs in a worker thread (``asyncio.to_thread``); never touches the event loop.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from app.analytics.validation import (CLEAN, DEGRADED, FAIL, FAULT, PASS, BotVerdict, RuleConfig,
                                      SymbolQuality, digit_counts, evaluate_combo)
from app.market.health import FAULT as LIVE_FAULT
from app.market.health import OK as LIVE_OK
from app.storage import database
from app.storage import repositories as repo
from app.strategies.loader import BotSpec

log = logging.getLogger("analytics.analyzer")


@dataclass
class Series:
    symbol: str
    epochs: np.ndarray
    digits: np.ndarray
    prices: np.ndarray

    @property
    def n(self) -> int:
        return int(self.epochs.shape[0])

    def nominal_interval(self) -> float:
        if self.n < 3:
            return 1.0
        d = np.diff(self.epochs[-5000:])
        d = d[d > 0]
        return float(np.median(d)) if d.size else 1.0


class SeriesCache:
    """In-memory numpy copies of the newest ``max_ticks`` ticks per symbol (= the rule window)."""

    def __init__(self, db_path, max_ticks: int) -> None:
        self.db_path = db_path
        self.max_ticks = int(max_ticks)
        self.conn = None
        self.series: dict[str, Series] = {}
        self._dirty: set[str] = set()
        self._last: dict[str, int] = {}
        self._lock = threading.Lock()

    def mark_history(self, symbol: str, min_epoch: int) -> None:
        """Called when back-filled rows were inserted; reload if they land inside the cached range."""
        with self._lock:
            last = self._last.get(symbol)
            if last is None or min_epoch <= last:
                self._dirty.add(symbol)

    def mark_dirty(self, symbol: str) -> None:
        with self._lock:
            self._dirty.add(symbol)

    def drop(self, symbol: str) -> None:
        with self._lock:
            self.series.pop(symbol, None)
            self._last.pop(symbol, None)

    def get(self, symbol: str) -> Series:
        if self.conn is None:
            self.conn = database.connect(self.db_path)
        with self._lock:
            dirty = symbol in self._dirty
            self._dirty.discard(symbol)
        cur = self.series.get(symbol)
        if cur is None or dirty or cur.n == 0:
            e, d, p = repo.load_series_rows(self.conn, symbol, self.max_ticks)
            cur = Series(symbol, e, d, p)
        else:
            e, d, p = repo.load_series_rows(self.conn, symbol, self.max_ticks, after_epoch=int(cur.epochs[-1]))
            if e.size:
                cur = Series(symbol, np.concatenate((cur.epochs, e))[-self.max_ticks:],
                             np.concatenate((cur.digits, d))[-self.max_ticks:],
                             np.concatenate((cur.prices, p))[-self.max_ticks:])
        self.series[symbol] = cur
        with self._lock:
            if cur.n:
                self._last[symbol] = int(cur.epochs[-1])
        return cur


@dataclass
class EvalResult:
    verdicts: dict[tuple[str, str], BotVerdict]
    quality: dict[str, SymbolQuality]
    duration_s: float
    tests: int                       # rule checks made (one per combination with a full window)
    contract_checks: int = 0         # individual contract evaluations made (PASS or FAIL)


class Analyzer:
    def __init__(self, bots: list[BotSpec], cache: SeriesCache, rule: RuleConfig) -> None:
        self.bots = {b.key: b for b in bots}
        self.cache = cache
        self.rule = rule

    def symbol_quality(self, series: Series, live: tuple[str, str]) -> SymbolQuality:
        live_status, live_detail = live
        w = self.rule.window_ticks
        digits = series.digits[-w:]
        epochs = series.epochs[-w:]
        q = SymbolQuality(series.symbol, CLEAN, "live feed OK", live_status, int(digits.shape[0]),
                          series.nominal_interval(), digit_counts(digits),
                          int(epochs[0]) if epochs.size else 0, int(epochs[-1]) if epochs.size else 0)
        if live_status == LIVE_FAULT:
            q.status, q.detail = FAULT, live_detail
        elif live_status != LIVE_OK:
            q.status, q.detail = DEGRADED, f"live feed: {live_detail}"
        return q

    def evaluate(self, combos: list[tuple[str, str]], compat: dict, markets: dict,
                 live: dict[str, tuple[str, str]], now: float | None = None) -> EvalResult:
        t0 = time.time()
        now = now or t0
        quality: dict[str, SymbolQuality] = {}
        for sym in sorted({s for _, s in combos}):
            quality[sym] = self.symbol_quality(self.cache.get(sym), live.get(sym, ("STALE", "not subscribed")))
        verdicts: dict[tuple[str, str], BotVerdict] = {}
        tests = 0
        contract_checks = 0
        for bkey, sym in combos:
            c = compat.get((bkey, sym))
            m = markets.get(sym)
            v = evaluate_combo(self.bots[bkey], sym, m.display_name if m else sym, quality[sym],
                               compatible=bool(c and c.compatible),
                               compat_text=(c.reason_text if c else "not checked"), rule=self.rule, now=now,
                               leg_compat=(c.legs if c else None))
            tests += int(quality[sym].n_ticks >= self.rule.window_ticks)
            contract_checks += sum(1 for r in v.contracts if r.result in (PASS, FAIL))
            verdicts[(bkey, sym)] = v
        return EvalResult(verdicts, quality, time.time() - t0, tests, contract_checks)

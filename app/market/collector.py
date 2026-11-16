"""Market data collector: live subscriptions, reconnect re-subscription,
gap back-fill and initial history bootstrap."""
from __future__ import annotations

import asyncio
import logging
import time

import numpy as np

from app.deriv.client import DerivAPIError, NotConnectedError
from app.deriv.history import fetch_page, fetch_range_backwards, page_rows
from app.deriv.symbols import MarketInfo
from app.market.health import HealthMonitor
from app.market.processor import TickProcessor
from app.monitoring.logger import event
from app.storage import repositories as repo

log = logging.getLogger("market.collector")


class Collector:
    def __init__(self, client, conn, processor: TickProcessor, health: HealthMonitor, cfg, *,
                 on_history_inserted=None) -> None:
        self.client = client
        self.conn = conn
        self.processor = processor
        self.health = health
        self.cfg = cfg   # Section: collection + data_quality merged view
        self.symbols: dict[str, MarketInfo] = {}
        self.subscriptions: dict[str, str | None] = {}
        self.priority: list[str] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self._queued: set[tuple] = set()
        self._attempted_gaps: set[tuple[str, int, int]] = set()
        self.on_history_inserted = on_history_inserted   # callback(symbol, min_epoch)
        self.bootstrap_done: set[str] = set()
        self._sub_lock = asyncio.Lock()

    # -------------------------------------------------------------- symbols
    async def set_symbols(self, markets: dict[str, MarketInfo], priority: list[str]) -> None:
        self.priority = priority
        async with self._sub_lock:
            new = set(markets) - set(self.symbols)
            gone = set(self.symbols) - set(markets)
            self.symbols = dict(markets)
            for sym, m in markets.items():
                self.processor.set_symbol(sym, m.pip_decimals)
            for sym in gone:
                self.processor.remove_symbol(sym)
                sub = self.subscriptions.pop(sym, None)
                self.health.remove(sym)
                if sub and self.client.connected:
                    await self.client.forget(sub)
                event(log, "market_removed_from_monitoring", symbol=sym)
            for sym in self._ordered(new):
                event(log, "market_added_to_monitoring", symbol=sym, name=markets[sym].display_name)
                last = repo.last_epoch(self.conn, sym)     # history from a previous run?
                if self.client.connected:
                    await self._subscribe(sym)
                if last is not None:                        # fill the downtime gap first
                    self._enqueue(("gap", sym, int(last) + 1, int(time.time())))
                self._enqueue(("bootstrap", sym))

    def _ordered(self, symbols) -> list[str]:
        pri = {s: i for i, s in enumerate(self.priority)}
        return sorted(symbols, key=lambda s: (pri.get(s, len(pri)), s))

    async def _subscribe(self, sym: str) -> None:
        try:
            sub_id = await self.client.subscribe_ticks(sym)
            self.subscriptions[sym] = sub_id
            self.health.on_subscribed(sym)
            event(log, "tick_subscription_ok", symbol=sym)
        except (DerivAPIError, NotConnectedError, asyncio.TimeoutError) as exc:
            event(log, "tick_subscription_failed", logging.WARNING, symbol=sym, error=str(exc))

    async def on_connect(self) -> None:
        """Called by the client after every (re)connection."""
        self.health.set_connected(True)
        async with self._sub_lock:
            self.subscriptions.clear()
            for sym in self._ordered(self.symbols):
                last = self.processor.last_epoch.get(sym) or repo.last_epoch(self.conn, sym)
                await self._subscribe(sym)
                if last is not None:
                    self._enqueue(("gap", sym, int(last) + 1, int(time.time())))

    def on_disconnect(self) -> None:
        self.health.set_connected(False)
        self.subscriptions.clear()

    # ------------------------------------------------------------ backfill
    def _enqueue(self, job: tuple) -> None:
        key = job[:2] if job[0] == "bootstrap" else job
        if key in self._queued:
            return
        self._queued.add(key)
        self.queue.put_nowait(job)

    def _store(self, sym: str, rows) -> int:
        if not rows:
            return 0
        n = repo.insert_ticks(self.conn, rows)
        if n and self.on_history_inserted:
            self.on_history_inserted(sym, min(r[1] for r in rows))
        return n

    async def backfill_worker(self) -> None:
        while True:
            job = await self.queue.get()
            key = job[:2] if job[0] == "bootstrap" else job
            try:
                await self.client.wait_connected(None)
                if job[0] == "gap":
                    await self._fill_range(job[1], job[2], job[3])
                elif job[0] == "bootstrap":
                    await self._bootstrap(job[1])
            except (NotConnectedError, DerivAPIError, asyncio.TimeoutError, ValueError) as exc:
                event(log, "backfill_job_failed", logging.WARNING, job=job[0], symbol=job[1], error=str(exc))
                if isinstance(exc, NotConnectedError) or isinstance(exc, asyncio.TimeoutError):
                    await asyncio.sleep(5)
                    self._queued.discard(key)
                    self._enqueue(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event=backfill_job_crashed job=%s", job)
            self._queued.discard(key)

    async def _fill_range(self, sym: str, start: int, end: int) -> None:
        if sym not in self.symbols or end < start:
            return
        m = self.symbols[sym]
        total = 0
        async for page in fetch_range_backwards(self.client, sym, start, end,
                                                page_size=int(self.cfg.history_page_size)):
            rows = [r for r in page_rows(page, m.pip_decimals) if start <= r[1] <= end]
            total += self._store(sym, rows)
        event(log, "gap_backfill_done", symbol=sym, start=start, end=end, inserted=total)

    async def _bootstrap(self, sym: str) -> None:
        if sym not in self.symbols:
            return
        m = self.symbols[sym]
        target = int(self.cfg.bootstrap_target_ticks)
        oldest_allowed = int(time.time()) - int(self.cfg.bootstrap_max_days) * 86400
        first, _last, count = repo.tick_bounds(self.conn, sym)
        inserted = 0
        end = (first - 1) if first else "latest"
        while count < target:
            if isinstance(end, int) and end < oldest_allowed:
                break
            page = await fetch_page(self.client, sym, end=end, count=int(self.cfg.history_page_size))
            if not page.epochs:
                break
            rows = page_rows(page, m.pip_decimals)
            n = self._store(sym, rows)
            inserted += n
            count += n
            oldest = min(page.epochs)
            if isinstance(end, int) and oldest >= end:
                break
            end = oldest - 1
            if len(page.epochs) < int(self.cfg.history_page_size):
                break
            if sym not in self.symbols:
                return
        self.bootstrap_done.add(sym)
        event(log, "bootstrap_done", symbol=sym, inserted=inserted, stored=count, target=target)

    async def audit_gaps(self, gap_threshold_for) -> None:
        """Scan recent stored history for holes and queue one back-fill attempt each."""
        now = int(time.time())
        lookback = int(self.cfg.gap_audit_lookback_hours) * 3600
        for sym in list(self.symbols):
            ep = repo.epochs_between(self.conn, sym, now - lookback, now)
            if ep.size < 2:
                continue
            thr = gap_threshold_for(sym, ep)
            d = np.diff(ep)
            idx = np.nonzero(d > thr)[0]
            for i in idx[-20:]:
                a, b = int(ep[i]) + 1, int(ep[i + 1]) - 1
                key = (sym, a, b)
                if key in self._attempted_gaps:
                    continue
                self._attempted_gaps.add(key)
                self._enqueue(("gap", sym, a, b))
        if len(self._attempted_gaps) > 20000:
            self._attempted_gaps.clear()

    async def flush_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                self.processor.flush()
            except Exception:
                log.exception("event=tick_flush_failed")

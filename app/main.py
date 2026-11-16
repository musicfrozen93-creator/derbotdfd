"""Application entry point: wires the collector, analyzer and signal engine.

    python -m app.main                 # run the service
    python -m app.main --dry-run       # run, but log notifications instead of sending them
    python -m app.main --validate      # load config + strategies, print the inventory, exit
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import __version__
from app.analytics.analyzer import Analyzer, EvalResult, SeriesCache
from app.analytics.payout import PayoutBook
from app.config import Settings, load_settings
from app.deriv.client import DerivAPIError, DerivClient, NotConnectedError
from app.deriv.contracts import fetch_contracts
from app.deriv.symbols import MarketInfo, fetch_active_symbols, select_candidates
from app.market.collector import Collector
from app.market.health import FAULT as LIVE_FAULT
from app.market.health import HealthMonitor
from app.market.processor import TickProcessor
from app.monitoring.heartbeat import write_heartbeat
from app.monitoring.logger import event, setup_logging
from app.signals.engine import SignalEngine
from app.signals.state import SignalStateStore
from app.signals.telegram import MessageFormatter, TelegramSender, enqueue
from app.storage import repositories as repo
from app.storage.database import init_db
from app.strategies.compatibility import CompatResult, build_matrix
from app.strategies.loader import load_strategies
from app.strategies.triggers import describe_trigger

log = logging.getLogger("app")


class Application:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.db_path = settings.path(settings.storage.db_path)
        self.conn = init_db(self.db_path)
        self.bots = load_strategies(settings.path(settings.strategies.dir), list(settings.strategies.enabled))
        self.bot_map = {b.key: b for b in self.bots}
        dq, an, dv = settings.data_quality, settings.analysis, settings.deriv
        self.health = HealthMonitor(stale_factor=float(dq.stale_factor), critical_stale_s=float(dq.critical_stale_s),
                                    critical_disconnect_s=float(dq.critical_disconnect_s))
        self.client = DerivClient(
            dv.ws_url, request_timeout=float(dv.request_timeout_s), open_timeout=float(dv.open_timeout_s),
            ping_interval=float(dv.ping_interval_s), idle_reconnect=float(dv.idle_reconnect_s),
            backoff_min=float(dv.reconnect_backoff_min_s), backoff_max=float(dv.reconnect_backoff_max_s),
            general_per_min=float(dv.rate_limit_general_per_min),
            proposal_per_min=float(dv.rate_limit_proposal_per_min))
        self.processor = TickProcessor(self.conn, self.health, missing_factor=float(dq.missing_factor))
        self.cache = SeriesCache(self.db_path, int(an.max_ticks))
        self.processor.on_out_of_order = self.cache.mark_dirty
        self.collector = Collector(self.client, self.conn, self.processor, self.health, settings.collection,
                                   on_history_inserted=self.cache.mark_history)
        self.client.on_tick = self.processor.on_tick
        self.client.add_connect_hook(self.collector.on_connect)
        self.client.add_disconnect_hook(self.collector.on_disconnect)
        pc = settings.payout
        self.payouts = PayoutBook(self.conn, stake=float(pc.probe_stake), currency=str(pc.currency),
                                  max_age_s=float(pc.max_age_minutes) * 60)
        self.analyzer = Analyzer(self.bots, self.cache, an, dq)
        tg = settings.telegram
        self.formatter = MessageFormatter(self.bot_map, str(tg.timezone), int(an.risk_streak))
        self.store = SignalStateStore(self.conn)
        self.engine = SignalEngine(self.conn, self.store, self.formatter, settings.signals)
        self.sender = TelegramSender(self.conn, settings.telegram_token, settings.telegram_chat_id,
                                     enabled=bool(tg.enabled), dry_run=settings.dry_run,
                                     timeout=float(tg.send_timeout_s), max_attempts=int(tg.max_attempts))
        self.all_markets: dict[str, MarketInfo] = {}
        self.markets: dict[str, MarketInfo] = {}
        self.contracts: dict = {}
        self.matrix: dict[tuple[str, str], CompatResult] = {}
        self.discovery_done = asyncio.Event()
        self.last_eval: EvalResult | None = None
        self.last_status: dict[tuple[str, str], str] = {}
        self._error_alerts: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self.started_at = time.time()

    # ------------------------------------------------------------ discovery
    async def discover(self) -> None:
        cfg = self.s.discovery
        listed = await fetch_active_symbols(self.client)
        if not listed:
            raise RuntimeError("active_symbols returned no markets")
        self.all_markets = {m.symbol: m for m in listed}
        cands = select_candidates(listed, include_markets=list(cfg.markets),
                                  include_submarkets=list(cfg.include_submarkets),
                                  exclude_symbols=list(cfg.exclude_symbols))
        repo.upsert_markets(self.conn, cands)
        gone = [s for s in self.markets if s not in self.all_markets]
        if gone:
            repo.deactivate_markets(self.conn, gone)
        contracts = {}
        for m in cands:
            try:
                sc = await fetch_contracts(self.client, m.symbol)
                contracts[m.symbol] = sc
                repo.replace_contracts(self.conn, m.symbol, sc)
            except (DerivAPIError, NotConnectedError, asyncio.TimeoutError) as exc:
                # A failed call (RateLimit, timeout, dropped socket) says nothing about the market:
                # keep the last known listing (memory, or the database after a restart). Only a
                # successful listing without the contract, or the symbol leaving active_symbols,
                # removes a market.
                event(log, "contracts_for_failed", logging.WARNING, symbol=m.symbol, error=str(exc))
                last = self.contracts.get(m.symbol) or repo.load_contracts(self.conn, m.symbol)
                if last is not None:
                    contracts[m.symbol] = last
        cand_map = {m.symbol: m for m in cands}
        matrix = build_matrix(self.bots, cand_map, contracts)
        repo.save_compatibility(self.conn, [(b, s, r.compatible, r.reason_text) for (b, s), r in matrix.items()])
        for key, r in matrix.items():
            old = self.matrix.get(key)
            if r.compatible and (old is None or not old.compatible):
                event(log, "bot_compatibility_confirmed", bot=key[0], symbol=key[1])
            elif old is not None and old.compatible and not r.compatible:
                event(log, "bot_compatibility_lost", logging.WARNING, bot=key[0], symbol=key[1], reason=r.reason_text)
        for sym, sc in contracts.items():
            digit = sorted(c for c in sc.contracts if c.startswith("DIGIT"))
            if digit and sym not in self.contracts:
                event(log, "contracts_verified", symbol=sym, digit_contracts=",".join(digit))
        compatible_syms = sorted({s for (b, s), r in matrix.items() if r.compatible})
        pri = {s: i for i, s in enumerate(cfg.priority_symbols)}
        compatible_syms.sort(key=lambda s: (pri.get(s, len(pri)), s))
        monitored = compatible_syms[: int(cfg.max_symbols)]
        if len(compatible_syms) > len(monitored):
            event(log, "max_symbols_reached", logging.WARNING, compatible=len(compatible_syms), kept=len(monitored))
        new_markets = {s: cand_map[s] for s in monitored}
        for s in set(new_markets) - set(self.markets):
            event(log, "market_discovered", symbol=s, name=new_markets[s].display_name,
                  pip_decimals=new_markets[s].pip_decimals)
        for s in set(self.markets) - set(new_markets):
            event(log, "market_removed", logging.WARNING, symbol=s)
            self.cache.drop(s)
        self.contracts = contracts
        self.matrix = matrix
        self.markets = new_markets
        await self.collector.set_symbols(new_markets, list(cfg.priority_symbols))
        event(log, "discovery_done", listed=len(listed), candidates=len(cands), monitored=len(new_markets),
              compatible_pairs=sum(1 for r in matrix.values() if r.compatible))
        self.discovery_done.set()

    async def discovery_loop(self) -> None:
        interval = float(self.s.discovery.interval_minutes) * 60
        while True:
            try:
                await self.client.wait_connected(None)
                await self.discover()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("event=discovery_failed")
                await self.system_error("discovery", exc)
                await asyncio.sleep(60)

    # --------------------------------------------------------------- payouts
    def needed_quotes(self) -> set[tuple]:
        out = set()
        for (bkey, sym), r in self.matrix.items():
            if not r.compatible or sym not in self.markets:
                continue
            for leg in self.bot_map[bkey].legs:
                out.add((sym, leg.contract_type, leg.barrier, leg.duration_ticks))
        return out

    async def payout_loop(self) -> None:
        await self.discovery_done.wait()
        while True:
            delay = float(self.s.payout.refresh_minutes) * 60
            try:
                await self.client.wait_connected(None)
                await self.payouts.refresh(self.client, self.needed_quotes(),
                                           rate_limit_pause_s=float(self.s.payout.rate_limit_pause_s))
                if self.payouts.failed:            # some quotes missing: try again sooner
                    delay = min(delay, float(self.s.payout.retry_failed_minutes) * 60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("event=payout_refresh_failed")
                await self.system_error("payout", exc)
                delay = min(delay, float(self.s.payout.retry_failed_minutes) * 60)
            await asyncio.sleep(delay)

    def payout_snapshot(self, now: float) -> dict:
        snap = {}
        for key, q in list(self.payouts.quotes.items()):
            if q.break_even is not None and now - q.fetched_at <= self.payouts.max_age_s:
                snap[key] = q
        return snap

    # ------------------------------------------------------------ evaluation
    def combos(self) -> tuple[list[tuple[str, str]], dict]:
        combos = {k for k, r in self.matrix.items() if r.compatible and k[1] in self.markets}
        missing = {}
        for st in self.store.active():
            key = (st.bot, st.symbol)
            if st.bot in self.bot_map and st.symbol in self.markets:
                combos.add(key)                     # evaluated -> STOP if contract lost
            elif st.symbol not in self.all_markets:
                missing[key] = "market no longer listed by Deriv active_symbols"
            elif st.bot not in self.bot_map:
                missing[key] = "bot disabled in configuration"
            else:
                missing[key] = "market no longer compatible / monitored"
        return sorted(combos), missing

    async def evaluate_once(self) -> EvalResult:
        now = time.time()
        combos, missing = self.combos()
        live = {sym: self.health.status(sym, now) for sym in self.markets}
        result = await asyncio.to_thread(self.analyzer.evaluate, combos, self.matrix, self.all_markets,
                                         self.payout_snapshot(now), live, now)
        for sym, q in result.quality.items():
            self.health.set_nominal(sym, q.nominal_interval)
        repo.save_evaluations(self.conn, [(v.bot, v.symbol, v.status, v.summary())
                                          for v in result.verdicts.values()])
        for key, v in result.verdicts.items():
            prev = self.last_status.get(key)
            if prev != v.status:
                lvl = logging.INFO
                event(log, "validation_" + ("passed" if v.status == "RUN_CANDIDATE" else "status"), lvl,
                      bot=v.bot, symbol=v.symbol, status=v.status, previous=prev,
                      reason=(v.reasons[0] if v.reasons else ""))
                self.last_status[key] = v.status
        self.engine.process(result.verdicts, now, missing)
        self.last_eval = result
        slow = result.duration_s > float(self.s.analysis.interval_s)
        event(log, "evaluation_slow" if slow else "evaluation_cycle", logging.WARNING if slow else logging.INFO,
              combos=len(combos), tests=result.tests, seconds=round(result.duration_s, 2), sims=result.sims_ran)
        return result

    async def evaluation_loop(self) -> None:
        await self.discovery_done.wait()
        interval = float(self.s.analysis.interval_s)
        while True:
            t0 = time.monotonic()
            try:
                self.processor.flush()
                await self.evaluate_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("event=evaluation_failed")
                await self.system_error("evaluation", exc)
            await asyncio.sleep(max(1.0, interval - (time.monotonic() - t0)))

    # ------------------------------------------------------- health / alerts
    def _active_on(self, symbol: str | None = None) -> list[str]:
        out = []
        for st in self.store.active():
            if symbol is None or st.symbol == symbol:
                name = self.bot_map[st.bot].name if st.bot in self.bot_map else st.bot
                out.append(f"{name} on {st.symbol}")
        return out

    def _alert(self, key: str, kind: str, symbol: str | None, title: str, detail: str, now: float) -> None:
        if repo.open_incident(self.conn, key, kind, symbol, detail):
            event(log, "incident_opened", logging.ERROR, key=key, detail=detail)
            if bool(self.s.telegram.critical_alerts):
                enqueue(self.conn, "CRITICAL", f"incident:{key}:{int(now)}",
                        self.formatter.critical(title, detail, now, self._active_on(symbol)), now)

    def _recover(self, key: str, title: str, detail: str, now: float) -> None:
        was_open, opened = repo.close_incident(self.conn, key)
        if was_open:
            event(log, "incident_closed", key=key)
            if bool(self.s.telegram.critical_alerts):
                dur = f" (lasted {int(now - opened)} s)" if opened else ""
                enqueue(self.conn, "RECOVERED", f"recovered:{key}:{int(now)}",
                        self.formatter.recovered(title, detail + dur, now), now)

    async def health_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                now = time.time()
                self.health.set_connected(self.client.connected, now)
                if self.health.connection_fault(now):
                    self._alert("connection", "connection", None, "Deriv API disconnected",
                                f"No connection to the Deriv public WebSocket for "
                                f"{int(now - self.health.disconnected_since)} s. Reconnecting automatically.", now)
                elif self.client.connected:
                    self._recover("connection", "Deriv API connection restored", "Live data collection resumed.", now)
                if self.client.connected and now - self.started_at > float(self.s.signals.startup_grace_s):
                    for sym in list(self.markets):
                        status, detail = self.health.status(sym, now)
                        key = f"feed:{sym}"
                        name = self.markets[sym].display_name
                        if status == LIVE_FAULT:
                            self._alert(key, "feed", sym, f"Deriv feed stale — {sym}",
                                        f"Market: {name} ({sym})\nDetail: {detail}", now)
                        elif status == "OK":
                            self._recover(key, f"Deriv feed live — {sym}", f"Market: {name} ({sym})", now)
                    for inc in repo.open_incidents(self.conn):
                        if inc["kind"] == "feed" and inc["symbol"] not in self.markets:
                            repo.close_incident(self.conn, inc["key"])
            except Exception:
                log.exception("event=health_loop_error")

    async def system_error(self, where: str, exc: Exception) -> None:
        now = time.time()
        cooldown = float(self.s.telegram.error_alert_cooldown_minutes) * 60
        key = f"{where}:{type(exc).__name__}"
        if now - self._error_alerts.get(key, 0) < cooldown:
            return
        self._error_alerts[key] = now
        if bool(self.s.telegram.critical_alerts):
            try:
                enqueue(self.conn, "CRITICAL", f"error:{key}:{int(now)}",
                        self.formatter.critical(f"system error in {where}", f"{type(exc).__name__}: {exc}"[:500],
                                                now), now)
            except Exception:  # database may be the failing component
                log.exception("event=alert_enqueue_failed")

    # -------------------------------------------------------- housekeeping
    async def notifier_loop(self) -> None:
        while True:
            try:
                await self.sender.deliver_pending()
            except Exception:
                log.exception("event=notifier_error")
            await asyncio.sleep(2)

    async def gap_audit_loop(self) -> None:
        await self.discovery_done.wait()
        await asyncio.sleep(120)
        thr_factor = float(self.s.data_quality.gap_factor)

        def thr(sym, epochs):
            nominal = self.health.get(sym).nominal_interval
            return thr_factor * max(nominal, 1.0)

        while True:
            try:
                await self.collector.audit_gaps(thr)
            except Exception:
                log.exception("event=gap_audit_failed")
            await asyncio.sleep(float(self.s.collection.gap_audit_interval_minutes) * 60)

    async def retention_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                cutoff = int(time.time()) - int(self.s.collection.retention_days) * 86400
                n = await asyncio.to_thread(self._prune, cutoff)
                if n:
                    event(log, "retention_pruned", rows=n)
            except Exception:
                log.exception("event=retention_failed")

    def _prune(self, cutoff: int) -> int:
        from app.storage.database import connect
        c = connect(self.db_path)
        try:
            n = repo.prune_ticks(c, cutoff)
            c.execute("DELETE FROM notifications WHERE status!='pending' AND created_at < ?", (cutoff,))
            return n
        finally:
            c.close()

    async def heartbeat_loop(self) -> None:
        path = self.s.path(self.s.heartbeat.file)
        silence = float(self.s.heartbeat.silence_alert_minutes) * 60
        while True:
            await asyncio.sleep(float(self.s.heartbeat.interval_s))
            try:
                self.mark_alive()
            except Exception:
                log.exception("event=mark_alive_failed")
            try:
                now = time.time()
                last_tick = max((h.last_wall or 0) for h in self.health.symbols.values()) if self.health.symbols else 0
                st_counts: dict[str, int] = {}
                if self.last_eval:
                    for v in self.last_eval.verdicts.values():
                        st_counts[v.status] = st_counts.get(v.status, 0) + 1
                write_heartbeat(path, {
                    "version": __version__, "connected": self.client.connected,
                    "monitored_symbols": sorted(self.markets), "last_tick_wall": int(last_tick),
                    "bootstrap_done": sorted(self.collector.bootstrap_done),
                    "backfill_queue": self.collector.queue.qsize(),
                    "active_signals": self._active_on(), "status_counts": st_counts,
                    "started_at": int(self.started_at),
                })
                if self.markets and now - self.started_at > max(silence, 60):
                    if last_tick and now - last_tick > silence:
                        self._alert("silence", "silence", None, "no market data",
                                    f"No ticks received from any market for {int(now - last_tick)} s.", now)
                    elif last_tick:
                        self._recover("silence", "market data flowing", "Ticks are being received again.", now)
            except Exception:
                log.exception("event=heartbeat_failed")

    async def daily_summary_loop(self) -> None:
        tg = self.s.telegram
        if not bool(tg.daily_summary):
            return
        tz = ZoneInfo(str(tg.timezone))
        hh, mm = (int(x) for x in str(tg.daily_summary_time).split(":"))
        while True:
            now_local = datetime.now(tz)
            target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= now_local:
                target += timedelta(days=1)
            await asyncio.sleep((target - now_local).total_seconds())
            try:
                now = time.time()
                st_counts: dict[str, int] = {}
                quality = {}
                if self.last_eval:
                    for v in self.last_eval.verdicts.values():
                        st_counts[v.status] = st_counts.get(v.status, 0) + 1
                    quality = {s: q.status for s, q in self.last_eval.quality.items()}
                text = self.formatter.daily_summary(
                    now, monitored=len(self.last_eval.verdicts) if self.last_eval else 0,
                    compatible=sum(1 for r in self.matrix.values() if r.compatible), symbols=len(self.markets),
                    active=self._active_on(), statuses=st_counts, quality=quality)
                enqueue(self.conn, "SUMMARY", f"summary:{target:%Y-%m-%d}", text, now)
            except Exception:
                log.exception("event=daily_summary_failed")

    # -------------------------------------------------------------- runtime
    def mark_alive(self, now: float | None = None) -> None:
        """Persist "the analyzer was running at this time" (read after the next restart)."""
        repo.meta_set(self.conn, "last_alive", str(int(now or time.time())))

    def startup_check(self, now: float | None = None) -> list:
        """Clear stale confirmation counters and STOP active signals if the analyzer itself was
        offline long enough that they went unmonitored (see SignalEngine.on_startup)."""
        now = now or time.time()
        raw = repo.meta_get(self.conn, "last_alive")
        last_alive = float(raw) if raw else None
        out = self.engine.on_startup(last_alive, now, float(self.s.signals.offline_stop_s))
        if last_alive is not None:
            event(log, "analyzer_restart", offline_s=int(now - last_alive), active=len(self.store.active()),
                  stopped=len(out))
        self.mark_alive(now)
        return out

    async def run(self) -> None:
        event(log, "analyzer_starting", version=__version__, bots=len(self.bots),
              telegram=("dry_run" if self.s.dry_run else ("on" if self.sender.enabled else "not_configured")))
        self.startup_check()
        loop = asyncio.get_running_loop()
        stop = self._stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        coros = [self.client.run(), self.discovery_loop(), self.payout_loop(), self.evaluation_loop(),
                 self.collector.flush_loop(float(self.s.collection.flush_interval_s)),
                 self.collector.backfill_worker(), self.gap_audit_loop(), self.health_loop(),
                 self.notifier_loop(), self.heartbeat_loop(), self.retention_loop(), self.daily_summary_loop()]
        self._tasks = [asyncio.create_task(c, name=getattr(c, "__name__", "task")) for c in coros]
        stopper = asyncio.create_task(stop.wait())
        pending = set(self._tasks)
        self.crashed = False
        # A loop that returns normally (e.g. the daily summary when it is disabled) is simply done;
        # a loop that raises is fatal: the process exits non-zero and systemd restarts it.
        while not stop.is_set() and not self.crashed:
            done, _ = await asyncio.wait(pending | {stopper}, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is stopper:
                    continue
                pending.discard(t)
                if not t.cancelled() and t.exception() is not None:
                    log.error("event=task_crashed task=%s error=%r", t.get_name(), t.exception())
                    self.crashed = True
        stopper.cancel()
        await self.shutdown()

    def request_stop(self) -> None:
        """Ask a running ``run()`` to shut down cleanly (used on Windows and by tests)."""
        if getattr(self, "_stop", None) is not None:
            self._stop.set()

    async def shutdown(self) -> None:
        event(log, "analyzer_stopping")
        await self.client.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            self.processor.flush()
            await self.sender.deliver_pending()
            self.mark_alive()
        except Exception:
            log.exception("event=shutdown_flush_failed")
        self.conn.close()
        event(log, "analyzer_stopped", crashed=getattr(self, "crashed", False))


def print_inventory(settings: Settings) -> None:
    bots = load_strategies(settings.path(settings.strategies.dir), list(settings.strategies.enabled))
    from app.analytics.risk import risk_lines
    for b in bots:
        print(f"\n== {b.name} ({b.source_file}) original symbol {b.original.get('symbol')}")
        for leg in b.legs:
            print(f"   leg {leg.id:<10} {leg.label:<14} {leg.duration_ticks}t  p0={leg.p0:.0%}  "
                  f"trigger: {leg.trigger_text or describe_trigger(leg.trigger)}")
        print(f"   sequence: {b.sequence}")
        for r in risk_lines(b, int(settings.analysis.risk_streak)):
            print(f"   risk: {r}")
    print(f"\n{len(bots)} bots, {sum(len(b.legs) for b in bots)} legs loaded")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deriv Market Condition Analyzer")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="log notifications instead of sending them")
    ap.add_argument("--validate", action="store_true", help="validate config and strategies, then exit")
    args = ap.parse_args(argv)
    settings = load_settings(args.config, dry_run=True if args.dry_run else None)
    if args.validate:
        print_inventory(settings)
        return 0
    lg = settings.logging
    setup_logging(settings.path(lg.dir), lg.level, int(lg.max_bytes), int(lg.backups),
                  secrets=[settings.telegram_token or ""])
    if not settings.telegram_configured and not settings.dry_run:
        log.warning("event=telegram_not_configured detail='TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing; "
                    "notifications will be logged only'")
    app = Application(settings)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    return 1 if getattr(app, "crashed", False) else 0


if __name__ == "__main__":
    sys.exit(main())

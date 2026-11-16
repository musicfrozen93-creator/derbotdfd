"""Evaluation cycle: series cache -> replay statistics -> FDR -> verdicts.

Runs in a worker thread (``asyncio.to_thread``); never touches the event loop.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from app.analytics import statistics as st
from app.analytics.validation import (CLEAN, DEGRADED, FAULT, BotVerdict, LegVerdict, SymbolQuality,
                                      bot_hold, combine_bot, evaluate_leg)
from app.market.health import FAULT as LIVE_FAULT
from app.market.health import OK as LIVE_OK
from app.storage import database
from app.storage import repositories as repo
from app.strategies.evaluator import LegStats, Series, compute_leg_stats, simulate_bot
from app.strategies.loader import BotSpec
from app.strategies.triggers import describe_trigger

log = logging.getLogger("analytics.analyzer")


class SeriesCache:
    """In-memory numpy copies of the newest ``max_ticks`` ticks per symbol."""

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
    tests: int
    sims_ran: bool = False
    leg_stats: dict = field(default_factory=dict)


class Analyzer:
    def __init__(self, bots: list[BotSpec], cache: SeriesCache, analysis_cfg, dq_cfg) -> None:
        self.bots = {b.key: b for b in bots}
        self.cache = cache
        self.cfg = analysis_cfg
        self.dq = dq_cfg
        self.sims: dict[tuple[str, str], object] = {}
        self.last_sim_at = 0.0

    def gap_threshold(self, nominal: float) -> float:
        return float(self.dq.gap_factor) * max(nominal, 1.0)

    def symbol_quality(self, series: Series, live: tuple[str, str], now: float) -> SymbolQuality:
        live_status, live_detail = live
        nominal = series.nominal_interval()
        thr = self.gap_threshold(nominal)
        q = SymbolQuality(series.symbol, CLEAN, "clean", live_status, series.n, nominal, thr)
        window = float(self.dq.clean_window_minutes) * 60
        recent = series.epochs[series.epochs >= now - window]
        q.window_coverage = float(recent.size * nominal / window) if window > 0 else 0.0
        if recent.size >= 2:
            q.recent_gaps = int(np.count_nonzero(np.diff(recent) > thr))
        cw = int(self.dq.chi2_window)
        if series.n >= cw:
            _, q.chi2_p, _ = st.chi2_uniform(series.digits[-cw:])
            q.chi2_n = cw
        if live_status == LIVE_FAULT:
            q.status, q.detail = FAULT, live_detail
        elif live_status != LIVE_OK:
            q.status, q.detail = DEGRADED, f"live feed: {live_detail}"
        elif q.recent_gaps:
            q.status, q.detail = DEGRADED, f"{q.recent_gaps} tick gap(s) > {thr:.0f}s in last {window / 60:.0f} min"
        elif q.window_coverage < 0.8:
            q.status, q.detail = DEGRADED, (f"only {100 * min(q.window_coverage, 1):.0f}% of expected ticks "
                                            f"in last {window / 60:.0f} min")
        return q

    def evaluate(self, combos: list[tuple[str, str]], compat: dict, markets: dict, payouts: dict,
                 live: dict[str, tuple[str, str]], now: float | None = None) -> EvalResult:
        t0 = time.time()
        now = now or t0
        symbols = sorted({s for _, s in combos})
        by_symbol: dict[str, list[str]] = {}
        for bkey, sym in combos:
            by_symbol.setdefault(sym, []).append(bkey)
        quality: dict[str, SymbolQuality] = {}
        stats: dict[tuple[str, str], LegStats] = {}

        # ---- optional sequential simulation (streak analysis), throttled
        run_sims = now - self.last_sim_at >= float(self.cfg.simulation_interval_minutes) * 60
        if run_sims:
            self.last_sim_at = now

        # ---- replay statistics, one symbol at a time, once per (symbol, identical leg).
        # The per-event arrays (up to ~3.6 MB per leg on 400k ticks) are only needed for this
        # symbol's simulation, so they are released before the next symbol: keeping all of them
        # for 20 symbols x 15 legs peaked at ~1.1 GB (measured), too much for a 2 GB VPS.
        for sym in symbols:
            series = self.cache.get(sym)
            quality[sym] = self.symbol_quality(series, live.get(sym, ("STALE", "not subscribed")), now)
            seg = series.segments(quality[sym].gap_threshold)
            trig_cache: dict = {}
            arrays: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = {}
            for bkey in by_symbol[sym]:
                for leg in self.bots[bkey].legs:
                    k = (sym, leg.stat_key)
                    if k in stats:
                        continue
                    ls, ev, wins = compute_leg_stats(series, seg, leg, self.cfg, trigger_cache=trig_cache,
                                                     latency_check=bool(self.cfg.latency_robustness))
                    stats[k] = ls
                    if run_sims:
                        arrays[k] = (ev, wins)
            if run_sims:
                for bkey in by_symbol[sym]:
                    bot = self.bots[bkey]
                    try:
                        start = max(0, series.n - int(self.cfg.simulation_max_ticks))
                        leg_data = {leg.id: arrays[(sym, leg.stat_key)] for leg in bot.legs}
                        sim = simulate_bot(bot, leg_data, start, int(self.cfg.next_trade_gap_ticks))
                        sim.ticks = series.n - start
                        self.sims[(bkey, sym)] = sim
                    except Exception:  # pragma: no cover - never let the simulation break evaluation
                        log.exception("event=simulation_failed bot=%s symbol=%s", bkey, sym)
            del arrays, trig_cache, seg, series

        # ---- break-even, binomial tests, Benjamini–Hochberg across every live test
        family: list[tuple[tuple[str, str], float]] = []
        leg_payout: dict[tuple[str, str], object] = {}
        for bkey, sym in combos:
            for leg in self.bots[bkey].legs:
                k = (sym, leg.stat_key)
                q = payouts.get((sym, leg.contract_type, "" if leg.barrier is None else str(leg.barrier),
                                 leg.duration_ticks))
                leg_payout[k] = q
                ls = stats[k]
                if q is not None and q.break_even is not None:
                    ls.p_be = q.break_even
                    if ls.disc_n:
                        ls.disc_p = st.binom_p_greater(ls.disc_k, ls.disc_n, q.break_even)
                    if ls.oos_n:
                        ls.oos_p = st.binom_p_greater(ls.oos_k, ls.oos_n, q.break_even)
        seen = set()
        for k, ls in stats.items():
            if ls.disc_p is not None and k not in seen:
                seen.add(k)
                family.append((k, ls.disc_p))
        if family:
            qv = st.benjamini_hochberg([p for _, p in family])
            for (k, _), qval in zip(family, qv):
                stats[k].q_value = float(qval)

        verdicts: dict[tuple[str, str], BotVerdict] = {}
        for bkey, sym in combos:
            bot = self.bots[bkey]
            c = compat.get((bkey, sym))
            m = markets.get(sym)
            legs = []
            for leg in bot.legs:
                k = (sym, leg.stat_key)
                ls = stats[k]
                status, reasons, checks = evaluate_leg(
                    ls, quality=quality[sym], payout=leg_payout.get(k), contract_ok=(c is not None and c.compatible),
                    contract_reason=(c.reason_text if c else "not checked"), cfg=self.cfg, dq_cfg=self.dq)
                legs.append(LegVerdict(leg.id, leg.label, leg.duration_ticks,
                                       leg.trigger_text or describe_trigger(leg.trigger), status, reasons, checks,
                                       ls, leg_payout.get(k)))
            status = combine_bot(legs)
            critical_kind = None
            if not (c and c.compatible):
                critical_kind = "contract"
            elif quality[sym].status == FAULT:
                critical_kind = "data"
            critical = critical_kind is not None
            reasons = [f"{lv.leg_id}: {r}" for lv in legs for r in lv.reasons] if len(legs) > 1 else \
                list(legs[0].reasons)
            verdicts[(bkey, sym)] = BotVerdict(
                bot=bkey, bot_name=bot.name, symbol=sym, symbol_name=(m.display_name if m else sym),
                status=status, critical=critical, reasons=reasons, legs=legs, quality=quality[sym],
                compatible=bool(c and c.compatible), compat_text=(c.reason_text if c else "not checked"),
                sim=self.sims.get((bkey, sym)), evaluated_at=now, critical_kind=critical_kind,
                hold=bot_hold(legs))
        return EvalResult(verdicts, quality, time.time() - t0, len(family), run_sims)

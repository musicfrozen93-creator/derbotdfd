"""Bot-specific evaluation of every bot x market combination, in five separate parts.

A. ENTRY       - did the bot's own entry rule fire on the ticks since the last check?
B. STATISTICS  - outcomes of the bot's OWN entry ticks, settled on the correct tick, recent window and
                 long window, against the fair probability (Wilson interval)
C. ECONOMICS   - live quote at the bot's real stake -> net win / net loss / break-even / EV
D. RISK        - can the bot's own recovery sequence be executed (payout cap, min stake, bankroll)?
E. DECISION    - RUN_CANDIDATE only when every required gate passes; AVOID (critical) when the contract is
                 unavailable, the feed is down, or risk is UNSAFE / BLOCKED

Statuses
* PASS / FAIL       - the check ran and passed / failed
* NOT_CHECKED       - not enough data to run the check reliably (never a PASS)
* UNAVAILABLE       - the contract is not offered on this market
* UNSUPPORTED       - the bot's rule could not be reproduced (none of the 10 bots needs this today)
* economics: POSITIVE / NEGATIVE / INCONCLUSIVE / NOT_CHECKED / UNKNOWN (see app.economics.ev)
* risk: SAFE / UNSAFE / BLOCKED / UNKNOWN (see app.risk.engine)
UNKNOWN never counts as PASS. The legacy "every winning digit > 10 % of 500 ticks" rule is still
computed and shown, labelled informational; it does not gate anything.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from fractions import Fraction

import numpy as np

from app.analytics.validation import (AVOID, CLEAN, DEGRADED, FAULT, INSUFFICIENT, RUN, WAIT, SymbolQuality,
                                      check_rule, digit_counts, pct)
from app.economics import ev as E
from app.economics.quotes import QuoteBook, QuoteKey, QuoteNeed
from app.market.health import FAULT as LIVE_FAULT
from app.market.health import OK as LIVE_OK
from app.risk import engine as R
from app.strategy_model.bots import BotModel
from app.strategy_model.entry import entry_mask, prices_as_doubles
from app.strategy_model.outcomes import WindowStats, gap_ok_steps, settle, window_stats, z_two_sided

PASS, FAIL, NOT_CHECKED, UNAVAILABLE, UNSUPPORTED = "PASS", "FAIL", "NOT_CHECKED", "UNAVAILABLE", "UNSUPPORTED"
GATES = ("entry", "statistics", "economics", "risk")


@dataclass(frozen=True)
class EvalConfig:
    window_ticks: int = 500
    long_window_ticks: int = 100_000
    confidence: float = 0.99
    min_outcomes: int = 100
    entry_gap_ticks: int = 1
    interval_s: float = 60
    gap_factor: float = 10
    start_requires: tuple[str, ...] = GATES
    max_quote_age_s: float = 24 * 3600
    legacy_threshold_pct: Fraction = Fraction(10)

    @classmethod
    def from_settings(cls, s) -> "EvalConfig":
        ev, an, dq, sr, ec = s.evaluation, s.analysis, s.data_quality, s.signal_rule, s.economics
        req = tuple(str(x).lower() for x in ev.start_requires)
        bad = [g for g in req if g not in GATES]
        if bad:
            raise ValueError(f"evaluation.start_requires: unknown gate(s) {bad}; allowed {GATES}")
        if "risk" not in req:
            raise ValueError("evaluation.start_requires must include 'risk' (a START never ignores risk)")
        conf = float(ev.confidence)
        if not 0.5 < conf < 1:
            raise ValueError("evaluation.confidence must be between 0.5 and 1")
        return cls(int(sr.window_ticks), max(int(ev.long_window_ticks), int(sr.window_ticks)), conf,
                   int(ev.min_outcomes), int(ev.entry_gap_ticks), float(an.interval_s), float(dq.gap_factor), req,
                   float(ec.max_quote_age_hours) * 3600, Fraction(str(sr.threshold_pct)))


@dataclass
class EntryResult:
    status: str
    leg_id: str
    rule: str
    evidence: str
    fired_last_interval: int = 0
    ticks_last_interval: int = 0
    fires_on_last_tick: bool | None = None
    trigger_rate_recent: float | None = None
    trigger_rate_long: float | None = None
    fair_rate: float | None = None
    detail: str = ""


@dataclass
class LegResult:
    leg_id: str
    label: str
    contract_type: str
    barrier: int | None
    duration: int
    used_when: str
    entry_rule: str
    stake: float
    recent: WindowStats | None
    long: WindowStats | None
    statistics_status: str
    statistics_detail: str
    economics: E.LegEconomics | None
    legacy_rule: dict = field(default_factory=dict)


@dataclass
class StrategyVerdict:
    bot: str
    bot_name: str
    symbol: str
    symbol_name: str
    status: str = WAIT
    critical: bool = False
    critical_kind: str | None = None          # data | contract | risk
    hold: bool = False
    reasons: list[str] = field(default_factory=list)
    quality: SymbolQuality | None = None
    compatible: bool = True
    compat_text: str = ""
    entry: EntryResult | None = None
    legs: list[LegResult] = field(default_factory=list)
    statistics_status: str = NOT_CHECKED
    economics_status: str = E.UNKNOWN
    economics_detail: str = ""
    risk: R.RiskResult | None = None
    gates: dict = field(default_factory=dict)
    window_ticks: int = 0
    long_window_ticks: int = 0
    evaluated_at: float = 0.0
    confidence: float = 0.0                   # interval level used for statistics and economics

    @property
    def entry_status(self) -> str:
        return self.entry.status if self.entry else NOT_CHECKED

    @property
    def risk_status(self) -> str:
        return self.risk.status if self.risk else R.UNKNOWN

    def status_tuple(self) -> tuple:
        return (self.status, self.entry_status, self.statistics_status, self.economics_status, self.risk_status)

    def gates_text(self) -> str:
        return " ".join(f"{k}={g['status']}{'' if g['pass'] else '!' if g['required'] else '?'}"
                        for k, g in self.gates.items())

    def summary(self) -> dict:
        d = {"model": "strategy_v1", "status": self.status, "critical": self.critical,
             "critical_kind": self.critical_kind, "hold": self.hold, "reasons": self.reasons,
             "compatible": self.compatible, "compat": self.compat_text, "gates": self.gates,
             "entry": None if self.entry is None else asdict(self.entry),
             "statistics_status": self.statistics_status, "economics_status": self.economics_status,
             "economics_detail": self.economics_detail,
             "legs": [], "risk": None if self.risk is None else self.risk.to_dict(),
             "window_ticks": self.window_ticks, "long_window_ticks": self.long_window_ticks,
             "confidence": self.confidence,
             "quality": None if self.quality is None else self.quality.to_dict()}
        for lg in self.legs:
            x = {k: v for k, v in asdict(lg).items() if k not in ("recent", "long", "economics")}
            x["recent"] = None if lg.recent is None else lg.recent.to_dict()
            x["long"] = None if lg.long is None else lg.long.to_dict()
            x["economics"] = None if lg.economics is None else lg.economics.to_dict()
            d["legs"].append(x)
        if self.risk is not None:          # keep the stored summary compact: SAFE ladders keep the first two
            for lad in d["risk"]["ladders"]:   # trades (next stake after a loss) and the last one
                if lad["status"] == R.SAFE and len(lad["steps"]) > 3:
                    lad["steps"] = lad["steps"][:2] + lad["steps"][-1:]
        return d


@dataclass
class StrategyEvalResult:
    verdicts: dict
    quality: dict
    duration_s: float
    tests: int


class StrategyAnalyzer:
    def __init__(self, specs: dict, models: dict[str, BotModel], cache, cfg: EvalConfig, book: QuoteBook,
                 risk_settings) -> None:
        self.specs = specs
        self.models = models
        self.cache = cache
        self.cfg = cfg
        self.book = book
        self.risk_settings = risk_settings      # Section: bankroll_usd, bankroll_per_bot, base_stakes, overrides
        self._z2 = z_two_sided(cfg.confidence)
        self._ladder_cache: dict[tuple, list] = {}

    # ------------------------------------------------------------- configuration helpers
    def base_stake(self, bot: str) -> float:
        v = (self.risk_settings.base_stakes or {}).get(bot)
        return self.models[bot].default_base_stake if v is None else float(v)

    def ladders(self, bot: str) -> list:
        """The bot's losing-streak ladders at its configured base stake (pure function of the XML
        model, so cached per base stake / step limit)."""
        key = (bot, self.base_stake(bot), int(self.risk_settings.max_ladder_steps))
        if key not in self._ladder_cache:
            self._ladder_cache[key] = self.models[bot].ladders(key[1], key[2])
        return self._ladder_cache[key]

    def risk_config(self, bot: str) -> R.RiskConfig:
        rs = self.risk_settings
        per = (rs.bankroll_per_bot or {}).get(bot)
        bank = per if per is not None else rs.bankroll_usd
        return R.RiskConfig(bankroll=None if bank is None else float(bank),
                            max_payout_override=None if rs.max_payout_override is None else float(rs.max_payout_override),
                            min_stake_override=None if rs.min_stake_override is None else float(rs.min_stake_override),
                            max_quote_age_s=self.cfg.max_quote_age_s, max_steps=int(rs.max_ladder_steps))

    def leg_stakes(self, bot: str) -> dict[str, float]:
        """Stake of each leg's first trade on a fresh run (the stake its economics are quoted at)."""
        m = self.models[bot]
        out = {}
        for lad in self.ladders(bot):
            for st in lad.steps:
                out.setdefault(st.leg_id, st.amount)
        for leg in m.legs:
            out.setdefault(leg.id, self.base_stake(bot))
        return out

    def quote_needs(self, combos: list[tuple[str, str]]) -> list[QuoteNeed]:
        needs, seen = [], set()
        for bot, sym in combos:
            m = self.models.get(bot)
            if m is None:
                continue
            stakes = self.leg_stakes(bot)
            for leg in m.legs:
                k = QuoteKey(sym, leg.contract_type, leg.barrier, leg.duration, stakes[leg.id])
                if k.db not in seen:
                    seen.add(k.db)
                    needs.append(QuoteNeed(0, k, f"{bot} {leg.id} economics"))
            for prio, k in R.ladder_quote_needs(m, sym, self.book, self.base_stake(bot),
                                                int(self.risk_settings.max_ladder_steps), ladders=self.ladders(bot)):
                if k.db not in seen:
                    seen.add(k.db)
                    needs.append(QuoteNeed(prio, k, f"{bot} ladder trade {prio}"))
        return needs

    # ------------------------------------------------------------------ evaluation
    def symbol_quality(self, series, live: tuple[str, str]) -> SymbolQuality:
        live_status, live_detail = live
        w = self.cfg.window_ticks
        digits, epochs = series.digits[-w:], series.epochs[-w:]
        q = SymbolQuality(series.symbol, CLEAN, "live feed OK", live_status, int(digits.shape[0]),
                          series.nominal_interval(), digit_counts(digits),
                          int(epochs[0]) if epochs.size else 0, int(epochs[-1]) if epochs.size else 0)
        if live_status == LIVE_FAULT:
            q.status, q.detail = FAULT, live_detail
        elif live_status != LIVE_OK:
            q.status, q.detail = DEGRADED, f"live feed: {live_detail}"
        return q

    def evaluate(self, combos, compat: dict, markets: dict, live: dict, now: float | None = None) -> StrategyEvalResult:
        t0 = time.time()
        now = now or t0
        quality, ctx = {}, {}
        for sym in sorted({s for _, s in combos}):
            series = self.cache.get(sym)
            quality[sym] = self.symbol_quality(series, live.get(sym, ("STALE", "not subscribed")))
            ctx[sym] = _SymbolContext(series, markets.get(sym), self.cfg)
        verdicts, tests = {}, 0
        for bot, sym in combos:
            v = self._evaluate_combo(bot, sym, compat.get((bot, sym)), markets.get(sym), quality[sym], ctx[sym], now)
            tests += int(quality[sym].n_ticks >= self.cfg.window_ticks)
            verdicts[(bot, sym)] = v
        return StrategyEvalResult(verdicts, quality, time.time() - t0, tests)

    def _evaluate_combo(self, bot, sym, comp, market, q: SymbolQuality, sc: "_SymbolContext", now) -> StrategyVerdict:
        spec, model = self.specs[bot], self.models[bot]
        v = StrategyVerdict(bot, spec.name, sym, market.display_name if market else sym, quality=q,
                            compatible=bool(comp and comp.compatible),
                            compat_text=(comp.reason_text if comp else "not checked"),
                            window_ticks=self.cfg.window_ticks, long_window_ticks=self.cfg.long_window_ticks,
                            evaluated_at=now, confidence=self.cfg.confidence)
        leg_compat = (comp.legs if comp else {}) or {}
        stakes = self.leg_stakes(bot)
        enough = sc.n >= self.cfg.window_ticks
        # ---- A. entry (fresh-run state = the first leg)
        v.entry = sc.entry(model.legs[0], now, self.cfg) if enough else EntryResult(
            NOT_CHECKED, model.legs[0].id, model.legs[0].entry.text, model.legs[0].entry.evidence,
            detail=f"only {sc.n:,} of {self.cfg.window_ticks:,} ticks stored")
        # ---- B + C per leg
        for leg in model.legs:
            lc = leg_compat.get(leg.id, "ok")
            recent = long = None
            st_status, st_detail = NOT_CHECKED, "not enough data"
            if lc != "ok":
                st_status, st_detail = UNAVAILABLE, lc
            elif enough:
                recent, long = sc.stats(leg, self._z2)
                if long is None:
                    st_status, st_detail = NOT_CHECKED, "price precision unknown for this market (SMA not reproducible)"
                else:
                    st_status, st_detail = self._statistics_status(long, leg)
            econ = E.leg_economics(
                leg.id, leg.label, stakes[leg.id],
                self.book.get(QuoteKey(sym, leg.contract_type, leg.barrier, leg.duration, stakes[leg.id])),
                leg.p_fair, long.wins if long else 0, long.settled if long else 0,
                confidence=self.cfg.confidence, min_outcomes=self.cfg.min_outcomes,
                max_age_s=self.cfg.max_quote_age_s, now=now)
            v.legs.append(LegResult(leg.id, leg.label, leg.contract_type, leg.barrier, leg.duration, leg.used_when,
                                    leg.entry.text, stakes[leg.id], recent, long, st_status, st_detail, econ,
                                    self._legacy_rule(leg, q)))
        sts = [lg.statistics_status for lg in v.legs]
        v.statistics_status = (UNAVAILABLE if UNAVAILABLE in sts else FAIL if FAIL in sts else
                               PASS if all(s == PASS for s in sts) else NOT_CHECKED)
        v.economics_status, v.economics_detail = E.combine([lg.economics for lg in v.legs])
        # ---- D. risk
        v.risk = R.evaluate_risk(model, sym, self.book, self.risk_config(bot), self.base_stake(bot), now,
                                 ladders=self.ladders(bot))
        # ---- gates
        v.gates = {
            "entry": {"status": v.entry_status, "pass": v.entry_status == PASS, "detail": v.entry.detail},
            "statistics": {"status": v.statistics_status, "pass": v.statistics_status == PASS,
                           "detail": "; ".join(f"{lg.label}: {lg.statistics_detail}" for lg in v.legs)},
            "economics": {"status": v.economics_status, "pass": v.economics_status == E.POSITIVE,
                          "detail": v.economics_detail},
            "risk": {"status": v.risk_status, "pass": v.risk_status == R.SAFE, "detail": v.risk.reason},
        }
        for k, g in v.gates.items():
            g["required"] = k in self.cfg.start_requires
        # ---- E. decision
        if not v.compatible:
            v.status, v.critical, v.critical_kind = AVOID, True, "contract"
            v.reasons = [f"contract unavailable: {v.compat_text}"]
        elif q.status == FAULT:
            v.status, v.critical, v.critical_kind = AVOID, True, "data"
            v.reasons = [f"live feed down: {q.detail}"]
        elif v.risk_status in (R.UNSAFE, R.BLOCKED):          # does not depend on tick data
            v.status, v.critical, v.critical_kind = AVOID, True, "risk"
            v.reasons = [f"risk {v.risk_status}: {v.risk.reason}"]
        elif not enough:
            v.status = INSUFFICIENT
            v.reasons = [f"only {sc.n:,} of {self.cfg.window_ticks:,} ticks stored for {sym} yet"]
        else:
            failed = [k for k, g in v.gates.items() if g["required"] and not g["pass"]]
            if failed:
                v.status = WAIT
                v.reasons = [f"{k} {v.gates[k]['status']}: {v.gates[k]['detail']}"[:400] for k in failed]
            elif q.status != CLEAN:
                v.status, v.hold = WAIT, True
                v.reasons = [f"all gates pass but the live feed is not current: {q.detail}"]
            else:
                v.status = RUN
                v.reasons = ["every required gate passes: " + ", ".join(k for k in v.gates if v.gates[k]["required"])]
        return v

    def _statistics_status(self, long: WindowStats, leg) -> tuple[str, str]:
        if long.settled < self.cfg.min_outcomes:
            return NOT_CHECKED, f"{long.settled} settled entries (need {self.cfg.min_outcomes})"
        txt = (f"{long.wins}/{long.settled} = {long.win_rate:.2%} (CI {long.ci_low:.2%} - {long.ci_high:.2%}) "
               f"vs fair {leg.p_fair:.0%}")
        if long.ci_high < leg.p_fair:
            return FAIL, "significantly below the fair probability: " + txt
        return PASS, "no significant shortfall: " + txt

    def _legacy_rule(self, leg, q: SymbolQuality) -> dict:
        n = q.n_ticks
        win = list(leg.win_digits)
        if n < self.cfg.window_ticks:
            return {"result": NOT_CHECKED, "informational": True}
        ok, failing = check_rule(q.counts, n, win, self.cfg.legacy_threshold_pct)
        return {"result": PASS if ok else FAIL, "informational": True, "winning_digits": win,
                "digit_pct": {str(d): round(pct(q.counts[d], n), 2) for d in win}, "failing_digits": failing,
                "rule": f"every winning digit > {float(self.cfg.legacy_threshold_pct):g}% of the last {n} ticks"}


class _SymbolContext:
    """Per-symbol arrays and memoised masks / outcomes for one evaluation cycle."""

    def __init__(self, series, market, cfg: EvalConfig) -> None:
        self.series = series
        self.n = int(series.n)
        self.cfg = cfg
        self.decimals = None if market is None else market.pip_decimals
        self.digits = series.digits
        self.epochs = series.epochs
        self.prices = (prices_as_doubles(series.prices, self.decimals) if self.decimals is not None
                       and self.n else None)
        thr = cfg.gap_factor * max(series.nominal_interval(), 1.0)
        self.gap_ok = gap_ok_steps(self.epochs, thr)
        self._masks: dict = {}
        self._outcomes: dict = {}

    def mask(self, rule):
        if rule.key not in self._masks:
            if rule.kind == "price_ge_sma" and self.prices is None:
                self._masks[rule.key] = (None, 0)
            else:
                self._masks[rule.key] = entry_mask(rule, self.digits, self.prices)
        return self._masks[rule.key]

    def outcomes(self, leg):
        key = (leg.entry.key, leg.contract_type, leg.barrier, leg.duration)
        if key not in self._outcomes:
            m, first = self.mask(leg.entry)
            self._outcomes[key] = None if m is None else (m, first, *settle(
                m, self.digits, self.gap_ok, leg.duration, self.cfg.entry_gap_ticks, leg.win_digits))
        return self._outcomes[key]

    def stats(self, leg, z2: float):
        o = self.outcomes(leg)
        if o is None:
            return None, None
        m, first, idx, s, w = o
        recent = window_stats(idx, s, w, self.n - self.cfg.window_ticks, self.n, m, first, leg.p_fair, z2)
        long = window_stats(idx, s, w, max(0, self.n - self.cfg.long_window_ticks), self.n, m, first, leg.p_fair, z2)
        return recent, long

    def entry(self, leg, now: float, cfg: EvalConfig) -> EntryResult:
        rule = leg.entry
        m, first = self.mask(rule)
        res = EntryResult(NOT_CHECKED, leg.id, rule.text, rule.evidence, fair_rate=rule.fair_rate)
        if m is None:
            res.detail = "price precision unknown for this market (SMA cannot be reproduced)"
            return res
        if self.n <= first:
            res.detail = f"needs {first + 1} ticks of history"
            return res
        recent_start = max(first, self.n - cfg.window_ticks)
        long_start = max(first, self.n - cfg.long_window_ticks)
        res.trigger_rate_recent = float(m[recent_start:].mean()) if self.n > recent_start else None
        res.trigger_rate_long = float(m[long_start:].mean()) if self.n > long_start else None
        res.fires_on_last_tick = bool(m[-1])
        since = now - cfg.interval_s
        lo = int(np.searchsorted(self.epochs, since, side="right"))
        lo = max(lo, first)
        res.ticks_last_interval = max(0, self.n - lo)
        res.fired_last_interval = int(m[lo:].sum()) if res.ticks_last_interval else 0
        if res.ticks_last_interval == 0:
            res.detail = f"no ticks in the last {cfg.interval_s:g} s"
            return res
        if res.fired_last_interval:
            res.status = PASS
            res.detail = (f"entry rule fired on {res.fired_last_interval} of the last {res.ticks_last_interval} ticks "
                          f"({rule.text})")
        else:
            res.status = FAIL
            res.detail = f"entry rule did not fire on any of the last {res.ticks_last_interval} ticks ({rule.text})"
        return res

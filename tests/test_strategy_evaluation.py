"""Bot-specific evaluation (app.strategy_model.evaluate) on real stored ticks: A entry, B statistics,
C payout economics, D risk and E the decision, each kept separate.

Quotes come from a fake Deriv (tests/strategy_support.py): ``deriv_like_payout`` is the payout formula
validated against live quotes; ``favourable_payout`` is NOT a Deriv price (5 % above fair odds) and is
used only where a test needs the START path.
"""
import json
import time

import numpy as np
import pytest

from app.analytics.analyzer import SeriesCache
from app.analytics.validation import AVOID, DEGRADED, INSUFFICIENT, RUN, WAIT
from app.config import DEFAULTS, Section
from app.deriv.symbols import MarketInfo
from app.economics import ev as E
from app.economics.quotes import QuoteBook
from app.market.health import FAULT as LIVE_FAULT
from app.market.health import STALE as LIVE_STALE
from app.risk import engine as R
from app.storage import repositories as repo
from app.strategies.compatibility import CompatResult
from app.strategy_model.bots import load_models
from app.strategy_model.evaluate import FAIL, NOT_CHECKED, PASS, UNAVAILABLE, EvalConfig, StrategyAnalyzer

from tests.conftest import make_rows
from tests.strategy_support import deriv_like_payout, favourable_payout, fill_quotes, risk_settings

SYM = "R_50"
MARKET = MarketInfo(SYM, "Volatility 50 Index", "synthetic_index", "random_index", 2, True, False)
#               0   1   2   3   4   5   6   7   8   9
ARIA_GOOD = [55, 55, 55, 55, 55, 55, 55, 55, 55, 5]          # Aria's UNDER 9 / UNDER 5 win often
LEGACY_FAIL = [56, 56, 56, 56, 56, 49, 56, 56, 54, 5]        # digit 5 = 9.8 %: legacy rule FAIL for Aria


def compat(bots, sym=SYM, unavailable=None):
    out = {}
    for k, b in bots.items():
        legs = {leg.id: (unavailable or {}).get((k, leg.id), "ok") for leg in b.legs}
        ok = all(v == "ok" for v in legs.values())
        out[(k, sym)] = CompatResult(k, sym, ok, [] if ok else [f"{k}: contract not offered"], legs)
    return out


def digits_with(counts, rng, repeat=1):
    return np.concatenate([rng.permutation(np.repeat(np.arange(10), counts)) for _ in range(repeat)])


class Setup:
    """Ticks stored in SQLite (newest tick 1 s before ``now``), the real SeriesCache, QuoteBook and
    StrategyAnalyzer."""

    def __init__(self, db, bots, digits, *, bankroll=None, quotes=None, base_stakes=None, epochs=None, **cfg):
        conn, path = db
        self.conn = conn
        self.now = float(int(time.time()))
        n = len(digits)
        if n:
            rows = make_rows(SYM, digits, start_epoch=int(self.now) - n)
            if epochs is not None:
                rows = [(s, int(e), q, d) for (s, _, q, d), e in zip(rows, epochs)]
            repo.insert_ticks(conn, rows)
        self.cfg = EvalConfig(**cfg)
        self.bots = bots
        self.models = load_models(list(bots.values()))
        self.book = QuoteBook(conn)
        self.an = StrategyAnalyzer(bots, self.models, SeriesCache(path, self.cfg.long_window_ticks), self.cfg,
                                   self.book, risk_settings(bankroll_usd=bankroll, base_stakes=base_stakes or {}))
        if quotes is not None:
            fill_quotes(self.an, self.book, [(k, SYM) for k in bots], self.now, payout_fn=quotes)

    def run(self, keys=None, live=("OK", "live"), comp=None, market=MARKET):
        combos = [(k, SYM) for k in (keys or self.bots)]
        res = self.an.evaluate(combos, comp or compat(self.bots), {SYM: market}, {SYM: live}, self.now)
        return {k[0]: v for k, v in res.verdicts.items()}


# ---------------------------------------------------------------- C / D unknown is never a pass
def test_unknown_payout_and_unknown_risk_never_pass(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4))               # no quotes, no bankroll
    vs = s.run()
    for k, v in vs.items():
        assert v.status != RUN, k
        assert v.economics_status == E.UNKNOWN and not v.gates["economics"]["pass"]
        assert v.risk_status == R.UNKNOWN and not v.gates["risk"]["pass"]
    a = vs["aria"]
    assert a.status == WAIT and a.entry_status == PASS and a.statistics_status == PASS
    assert any(r.startswith("economics UNKNOWN") for r in a.reasons)
    assert any(r.startswith("risk UNKNOWN") for r in a.reasons)
    assert "no Deriv quote yet" in a.legs[0].economics.detail


def test_bankroll_not_configured_keeps_risk_unknown(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=favourable_payout)
    a = s.run(["aria"])["aria"]
    assert a.risk_status == R.UNKNOWN and "bankroll not configured" in a.risk.reason
    assert a.risk.required_bankroll == pytest.approx(521.14, abs=0.01)
    assert a.status == WAIT


# ---------------------------------------------------------------- C: real-like payouts on fair digits
def test_deriv_payouts_are_never_positive_on_fair_digits(db, bots, rng):
    s = Setup(db, bots, rng.integers(0, 10, 6000), quotes=deriv_like_payout, bankroll=100_000)
    vs = s.run()
    for k, v in vs.items():
        assert v.status != RUN, k
        for lg in v.legs:
            e = lg.economics
            assert e.status != E.POSITIVE, (k, lg.leg_id)
            if e.payout is not None:
                assert e.ev_fair < 0                         # the commission: negative at fair odds
                assert e.break_even > lg.long.p_fair if lg.long else True


def test_ev_numbers_follow_the_quote(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=deriv_like_payout, bankroll=100_000)
    e = s.run(["aria"])["aria"].legs[0].economics          # DIGITUNDER 9, stake 1.00
    assert e.payout == pytest.approx(1.09) and e.net_win == pytest.approx(0.09) and e.net_loss == 1.0
    assert e.break_even == pytest.approx(1 / 1.09)
    assert e.ev_fair == pytest.approx(0.9 * 0.09 - 0.1 * 1.0)
    assert e.ev_hat == pytest.approx(e.p_hat * 0.09 - (1 - e.p_hat) * 1.0)


# ---------------------------------------------------------------- D: risk
def test_martingale_breaking_the_payout_cap_is_critical_even_without_ticks(db, bots):
    s = Setup(db, bots, [], quotes=deriv_like_payout, bankroll=100_000)
    vs = s.run()
    for k in ("predator", "predator_v2", "navix", "pulse2", "crysis"):
        v = vs[k]
        assert (v.status, v.critical, v.critical_kind, v.risk_status) == (AVOID, True, "risk", R.UNSAFE), k
        assert "refused" in v.reasons[0] and "payout" in v.reasons[0]
    for k in ("aria", "aria_v2", "delta", "voltrix", "zony_ai"):     # own stop before the cap: no ticks yet
        assert vs[k].status == INSUFFICIENT and not vs[k].critical, k
        assert vs[k].risk_status == R.SAFE, k


def test_first_trade_refused_is_blocked(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=deriv_like_payout, bankroll=100_000,
              base_stakes={"aria": 0.2})                              # below Deriv's minimum stake 0.35
    a = s.run(["aria"])["aria"]
    assert (a.status, a.critical_kind, a.risk_status) == (AVOID, "risk", R.BLOCKED)
    assert "below the minimum stake" in a.risk.reason


def test_bankroll_too_small_is_unsafe(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=favourable_payout, bankroll=300)
    a = s.run(["aria"])["aria"]
    assert (a.status, a.critical_kind, a.risk_status) == (AVOID, "risk", R.UNSAFE)
    assert "exceeds the balance left" in a.risk.reason


# ---------------------------------------------------------------- E: all gates pass
def test_all_required_gates_pass_gives_run_candidate(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=favourable_payout, bankroll=100_000)
    a = s.run(["aria"])["aria"]
    assert a.status == RUN and not a.critical and not a.hold
    assert a.status_tuple() == (RUN, PASS, PASS, E.POSITIVE, R.SAFE)
    assert all(g["pass"] and g["required"] for g in a.gates.values())
    assert a.reasons == ["every required gate passes: entry, statistics, economics, risk"]
    json.dumps(a.summary(), default=str)                               # stored as JSON


def test_legacy_digit_rule_is_informational_only(db, bots, rng):
    s = Setup(db, bots, digits_with(LEGACY_FAIL, rng, 1), quotes=favourable_payout, bankroll=100_000)
    a = s.run(["aria"])["aria"]
    assert a.legs[0].legacy_rule["result"] == FAIL and a.legs[0].legacy_rule["informational"] is True
    assert a.legs[0].legacy_rule["failing_digits"] == [5]
    assert a.status == RUN                                              # does not gate anything


def test_optional_gate_is_shown_but_not_required(db, bots, rng):
    s = Setup(db, bots, rng.integers(0, 10, 6000), quotes=deriv_like_payout, bankroll=100_000,
              start_requires=("entry", "statistics", "risk"))
    a = s.run(["aria"])["aria"]
    assert a.economics_status != E.POSITIVE and not a.gates["economics"]["required"]
    assert a.status == RUN
    assert "economics=" in a.gates_text() and "?" in a.gates_text()


def test_start_requires_must_include_risk():
    for bad in (["entry", "statistics", "economics"], ["entry", "risk", "luck"]):
        s = Section({**DEFAULTS, "evaluation": {**DEFAULTS["evaluation"], "start_requires": bad}})
        with pytest.raises(ValueError):
            EvalConfig.from_settings(s)
    assert EvalConfig.from_settings(Section(DEFAULTS)).start_requires == ("entry", "statistics", "economics", "risk")


# ---------------------------------------------------------------- A / B
def test_statistics_fail_when_outcomes_are_significantly_below_fair(db, bots, rng):
    s = Setup(db, bots, digits_with([10] * 9 + [410], rng, 2), quotes=favourable_payout, bankroll=100_000)
    a = s.run(["aria"])["aria"]
    assert a.statistics_status == FAIL and a.legs[0].statistics_status == FAIL
    assert a.status == WAIT and not a.critical
    assert "significantly below the fair probability" in a.legs[0].statistics_detail
    assert a.economics_status == E.NEGATIVE


def test_entry_rule_is_the_bots_own(db, bots, rng):
    base = rng.integers(0, 10, 3000)
    tail = rng.choice([0, 2, 3, 4, 5, 6, 7, 8, 9], 70)                 # no digit 1 in the last 70 s
    s = Setup(db, bots, np.concatenate([base, tail]), quotes=favourable_payout, bankroll=100_000)
    d = s.run(["delta"])["delta"]
    assert d.entry_status == FAIL and "did not fire" in d.entry.detail and "last digit = 1" in d.entry.rule
    assert d.status == WAIT
    a = s.run(["aria"])["aria"]                                        # Aria buys on every tick
    assert a.entry_status == PASS and a.entry.fired_last_interval == a.entry.ticks_last_interval


def test_insufficient_data_is_never_a_start(db, bots, rng):
    s = Setup(db, bots, digits_with([20] * 10, rng, 1), quotes=favourable_payout, bankroll=100_000)
    a = s.run(["aria"])["aria"]
    assert a.status == INSUFFICIENT and a.entry_status == NOT_CHECKED and a.statistics_status == NOT_CHECKED


def test_too_few_bot_entries_is_not_checked(db, bots, rng):
    s = Setup(db, bots, rng.integers(0, 10, 600), quotes=favourable_payout, bankroll=100_000)
    c = s.run(["crysis"])["crysis"]                                    # pattern fires on ~9 % of ticks
    assert c.legs[0].long.settled < 100
    assert c.statistics_status == NOT_CHECKED and c.economics_status == E.NOT_CHECKED
    assert c.status != RUN


def test_contracts_across_a_tick_gap_are_not_settled(db, bots, rng):
    d = rng.integers(0, 10, 1000)
    now = int(time.time())
    epochs = list(range(now - 2000, now - 1500)) + list(range(now - 500, now))   # a 1,000 s hole
    s = Setup(db, bots, d, epochs=epochs, quotes=favourable_payout, bankroll=100_000)
    a = s.run(["aria"])["aria"]
    assert a.legs[0].long.settled == 998          # 1000 entries - the last (not settled yet) - the one across the gap


def test_sma_rule_without_price_precision_is_not_checked(db, bots, rng):
    s = Setup(db, bots, rng.integers(0, 10, 2000), quotes=favourable_payout, bankroll=100_000)
    p = s.run(["predator"], market=None)["predator"]
    assert p.entry_status == NOT_CHECKED and p.statistics_status == NOT_CHECKED
    assert "price precision unknown" in p.entry.detail


# ---------------------------------------------------------------- feed / contract faults
def test_feed_fault_is_a_critical_data_stop(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=favourable_payout, bankroll=100_000)
    a = s.run(["aria"], live=(LIVE_FAULT, "no tick for 300 s"))["aria"]
    assert (a.status, a.critical, a.critical_kind) == (AVOID, True, "data")


def test_degraded_feed_holds_instead_of_run(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=favourable_payout, bankroll=100_000)
    a = s.run(["aria"], live=(LIVE_STALE, "late"))["aria"]
    assert a.quality.status == DEGRADED and a.status == WAIT and a.hold and not a.critical


def test_unavailable_contract_is_a_critical_contract_stop(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=favourable_payout, bankroll=100_000)
    comp = compat(bots, unavailable={("aria", "recovery"): "DIGITUNDER 5 1t: barrier 5 not offered"})
    a = s.run(["aria"], comp=comp)["aria"]
    assert (a.status, a.critical, a.critical_kind) == (AVOID, True, "contract")
    assert a.legs[1].statistics_status == UNAVAILABLE and a.statistics_status == UNAVAILABLE


# ---------------------------------------------------------------- quotes the evaluation asks for
def test_quote_needs_cover_ev_stakes_and_the_losing_streak(db, bots):
    s = Setup(db, bots, [])
    needs = s.an.quote_needs([("aria", SYM)])
    first = [(n.priority, n.key.contract_type, n.key.barrier, n.key.stake) for n in needs]
    assert (0, "DIGITUNDER", 9, 1.0) in first and (0, "DIGITUNDER", 5, 2.1) in first
    fill_quotes(s.an, s.book, [("aria", SYM)], s.now)                   # ladders advance as quotes arrive
    stakes = sorted({n.key.stake for n in s.an.quote_needs([("aria", SYM)])})
    assert {1.0, 2.1, 4.41, 9.26, 19.45, 40.84, 85.76, 180.1, 378.21} <= set(stakes)

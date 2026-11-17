"""Signal rule "Manual Conditions", mode Individual, threshold 10 %.

START needs EVERY winning digit of the bot's contracts STRICTLY above 10 % of the last 500 ticks
of that market; one winning digit at 10.00 % or lower fails the condition.

Numbered as in the task's testing requirements:
 1 all winning digits above 10 % -> PASS
 2 one winning digit below 10 % -> FAIL
 3 one winning digit exactly 10 % -> FAIL
 4 several winning digits below 10 % -> FAIL
 5 winning digits for every supported contract type
 6 individual evaluation per bot and per market
 7 START notification on PASS
 8 STOP notification when a previously passing condition fails
 9 no duplicate notifications
10 non-Continuous Indices are excluded
"""
import asyncio
import time
from fractions import Fraction

import numpy as np
import pytest

from app.analytics.analyzer import Analyzer, SeriesCache
from app.analytics.validation import (AVOID, FAULT, INSUFFICIENT, RUN, WAIT, RuleConfig, SymbolQuality,
                                      check_rule, digit_counts, digit_passes, evaluate_combo, rule_legs)
from app.config import DEFAULTS, Section, load_settings
from app.deriv.contracts import DigitContract, SymbolContracts
from app.deriv.symbols import MarketInfo
from app.storage import repositories as repo
from app.strategies.compatibility import check
from app.strategies.loader import win_digits

from tests.conftest import make_rows
from tests.test_discovery import (ALLOWED, DIGIT_SYMBOLS, EXCLUDED_WITH_DIGITS, FOREX, NO_DIGITS,  # noqa: F401
                                  FakeDeriv, app_factory)
from tests.test_signals import make_engine, notes

TEN = Fraction(10)
RULE = RuleConfig.from_settings(Section(DEFAULTS).signal_rule)


def counts_for(pcts: dict[int, float], n: int = 500) -> list[int]:
    """Digit counts for an n-tick window with the given percentages; the rest is spread over
    the other digits."""
    c = [0] * 10
    for d, p in pcts.items():
        c[d] = round(p * n / 100)
    rest = [d for d in range(10) if d not in pcts]
    left = n - sum(c)
    for i, d in enumerate(rest):
        c[d] = left // len(rest) + (1 if i < left % len(rest) else 0)
    assert sum(c) == n
    return c


def quality(counts, sym="1HZ10V", status="CLEAN", detail="live feed OK", n=None):
    return SymbolQuality(sym, status, detail, "OK" if status == "CLEAN" else status,
                         sum(counts) if n is None else n, 1.0, list(counts))


def judge(bots, bot, counts, sym="1HZ10V", **kw):
    return evaluate_combo(bots[bot], sym, sym, quality(counts, sym, **kw), compatible=True, compat_text="ok",
                          rule=RULE)


# --------------------------------------------------------------- configuration
def test_shipped_rule_is_individual_10_percent_500_ticks_all_contracts():
    r = RuleConfig.from_settings(load_settings().signal_rule)          # the production config.yaml
    assert (r.mode, r.threshold_pct, r.window_ticks, r.contracts) == ("individual", Fraction(10), 500, "all")
    assert RULE == r                                                     # DEFAULTS agree with config.yaml


@pytest.mark.parametrize("bad", [dict(mode="combined"), dict(contracts="some"), dict(window_ticks=5),
                                 dict(threshold_pct=100), dict(threshold_pct=-1)])
def test_invalid_rule_settings_are_rejected(bad):
    base = dict(mode="individual", threshold_pct=10, window_ticks=500, contracts="all") | bad
    with pytest.raises(ValueError):
        RuleConfig.from_settings(Section(base))


# ------------------------------------------------------------ 1-4 PASS / FAIL
def test_1_all_winning_digits_above_10_percent_pass(bots):
    # Predator: DIGITOVER 3 -> winning digits 4-9
    c = counts_for({4: 11.2, 5: 10.8, 6: 12.2, 7: 10.4, 8: 11.4, 9: 10.2})
    v = judge(bots, "predator", c)
    [cr] = v.contracts
    assert v.status == RUN and cr.result == "PASS" and cr.failing_digits == [] and not v.critical
    assert cr.winning_digits == [4, 5, 6, 7, 8, 9]
    assert cr.digit_pct == pytest.approx({4: 11.2, 5: 10.8, 6: 12.2, 7: 10.4, 8: 11.4, 9: 10.2})
    assert check_rule(c, 500, [4, 5, 6, 7, 8, 9], TEN) == (True, [])


def test_1_just_above_10_percent_passes(bots):
    v = judge(bots, "voltrix", counts_for({8: 10.2, 9: 10.2}))     # 51 of 500 = 10.20 %
    assert v.status == RUN


def test_2_one_winning_digit_below_10_percent_fails(bots):
    c = counts_for({4: 11.2, 5: 10.8, 6: 12.2, 7: 10.4, 8: 9.8, 9: 10.2})
    v = judge(bots, "predator", c)
    assert v.status == WAIT and v.contracts[0].failing_digits == [8] and not v.critical
    assert v.contracts[0].result == "FAIL"
    assert v.reasons == ["contract DIGITOVER 3 (main) FAIL - digit at or below 10%: 8 = 9.80%"]


def test_3_one_winning_digit_exactly_10_percent_fails(bots):
    c = counts_for({4: 11.2, 5: 10.8, 6: 12.2, 7: 10.0, 8: 11.4, 9: 10.2})
    assert c[7] == 50                                          # exactly 50 of 500 = 10.00 %
    v = judge(bots, "predator", c)
    assert v.status == WAIT and v.contracts[0].failing_digits == [7]
    assert not digit_passes(50, 500, TEN) and digit_passes(51, 500, TEN)
    assert not digit_passes(1, 10, TEN)                        # exact, also for other window sizes
    assert not digit_passes(100, 1000, TEN) and digit_passes(101, 1000, TEN)


def test_3_exact_threshold_is_not_a_float_rounding_case():
    # 0.1 is not exact in binary floating point; the rule must use exact arithmetic
    for n in (10, 30, 70, 500, 1230, 99990):
        assert not digit_passes(n // 10, n, TEN)
        assert digit_passes(n // 10 + 1, n, TEN)
    assert not digit_passes(0, 0, TEN)                         # empty window never passes


def test_4_several_winning_digits_below_10_percent_fail(bots):
    c = counts_for({4: 9.0, 5: 10.8, 6: 8.4, 7: 10.0, 8: 11.4, 9: 10.2})
    v = judge(bots, "predator", c)
    assert v.status == WAIT and v.contracts[0].failing_digits == [4, 6, 7]
    assert v.reasons == ["contract DIGITOVER 3 (main) FAIL - digits at or below 10%: "
                         "4 = 9.00%, 6 = 8.40%, 7 = 10.00%"]


def test_only_winning_digits_count(bots):
    # losing digits 0-3 may be anywhere, even 0 %: Predator still passes on 4-9
    c = [0, 0, 0, 0, 84, 84, 83, 83, 83, 83]
    assert judge(bots, "predator", c).status == RUN
    # Voltrix (OVER 7) ignores 0-7 entirely
    assert judge(bots, "voltrix", [0, 0, 0, 0, 0, 0, 0, 0, 250, 250]).status == RUN
    assert judge(bots, "voltrix", [100, 100, 100, 50, 50, 25, 25, 0, 49, 1]).status == WAIT


# ------------------------------------------------------- 5 winning digits
@pytest.mark.parametrize("ctype,barrier,expected", [
    ("DIGITOVER", 0, [1, 2, 3, 4, 5, 6, 7, 8, 9]), ("DIGITOVER", 3, [4, 5, 6, 7, 8, 9]),
    ("DIGITOVER", 5, [6, 7, 8, 9]), ("DIGITOVER", 7, [8, 9]), ("DIGITOVER", 8, [9]),
    ("DIGITUNDER", 9, [0, 1, 2, 3, 4, 5, 6, 7, 8]), ("DIGITUNDER", 8, [0, 1, 2, 3, 4, 5, 6, 7]),
    ("DIGITUNDER", 5, [0, 1, 2, 3, 4]), ("DIGITUNDER", 1, [0]),
    ("DIGITEVEN", None, [0, 2, 4, 6, 8]), ("DIGITODD", None, [1, 3, 5, 7, 9]),
    ("DIGITMATCH", 4, [4]), ("DIGITDIFF", 4, [0, 1, 2, 3, 5, 6, 7, 8, 9]),
])
def test_5_winning_digits_per_contract_type(ctype, barrier, expected):
    assert win_digits(ctype, barrier) == expected


def test_5_barrier_digit_itself_loses_over_and_under():
    for b in range(10):
        assert b not in win_digits("DIGITOVER", b) and b not in win_digits("DIGITUNDER", b)


# every bot's contracts exactly as in its strategy YAML (= its original XML), each with ITS OWN
# winning digits (never merged)
EXPECTED_CONTRACTS = {
    "aria": [("base", "DIGITUNDER 9", [0, 1, 2, 3, 4, 5, 6, 7, 8]), ("recovery", "DIGITUNDER 5", [0, 1, 2, 3, 4])],
    "aria_v2": [("base", "DIGITUNDER 9", [0, 1, 2, 3, 4, 5, 6, 7, 8]),
                ("recovery", "DIGITUNDER 5", [0, 1, 2, 3, 4])],
    "crysis": [("main", "DIGITOVER 3", [4, 5, 6, 7, 8, 9])],
    "delta": [("main", "DIGITOVER 3", [4, 5, 6, 7, 8, 9])],
    "predator": [("main", "DIGITOVER 3", [4, 5, 6, 7, 8, 9])],
    "predator_v2": [("main", "DIGITOVER 5", [6, 7, 8, 9])],
    "pulse2": [("main", "DIGITEVEN", [0, 2, 4, 6, 8])],
    "voltrix": [("main", "DIGITOVER 7", [8, 9])],
    # Zony AI.xml predictionList = [0, 4, 5, 6, 7, 8]: six barriers, no OVER 1 / 2 / 3
    "zony_ai": [("step1", "DIGITOVER 0", [1, 2, 3, 4, 5, 6, 7, 8, 9]), ("step2", "DIGITOVER 4", [5, 6, 7, 8, 9]),
                ("step3", "DIGITOVER 5", [6, 7, 8, 9]), ("step4", "DIGITOVER 6", [7, 8, 9]),
                ("step5", "DIGITOVER 7", [8, 9]), ("step6", "DIGITOVER 8", [9])],
    "navix": [("base", "DIGITUNDER 8", [0, 1, 2, 3, 4, 5, 6, 7]), ("recovery", "DIGITOVER 2", [3, 4, 5, 6, 7, 8, 9])],
}


def test_5_winning_digits_for_every_contract_of_every_bot(bots):
    assert set(bots) == set(EXPECTED_CONTRACTS)
    for key, bot in bots.items():
        v = judge(bots, key, [50] * 10)
        assert [(c.leg_id, c.label, c.winning_digits) for c in v.contracts] == EXPECTED_CONTRACTS[key], key
        assert [lg.id for lg in rule_legs(bot, "first")] == [EXPECTED_CONTRACTS[key][0][0]], key


def test_5_zony_ai_has_exactly_six_barriers(bots):
    assert [lg.barrier for lg in bots["zony_ai"].legs] == [0, 4, 5, 6, 7, 8]
    assert {lg.contract_type for lg in bots["zony_ai"].legs} == {"DIGITOVER"}


def test_5_navix_with_all_contracts_can_never_pass(bots):
    """UNDER 8 + OVER 2 together win on every digit 0-9; ten digits cannot all be above 10 %
    (they sum to 100 %), so with contracts: all Navix never STARTs. Documented limitation."""
    rng = np.random.default_rng(1)
    for _ in range(200):
        c = list(rng.multinomial(500, rng.dirichlet(np.ones(10))))
        assert judge(bots, "navix", c).status != RUN
    assert judge(bots, "navix", [50] * 10).status == WAIT
    # with contracts: first only UNDER 8 (0-7) is checked, which can pass
    first = RuleConfig(TEN, 500, "first")
    v = evaluate_combo(bots["navix"], "R_100", "R_100", quality([62] * 8 + [2, 2]), compatible=True,
                       compat_text="ok", rule=first)
    assert v.status == RUN and [c.winning_digits for c in v.contracts] == [list(range(8))]


def test_5_rule_legs_are_reported_per_contract(bots):
    v = judge(bots, "aria_v2", [55] * 9 + [5])
    assert [(c.leg_id, c.label, c.winning_digits) for c in v.contracts] == [
        ("base", "DIGITUNDER 9", [0, 1, 2, 3, 4, 5, 6, 7, 8]), ("recovery", "DIGITUNDER 5", [0, 1, 2, 3, 4])]
    assert v.status == RUN                         # digit 9 is a losing digit for Aria V2


# ------------------------------------------------------------ other statuses
def test_window_not_full_is_insufficient(bots):
    v = judge(bots, "predator", [50] * 9 + [49], n=499)
    assert v.status == INSUFFICIENT and not v.critical


def test_contract_withdrawn_is_critical(bots):
    v = evaluate_combo(bots["predator"], "R_50", "R_50", quality(counts_for({}), "R_50"), compatible=False,
                       compat_text="DIGITOVER not offered", rule=RULE)
    assert v.status == AVOID and v.critical and v.critical_kind == "contract"


def test_live_feed_fault_is_critical_and_late_feed_holds(bots):
    c = counts_for({4: 11.2, 5: 10.8, 6: 12.2, 7: 10.4, 8: 11.4, 9: 10.2})
    v = judge(bots, "predator", c, status=FAULT, detail="no ticks for 180 s")
    assert v.status == AVOID and v.critical_kind == "data"
    v = judge(bots, "predator", c, status="DEGRADED", detail="live feed: late")
    assert v.status == WAIT and v.hold                  # never START from a frozen feed, never STOP on it


def test_no_statistical_gate_remains(bots):
    """The old gates (win-rate, break-even, confidence bounds, FDR, chi-square, OOS, stability,
    runs) are gone: a perfectly ordinary window where every winning digit is above 10 % passes."""
    import app.analytics.validation as val
    for name in ("evaluate_leg", "is_data_only_wait", "LegVerdict"):
        assert not hasattr(val, name), name
    for mod in ("app.analytics.statistics", "app.strategies.evaluator"):
        with pytest.raises(ImportError):
            __import__(mod)
    settings = load_settings()
    assert "payout" not in settings.data
    for k in ("confidence", "fdr_q", "oos_alpha", "min_trials_run", "stability_alpha", "runs_alpha"):
        assert k not in dict(settings.analysis), k
    for k in ("chi2_window", "chi2_fault_p", "clean_window_minutes"):
        assert k not in dict(settings.data_quality), k


# ------------------------------------------------- 6 individual per bot x market
def analyzer_setup(db, bots, cfg, digits, symbol="SYN"):
    """Real Analyzer over real stored ticks (newest tick = now)."""
    conn, path = db
    now = int(time.time())
    n = len(digits)
    repo.insert_ticks(conn, make_rows(symbol, digits, start_epoch=now - n))
    an = Analyzer(list(bots.values()), SeriesCache(path, RULE.window_ticks), RULE)
    market = MarketInfo(symbol, "Synthetic", "synthetic_index", "random_index", 2, True, False)
    sc = SymbolContracts(symbol, {c: DigitContract(c, 1, 10, list(range(10)))
                                  for c in ("DIGITUNDER", "DIGITOVER", "DIGITEVEN", "DIGITODD")})
    compat = {(k, symbol): check(b, market, sc) for k, b in bots.items()}
    return an, market, compat, now


def window(counts, rng):
    return rng.permutation(np.repeat(np.arange(10), counts))


def test_6_only_the_newest_500_ticks_are_used(db, bots, cfg, rng):
    old = np.full(5000, 1)                                       # ancient ticks: only digit 1
    new = window([50] * 10, rng)                                  # newest 500: every digit 10.00 %
    an, market, compat, now = analyzer_setup(db, bots, cfg, np.concatenate([old, new]))
    q = an.evaluate([("predator", "SYN")], compat, {"SYN": market}, {"SYN": ("OK", "live")}, now).quality["SYN"]
    assert q.n_ticks == 500 and q.counts == [50] * 10


def test_6_each_bot_is_judged_on_its_own_winning_digits(db, bots, cfg, rng):
    # 4-9 above 10 %, 0-3 below -> OVER bots pass, UNDER / EVEN bots fail
    c = [40, 40, 40, 40, 57, 57, 57, 57, 56, 56]
    an, market, compat, now = analyzer_setup(db, bots, cfg, window(c, rng))
    res = an.evaluate([(b, "SYN") for b in bots], compat, {"SYN": market}, {"SYN": ("OK", "live")}, now)
    status = {b: v.status for (b, _), v in res.verdicts.items()}
    assert status == {"predator": RUN, "predator_v2": RUN, "voltrix": RUN, "delta": RUN, "crysis": RUN,
                      "aria": WAIT, "aria_v2": WAIT, "pulse2": WAIT, "zony_ai": WAIT, "navix": WAIT}
    assert res.verdicts[("pulse2", "SYN")].contracts[0].failing_digits == [0, 2]
    zony = res.verdicts[("zony_ai", "SYN")]
    assert [(c.leg_id, c.result, c.failing_digits) for c in zony.contracts] == [
        ("step1", "FAIL", [1, 2, 3]), ("step2", "PASS", []), ("step3", "PASS", []), ("step4", "PASS", []),
        ("step5", "PASS", []), ("step6", "PASS", [])]
    assert res.tests == 10


def test_6_each_market_is_judged_on_its_own_ticks(db, bots, cfg, rng):
    conn, path = db
    an, market_a, compat, now = analyzer_setup(db, bots, cfg, window([40, 40, 40, 40, 57, 57, 57, 57, 56, 56], rng),
                                               symbol="AAA")
    _, market_b, compat_b, _ = analyzer_setup(db, bots, cfg, window([56, 57, 57, 57, 57, 40, 40, 40, 56, 40], rng),
                                              symbol="BBB")
    compat |= compat_b
    combos = [(b, s) for b in ("predator", "aria") for s in ("AAA", "BBB")]
    res = an.evaluate(combos, compat, {"AAA": market_a, "BBB": market_b},
                      {"AAA": ("OK", "live"), "BBB": ("OK", "live")}, now)
    got = {k: v.status for k, v in res.verdicts.items()}
    assert got == {("predator", "AAA"): RUN, ("predator", "BBB"): WAIT,
                   ("aria", "AAA"): WAIT, ("aria", "BBB"): WAIT}
    assert res.verdicts[("predator", "BBB")].contracts[0].failing_digits == [5, 6, 7, 9]
    aria = res.verdicts[("aria", "BBB")]
    assert [(c.leg_id, c.result, c.failing_digits) for c in aria.contracts] == [
        ("base", "FAIL", [5, 6, 7]), ("recovery", "PASS", [])]


# ------------------------------------------ 7-9 START / STOP / no duplicates
PASS_C = [42, 42, 42, 43, 56, 54, 61, 52, 57, 51]        # Predator 4-9 all > 10 %
FAIL_C = [44, 44, 44, 45, 56, 54, 61, 52, 49, 51]        # digit 8 = 9.80 %


T0 = 1_800_000_000.0


@pytest.fixture
def engine(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg, started_at=T0 - 10_000)

    def predator(counts, sym="1HZ10V"):
        return {("predator", sym): evaluate_combo(bots["predator"], sym, "Volatility 10 (1s) Index",
                                                  quality(counts, sym), compatible=True, compat_text="ok",
                                                  rule=RULE)}
    return conn, eng, predator


def texts(conn, kind):
    return [r[0] for r in conn.execute("SELECT text FROM notifications WHERE kind=? ORDER BY id", (kind,))]


def test_7_start_notification_after_three_passing_checks(engine):
    conn, eng, predator = engine
    assert eng.process(predator(PASS_C), T0) == [] and eng.process(predator(PASS_C), T0 + 60) == []
    tr = eng.process(predator(PASS_C), T0 + 120)
    assert [t.kind for t in tr] == ["START"]
    [text] = texts(conn, "START")
    for needle in ("🟢 START SIGNAL", "Bot: Predator", "Market: Volatility 10 (1s) Index", "Symbol: 1HZ10V",
                   "Contract (main): DIGITOVER, barrier 3, 1 tick", "Winning digits: 4, 5, 6, 7, 8, 9",
                   "• Digit 4: 11.20% ✅", "• Digit 5: 10.80% ✅", "• Digit 6: 12.20% ✅", "• Digit 7: 10.40% ✅",
                   "• Digit 8: 11.40% ✅", "• Digit 9: 10.20% ✅", "Result: PASS ✅",
                   "Signal status: START — 1 of 1 contract PASS (all required)"):
        assert needle in text, needle


def test_8_stop_notification_when_a_passing_condition_fails(engine):
    conn, eng, predator = engine
    for i in range(3):
        eng.process(predator(PASS_C), T0 + i * 60)
    assert eng.process(predator(FAIL_C), T0 + 180) == []          # 1st failing check: confirmation
    assert eng.process(predator(FAIL_C), T0 + 240) == []          # 2nd
    tr = eng.process(predator(FAIL_C), T0 + 300)                  # 3rd -> STOP
    assert [t.kind for t in tr] == ["STOP"] and not tr[0].critical
    [text] = texts(conn, "STOP")
    for needle in ("🔴 STOP SIGNAL", "Bot: Predator", "Market: Volatility 10 (1s) Index", "Symbol: 1HZ10V",
                   "Contract (main): DIGITOVER, barrier 3, 1 tick", "Winning digits: 4, 5, 6, 7, 8, 9",
                   "• Digit 8: 9.80% ❌", "• Digit 9: 10.20% ✅",
                   "Result: FAIL ❌ — digit 8 (9.80%) at or below 10%",
                   "Signal status: STOP — 1 of 1 contract FAILED",
                   "• DIGITOVER 3 (main) — digit 8 (9.80%) at or below 10%"):
        assert needle in text, needle


def test_8_exactly_10_percent_stops_an_active_signal(engine):
    conn, eng, predator = engine
    for i in range(3):
        eng.process(predator(PASS_C), T0 + i * 60)
    exact = [44, 44, 44, 44, 56, 54, 61, 52, 51, 50]            # digit 9 = 50/500 = 10.00 %
    out = []
    for i in range(3):
        out += eng.process(predator(exact), T0 + 180 + i * 60)
    assert [t.kind for t in out] == ["STOP"]
    assert "Signal status: STOP — 1 of 1 contract FAILED" in texts(conn, "STOP")[0]
    assert "• DIGITOVER 3 (main) — digit 9 (10.00%) at or below 10%" in texts(conn, "STOP")[0]
    assert "• Digit 9: 10.00% ❌" in texts(conn, "STOP")[0]


def test_9_no_duplicate_notifications(engine):
    conn, eng, predator = engine
    for i in range(20):                                          # passes for 20 minutes
        eng.process(predator(PASS_C), T0 + i * 60)
    for i in range(20):                                          # then fails for 20 minutes
        eng.process(predator(FAIL_C), T0 + 1200 + i * 60)
    assert notes(conn) == ["START", "STOP"]
    keys = [r[0] for r in conn.execute("SELECT dedupe_key FROM notifications")]
    assert len(keys) == len(set(keys))
    assert not any(k.startswith("WAIT") for k in notes(conn))     # no WAIT notifications, ever


def test_9_inactive_failing_condition_sends_nothing(engine):
    conn, eng, predator = engine
    for i in range(30):
        eng.process(predator(FAIL_C), T0 + i * 60)
    assert notes(conn) == []


# ---------------------------------------------- 10 Continuous Indices only
def test_10_non_continuous_indices_are_never_evaluated_or_signalled(app_factory, monkeypatch, rng):  # noqa: F811
    """Jump, Crash/Boom, Range Break, Daily Reset, Step, basket and forex symbols are rejected
    by discovery; even with passing digits stored for them they get no verdict and no START."""
    client = FakeDeriv(DIGIT_SYMBOLS + NO_DIGITS + FOREX)
    app = app_factory(client)
    asyncio.run(app.discover())
    now = int(time.time())
    passing = window([40, 40, 40, 40, 57, 57, 57, 57, 56, 56], rng)
    excluded = EXCLUDED_WITH_DIGITS + NO_DIGITS + FOREX
    for sym in excluded + list(ALLOWED):
        repo.insert_ticks(app.conn, make_rows(sym, passing, start_epoch=now - 500))
        app.cache.mark_dirty(sym)
    monkeypatch.setattr(app.health, "status", lambda sym, t: ("OK", "live"))
    app.engine.started_at = now - 10_000
    for _ in range(3):
        res = asyncio.run(app.evaluate_once())
    assert {s for _, s in res.verdicts} == set(ALLOWED)
    assert res.verdicts[("predator", "R_50")].status == RUN
    started = {r[0] for r in app.conn.execute("SELECT text FROM notifications WHERE kind='START'")}
    syms = {line.split(": ", 1)[1] for t in started for line in t.splitlines() if line.startswith("Symbol: ")}
    assert syms and syms <= set(ALLOWED)
    assert not syms & set(excluded)
    assert not any(s in excluded for _, s in app.matrix)

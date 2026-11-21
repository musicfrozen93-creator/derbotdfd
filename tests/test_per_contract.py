"""Per-contract evaluation of multi-contract bots.

Every contract of a bot is evaluated on ITS OWN winning digits (never merged with another
contract's), gets its own PASS / FAIL, and the bot passes only when ALL its contracts pass.
START needs 3 consecutive passing checks; STOP needs 3 consecutive failing checks.
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from app.analytics.validation import (AVOID, FAIL, INSUFFICIENT, NOT_CHECKED, PASS, RUN, UNAVAILABLE, WAIT,
                                      RuleConfig, SymbolQuality, evaluate_combo, evaluate_contract)
from app.config import DEFAULTS, Section
from app.signals.telegram import MessageFormatter
from app.storage import repositories as repo

from tests.conftest import make_rows
from tests.strategy_support import make_runnable
from tests.test_digit_rule import analyzer_setup, counts_for, window
from tests.test_discovery import ALLOWED, DIGIT_SYMBOLS, FOREX, NO_DIGITS, FakeDeriv, app_factory  # noqa: F401
from tests.test_signals import make_engine, notes

RULE = RuleConfig.from_settings(Section(DEFAULTS).signal_rule)
T0 = 1_800_000_000.0
ROOT = Path(__file__).resolve().parent.parent

#            0   1   2   3   4   5   6   7   8   9
ARIA_BOTH = [55, 55, 55, 55, 55, 55, 55, 55, 55, 5]      # 0-8 at 11 %: base and recovery PASS
ARIA_REC_FAIL = [54, 56, 56, 49, 56, 56, 56, 56, 56, 5]   # digit 3 = 9.8 %: base FAIL, recovery FAIL
ARIA_BASE_ONLY_FAIL = [56, 56, 56, 56, 56, 49, 56, 56, 54, 5]   # digit 5 = 9.8 %: base FAIL, recovery PASS
ZONY_STEP6_ONLY = [40, 56, 54, 57, 49, 58, 57, 56, 18, 55]      # see test_zony_each_barrier_independent


def q(counts, sym="1HZ10V", status="CLEAN"):
    return SymbolQuality(sym, status, "live feed OK" if status == "CLEAN" else "late", "OK", sum(counts), 1.0,
                         list(counts))


def judge(bots, bot, counts, sym="1HZ10V", **kw):
    return evaluate_combo(bots[bot], sym, "Volatility 10 (1s) Index", q(counts, sym), compatible=True,
                          compat_text="ok", rule=RULE, now=T0, **kw)


def results(v):
    return [(c.leg_id, c.result, c.failing_digits) for c in v.contracts]


# ------------------------------------------------- each contract evaluated independently
def test_each_contract_uses_only_its_own_winning_digits(bots):
    # recovery (UNDER 5 -> 0-4) passes; base (UNDER 9 -> 0-8) fails on digit 5 only
    v = judge(bots, "aria", ARIA_BASE_ONLY_FAIL)
    assert results(v) == [("base", FAIL, [5]), ("recovery", PASS, [])]
    base, rec = v.contracts
    assert set(base.digit_pct) == set(range(9)) and set(rec.digit_pct) == set(range(5))   # own digits only
    assert rec.digit_pct == pytest.approx({0: 11.2, 1: 11.2, 2: 11.2, 3: 11.2, 4: 11.2})
    assert v.status == WAIT                                   # one failing contract: no pass


def test_a_digit_shared_by_two_contracts_fails_both(bots):
    v = judge(bots, "aria", ARIA_REC_FAIL)                    # digit 3 is in base AND recovery
    assert results(v) == [("base", FAIL, [3]), ("recovery", FAIL, [3])]
    assert v.status == WAIT and len(v.failing_contracts) == 2


def test_navix_contracts_are_independent(bots):
    # UNDER 8 (0-7) all above 10 %; OVER 2 (3-9) fails on 8 and 9
    v = judge(bots, "navix", [62, 62, 62, 62, 62, 62, 62, 62, 2, 2])
    assert results(v) == [("base", PASS, []), ("recovery", FAIL, [8, 9])]
    assert v.status == WAIT
    # and the other way round
    v = judge(bots, "navix", [2, 2, 2, 71, 71, 71, 71, 71, 70, 69])
    assert results(v) == [("base", FAIL, [0, 1, 2]), ("recovery", PASS, [])]
    assert v.status == WAIT


def test_zony_each_barrier_independent(bots):
    v = judge(bots, "zony_ai", ZONY_STEP6_ONLY)
    assert [c.label for c in v.contracts] == ["DIGITOVER 0", "DIGITOVER 4", "DIGITOVER 5", "DIGITOVER 6",
                                              "DIGITOVER 7", "DIGITOVER 8"]
    assert results(v) == [("step1", FAIL, [4, 8]), ("step2", FAIL, [8]), ("step3", FAIL, [8]),
                          ("step4", FAIL, [8]), ("step5", FAIL, [8]), ("step6", PASS, [])]
    assert v.status == WAIT and len(v.passing_contracts) == 1


def test_evaluate_contract_matches_single_contract_rule(bots):
    leg = bots["aria"].leg("recovery")
    c = evaluate_contract(leg, [51, 51, 51, 51, 50] + [49] * 5, 500, RULE)
    assert (c.result, c.failing_digits) == (FAIL, [4])               # 50/500 = exactly 10 %: FAIL
    c = evaluate_contract(leg, [51, 51, 51, 51, 51] + [49] * 5, 500, RULE)
    assert c.result == PASS


def test_contract_not_checked_until_window_full(bots):
    window_499 = SymbolQuality("R_10", "CLEAN", "ok", "OK", 499, 1.0, [50] * 9 + [49])
    v = evaluate_combo(bots["aria"], "R_10", "R_10", window_499, compatible=True, compat_text="ok", rule=RULE)
    assert v.status == INSUFFICIENT and [c.result for c in v.contracts] == [NOT_CHECKED, NOT_CHECKED]


def test_unavailable_contract_is_marked_and_bot_stops_critically(bots):
    v = judge(bots, "aria", ARIA_BOTH, leg_compat={"base": "ok", "recovery": "DIGITUNDER 5 1t: barrier 5 not offered"})
    v2 = evaluate_combo(bots["aria"], "1HZ10V", "V10", q(ARIA_BOTH), compatible=False,
                        compat_text="leg recovery: barrier 5 not offered", rule=RULE,
                        leg_compat={"base": "ok", "recovery": "DIGITUNDER 5 1t: barrier 5 not offered"})
    assert [c.result for c in v2.contracts] == [PASS, UNAVAILABLE]
    assert v2.status == AVOID and v2.critical_kind == "contract"
    assert [c.result for c in v.contracts] == [PASS, UNAVAILABLE] and v.status == WAIT


# ------------------------------------------------- bot decision: ALL contracts must pass
@pytest.mark.parametrize("bot,counts,expected", [
    ("aria", ARIA_BOTH, RUN),
    ("aria", ARIA_BASE_ONLY_FAIL, WAIT),
    ("aria", ARIA_REC_FAIL, WAIT),
    ("aria_v2", ARIA_BOTH, RUN),
    ("aria_v2", ARIA_BASE_ONLY_FAIL, WAIT),
    ("zony_ai", ZONY_STEP6_ONLY, WAIT),
    ("zony_ai", [5] + [55] * 9, RUN),
])
def test_bot_passes_only_if_all_contracts_pass(bots, bot, counts, expected):
    v = judge(bots, bot, counts)
    assert v.status == expected
    assert (v.status == RUN) == all(c.result == PASS for c in v.contracts)


def test_bot_verdict_equals_and_of_contract_results_on_random_windows(bots):
    rng = np.random.default_rng(7)
    for _ in range(400):
        counts = list(rng.multinomial(500, rng.dirichlet(np.ones(10) * 20)))
        for key in bots:
            v = judge(bots, key, counts)
            assert (v.status == RUN) == all(c.passed for c in v.contracts), (key, counts)


# ------------------------------------------------- START / STOP through the real engine
@pytest.fixture
def aria(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg, started_at=T0 - 10_000)

    def step(counts, t):
        return eng.process({("aria", "1HZ10V"): judge(bots, "aria", counts)}, t)
    return conn, eng, step


def test_one_failing_contract_prevents_start(aria):
    conn, eng, step = aria
    for i in range(30):                                   # 30 minutes: base fails, recovery passes
        assert step(ARIA_BASE_ONLY_FAIL, T0 + i * 60) == []
    assert notes(conn) == []
    assert not eng.store.get("aria", "1HZ10V").active


def test_all_contracts_pass_starts_after_three_consecutive_checks(aria):
    conn, eng, step = aria
    assert step(ARIA_BOTH, T0) == []
    assert step(ARIA_BOTH, T0 + 60) == []
    tr = step(ARIA_BOTH, T0 + 120)
    assert [t.kind for t in tr] == ["START"] and notes(conn) == ["START"]


def test_a_failing_contract_in_between_resets_the_start_count(aria):
    conn, eng, step = aria
    seq = [ARIA_BOTH, ARIA_BOTH, ARIA_BASE_ONLY_FAIL, ARIA_BOTH, ARIA_BOTH]
    for i, c in enumerate(seq):
        assert step(c, T0 + i * 60) == []
    assert [t.kind for t in step(ARIA_BOTH, T0 + 300)] == ["START"]


def test_stop_after_three_consecutive_failing_checks(aria):
    conn, eng, step = aria
    for i in range(3):
        step(ARIA_BOTH, T0 + i * 60)
    assert step(ARIA_BASE_ONLY_FAIL, T0 + 180) == []
    assert step(ARIA_BASE_ONLY_FAIL, T0 + 240) == []
    tr = step(ARIA_BASE_ONLY_FAIL, T0 + 300)
    assert [t.kind for t in tr] == ["STOP"] and not tr[0].critical
    assert notes(conn) == ["START", "STOP"]
    text = conn.execute("SELECT text FROM notifications WHERE kind='STOP'").fetchone()[0]
    assert "Signal status: STOP — 1 of 2 contracts FAILED" in text
    assert "• DIGITUNDER 9 (base) — digit 5 (9.80%) at or below 10%" in text
    assert "DIGITUNDER 5 (recovery) —" not in text


def test_two_failing_checks_then_pass_does_not_stop(aria):
    conn, eng, step = aria
    for i in range(3):
        step(ARIA_BOTH, T0 + i * 60)
    seq = [ARIA_REC_FAIL, ARIA_BASE_ONLY_FAIL, ARIA_BOTH, ARIA_REC_FAIL, ARIA_REC_FAIL, ARIA_BOTH]
    for i, c in enumerate(seq):
        assert step(c, T0 + 180 + i * 60) == []
    assert notes(conn) == ["START"]


def test_no_duplicate_start_or_stop(aria):
    conn, eng, step = aria
    for i in range(15):
        step(ARIA_BOTH, T0 + i * 60)
    for i in range(15):
        step(ARIA_REC_FAIL, T0 + 900 + i * 60)
    assert notes(conn) == ["START", "STOP"]


# ------------------------------------------------- Telegram shows each contract accurately
def test_start_message_has_one_block_per_contract(bots):
    v = judge(bots, "aria_v2", ARIA_BOTH)
    text = MessageFormatter(bots, "Asia/Colombo").start(v, T0)
    b1 = text.index("Contract 1 of 2 (base): DIGITUNDER, barrier 9, 1 tick")
    b2 = text.index("Contract 2 of 2 (recovery): DIGITUNDER, barrier 5, 1 tick")
    end = text.index("Signal status:")
    base, rec = text[b1:b2], text[b2:end]
    assert "Winning digits: 0, 1, 2, 3, 4, 5, 6, 7, 8" in base
    assert all(f"• Digit {d}: 11.00% ✅" in base for d in range(9)) and "Result: PASS ✅" in base
    assert "Winning digits: 0, 1, 2, 3, 4\n" in rec
    assert all(f"• Digit {d}: 11.00% ✅" in rec for d in range(5)) and "Result: PASS ✅" in rec
    assert not any(f"• Digit {d}:" in rec for d in range(5, 10))       # no merged digit list
    assert "Signal status: START — 2 of 2 contracts PASS (all required)" in text
    assert "Winning digits checked" not in text


def test_stop_message_identifies_the_failed_contract(bots):
    v = judge(bots, "aria_v2", ARIA_BASE_ONLY_FAIL)
    text = MessageFormatter(bots, "Asia/Colombo").stop(v, T0)
    b2 = text.index("Contract 2 of 2 (recovery)")
    base, rec = text[:b2], text[b2:text.index("Signal status:")]
    assert "• Digit 5: 9.80% ❌" in base and "Result: FAIL ❌ — digit 5 (9.80%) at or below 10%" in base
    assert "Result: PASS ✅" in rec and "❌" not in rec
    tail = text[text.index("Signal status:"):]
    assert tail.startswith("Signal status: STOP — 1 of 2 contracts FAILED\nFailed contract:\n"
                           "• DIGITUNDER 9 (base) — digit 5 (9.80%) at or below 10%")


def test_zony_stop_message_lists_six_contracts_and_each_result(bots):
    v = judge(bots, "zony_ai", ZONY_STEP6_ONLY)
    text = MessageFormatter(bots, "Asia/Colombo").stop(v, T0)
    for i, b in enumerate((0, 4, 5, 6, 7, 8), 1):
        assert f"Contract {i} of 6 (step{i}): DIGITOVER, barrier {b}, 1 tick" in text
    assert text.count("Result: FAIL ❌") == 5 and text.count("Result: PASS ✅") == 1
    assert "Signal status: STOP — 5 of 6 contracts FAILED" in text
    assert "• DIGITOVER 0 (step1) — digits 4 (9.80%), 8 (3.60%) at or below 10%" in text
    assert "DIGITOVER 8 (step6) —" not in text
    assert len(text) <= 4096


def test_message_percentages_equal_stored_contract_percentages(bots):
    v = judge(bots, "navix", [62, 62, 62, 62, 62, 62, 62, 62, 2, 2])
    text = MessageFormatter(bots, "Asia/Colombo").stop(v, T0)
    for c in v.contracts:
        block = text[text.index(f"({c.leg_id}): "):]
        block = block[:block.index("Result:")]
        for d in c.winning_digits:
            mark = "❌" if d in c.failing_digits else "✅"
            assert f"• Digit {d}: {c.digit_pct[d]:.2f}% {mark}" in block


def test_critical_stop_names_the_unavailable_contract(bots):
    v = evaluate_combo(bots["aria"], "1HZ10V", "V10", q(ARIA_BOTH), compatible=False,
                       compat_text="leg recovery: barrier 5 not offered", rule=RULE,
                       leg_compat={"base": "ok", "recovery": "DIGITUNDER 5 1t: barrier 5 not offered"})
    text = MessageFormatter(bots, "Asia/Colombo").stop(v, T0, critical=True)
    assert text.startswith("🔴 STOP SIGNAL (critical)")
    assert "Result: NOT AVAILABLE ❌ — DIGITUNDER 5 1t: barrier 5 not offered" in text
    assert "Signal status: STOP — contract no longer offered on this market" in text
    assert "• DIGITUNDER 5 (recovery) — DIGITUNDER 5 1t: barrier 5 not offered" in text


# ------------------------------------------------- SQLite, logs, report
def _run_app(app_factory, rng, monkeypatch, counts):
    client = FakeDeriv(DIGIT_SYMBOLS + NO_DIGITS + FOREX)
    app = app_factory(client)
    asyncio.run(app.discover())
    now = int(time.time())
    for sym in ALLOWED:
        repo.insert_ticks(app.conn, make_rows(sym, window(counts, rng), start_epoch=now - 500))
        app.cache.mark_dirty(sym)
    monkeypatch.setattr(app.health, "status", lambda sym, t: ("OK", "live"))
    return app


def test_evaluation_results_are_stored_in_sqlite(app_factory, monkeypatch, rng):  # noqa: F811
    """Every cycle stores, in one transaction: the decision and the four checks per combination
    (strategy_evaluations), each leg's statistics / payout / EV (leg_evaluations), a history row when a
    verdict changes, and the legacy digit rule per contract (contract_results, informational only)."""
    app = _run_app(app_factory, rng, monkeypatch, ARIA_BASE_ONLY_FAIL)
    res = asyncio.run(app.evaluate_once())
    rows = app.conn.execute("SELECT * FROM contract_results").fetchall()
    assert len(rows) == sum(len(v.legs) for v in res.verdicts.values()) == 18 * 7
    aria = {r["leg_id"]: r for r in repo.load_contract_results(app.conn, "aria", "R_50")}
    assert aria["base"]["result"] == "FAIL" and aria["base"]["failing_digits"] == "5"
    assert aria["base"]["winning_digits"] == "0,1,2,3,4,5,6,7,8" and aria["base"]["barrier"] == 9
    assert aria["recovery"]["result"] == "PASS" and aria["recovery"]["failing_digits"] == ""
    assert json.loads(aria["recovery"]["digit_pct"]) == pytest.approx({"0": 11.2, "1": 11.2, "2": 11.2,
                                                                       "3": 11.2, "4": 11.2})
    assert aria["base"]["bot_status"] == WAIT and "informational" in aria["base"]["detail"]
    zony = repo.load_contract_results(app.conn, "zony_ai", "R_10")
    assert [r["barrier"] for r in zony] == [0, 4, 5, 6, 7, 8]
    sm = json.loads(app.conn.execute("SELECT summary FROM evaluations WHERE bot='aria' AND symbol='R_50'")
                    .fetchone()[0])
    assert sm["model"] == "strategy_v1" and set(sm["gates"]) == {"entry", "statistics", "economics", "risk"}
    assert [(lg["leg_id"], lg["legacy_rule"]["result"]) for lg in sm["legs"]] == [("base", "FAIL"),
                                                                                  ("recovery", "PASS")]
    # no quote and no bankroll yet: payout economics and risk are UNKNOWN, which is never a pass
    assert sm["gates"]["economics"]["status"] == "UNKNOWN" and sm["gates"]["risk"]["status"] == "UNKNOWN"
    st = app.conn.execute("SELECT * FROM strategy_evaluations WHERE bot='aria' AND symbol='R_50'").fetchone()
    assert (st["decision"], st["entry_status"], st["statistics_status"], st["economics_status"],
            st["risk_status"]) == (WAIT, PASS, PASS, "UNKNOWN", "UNKNOWN")
    legs = app.conn.execute("SELECT * FROM leg_evaluations WHERE bot='aria' AND symbol='R_50' ORDER BY rowid").fetchall()
    assert [r["leg_id"] for r in legs] == ["base", "recovery"]
    assert legs[0]["settled_long"] == 499 and legs[0]["payout"] is None and legs[0]["economics_status"] == "UNKNOWN"
    assert app.conn.execute("SELECT COUNT(*) FROM evaluation_history").fetchone()[0] == 70
    # replaced, not appended, on the next cycle; the history grows only when a verdict changes
    asyncio.run(app.evaluate_once())
    for t, n in (("contract_results", 126), ("leg_evaluations", 126), ("strategy_evaluations", 70),
                 ("evaluations", 70), ("evaluation_history", 70)):
        assert app.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n, t


def test_verdict_log_shows_every_check(app_factory, monkeypatch, rng, caplog):  # noqa: F811
    app = _run_app(app_factory, rng, monkeypatch, ARIA_BASE_ONLY_FAIL)
    with caplog.at_level(logging.INFO):
        asyncio.run(app.evaluate_once())
    lines = [r.getMessage() for r in caplog.records if "event=verdict_status" in r.getMessage()
             and "bot=aria " in r.getMessage() and "symbol=R_50" in r.getMessage()]
    assert len(lines) == 1
    # '!' = required for START and not passing
    assert "entry=PASS statistics=PASS economics=UNKNOWN! risk=UNKNOWN!" in lines[0]
    cycle = [r.getMessage() for r in caplog.records if "event=evaluation_cycle" in r.getMessage()]
    assert cycle and "legs=126" in cycle[-1] and "WAIT:70" in cycle[-1]
    caplog.clear()
    with caplog.at_level(logging.INFO):                      # nothing changed: no repeat log line
        asyncio.run(app.evaluate_once())
    assert not [r for r in caplog.records if "event=verdict_" in r.getMessage()]


def test_report_shows_every_check_separately(app_factory, monkeypatch, rng, tmp_path):  # noqa: F811
    app = _run_app(app_factory, rng, monkeypatch, ARIA_BASE_ONLY_FAIL)
    make_runnable(app, time.time())                 # fake quotes + bankroll: payout and risk get numbers
    asyncio.run(app.evaluate_once())
    cfg = tmp_path / "config.yaml"
    env = dict(os.environ, ANALYZER_CONFIG=str(cfg), PYTHONUTF8="1")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "report.py"), "--bot", "aria", "--symbol", "R_50",
                          "--contracts", "--quotes", "--history"], capture_output=True, text=True, encoding="utf-8",
                         env=env, cwd=ROOT, timeout=60)
    assert out.returncode == 0, out.stderr
    o = out.stdout
    assert "aria         R_50     feed=CLEAN entry=PASS statistics=PASS economics=POSITIVE risk=SAFE" in o
    assert "leg base      DIGITUNDER barrier 9  1t  stake 1.00" in o
    assert "payout 1.17 for 1.00: net win +0.17, net loss -1.00, break-even 85.47%" in o
    assert "legacy digit rule (informational, not a gate): FAIL [failing 5]" in o
    assert "current recovery level: not observable" in o
    assert "next trade: DIGITUNDER 9 1t at 1.00" in o
    assert "Legacy digit rule per contract — informational only, gates nothing (2 contracts): {'FAIL': 1, 'PASS': 1}" in o
    assert "Stored Deriv quotes:" in o and "Payout formula cross-check" in o
    assert "Recent verdict changes" in o

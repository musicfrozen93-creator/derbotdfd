"""Regression: a pass streak must never survive a cycle in which the combination was not evaluated.

Audit 2026-11-18 (commit 9c43f3a): an INACTIVE combination with 2 of 3 confirmations that dropped
out of the verdicts (compatibility lost, market removed and re-added, evaluation exception) kept
``pass_streak = 2``, so ONE fresh RUN_CANDIDATE after it came back sent a START. START must rest on
``start_confirm_cycles`` (3) fresh consecutive evaluation cycles.
"""
import asyncio
import logging
import sqlite3

import pytest

from app.analytics.validation import AVOID, RUN, WAIT

from tests.test_discovery import DIGIT_SYMBOLS, FakeDeriv, app_factory  # noqa: F401
from tests.test_signals import T0, make_engine, notes, verdict

KEY = ("aria", "1HZ10V")
OTHER = ("delta", "R_100")          # another combination that keeps being evaluated
STEP = 60


def kinds(tr):
    return [t.kind for t in tr]


def run(eng, statuses, t, key=KEY, others=(), others_status=WAIT, **kw):
    """One cycle per status for ``key``; ``others`` are evaluated (``others_status``) in the same cycles.
    A status of None means ``key`` is missing from that cycle's verdicts."""
    out = []
    for st in statuses:
        verdicts = {k: verdict(k[0], k[1], others_status) for k in others}
        if st is not None:
            verdicts[key] = verdict(key[0], key[1], st, **kw)
        out.append(kinds(eng.process(verdicts, t)))
        t += STEP
    return out, t


def stored(conn, key=KEY):
    r = conn.execute("SELECT active, pass_streak, fail_streak FROM signal_state WHERE bot=? AND symbol=?",
                     key).fetchone()
    return tuple(r) if r else None


# --------------------------------------------------------------- the audit scenario
@pytest.mark.parametrize("gap", [1, 3, 360])           # one cycle, a few, six hours
def test_pass_pass_missing_cycles_pass_does_not_start(db, bots, cfg, gap):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    tr, t = run(eng, [RUN, RUN] + [None] * gap + [RUN], T0, others=[OTHER])
    assert tr == [[]] * (gap + 3) and notes(conn) == []
    assert eng.store.get(*KEY).pass_streak == 1          # only the fresh PASS counts
    tr, _ = run(eng, [RUN, RUN], t, others=[OTHER])
    assert tr == [[], ["START"]]                         # 3rd fresh consecutive PASS
    assert notes(conn) == ["START"]


def test_pass_pass_missing_with_empty_verdicts_does_not_start(db, bots, cfg):
    """Nothing at all evaluated in the gap (every combination missing)."""
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    tr, _ = run(eng, [RUN, RUN, None, None, RUN], T0)
    assert tr == [[]] * 5 and notes(conn) == []


def test_removed_and_readded_needs_three_fresh_passes(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    tr, t = run(eng, [RUN, RUN], T0, others=[OTHER])
    assert eng.store.get(*KEY).pass_streak == 2
    tr, t = run(eng, [None] * 5, t, others=[OTHER])      # combination removed from the list
    assert eng.store.get(*KEY).pass_streak == 0
    assert stored(conn) == (0, 0, 0)                     # the reset is persisted too
    tr, t = run(eng, [RUN, RUN, RUN], t, others=[OTHER])  # re-added: three fresh checks
    assert tr == [[], [], ["START"]]                     # START only on the third fresh check
    assert notes(conn) == ["START"]


def test_streak_of_a_missing_combination_resets_independently(db, bots, cfg):
    """Only the missing combination loses its streak; the evaluated one keeps counting."""
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    both = {KEY: None, OTHER: None}
    for i in range(2):
        eng.process({k: verdict(k[0], k[1], RUN) for k in both}, T0 + i * STEP)
    tr = eng.process({OTHER: verdict(*OTHER, RUN)}, T0 + 2 * STEP)         # KEY missing
    assert [(x.kind, x.bot, x.symbol) for x in tr] == [("START", *OTHER)]
    assert eng.store.get(*KEY).pass_streak == 0 and not eng.store.get(*KEY).active


# --------------------------------------------------------------- evaluation exceptions
def test_evaluation_exception_discards_start_credit(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run(eng, [RUN, RUN], T0)
    assert eng.discard_start_credit(t, "evaluation failed: RuntimeError") == 1
    assert eng.store.get(*KEY).pass_streak == 0 and stored(conn) == (0, 0, 0)
    tr, t = run(eng, [RUN, RUN], t + STEP)
    assert tr == [[], []] and notes(conn) == []
    tr, _ = run(eng, [RUN], t)
    assert tr == [["START"]]


def test_discard_start_credit_keeps_memory_reset_when_db_write_fails(db, bots, cfg, caplog):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run(eng, [RUN, RUN], T0)
    conn.execute("CREATE TEMP TRIGGER fail_state BEFORE UPDATE ON signal_state "
                 "BEGIN SELECT RAISE(ABORT, 'forced state failure'); END")
    with caplog.at_level(logging.INFO):
        assert eng.discard_start_credit(t) == 1          # does not raise
    assert conn.in_transaction is False
    assert eng.store.get(*KEY).pass_streak == 0          # memory: no credit left
    assert stored(conn) == (0, 2, 0)                     # write rolled back (cleared on restart)
    assert any("event=signal_streak_reset_unsaved" in r.getMessage() for r in caplog.records)
    conn.execute("DROP TRIGGER fail_state")
    tr, _ = run(eng, [RUN], t + STEP)
    assert tr == [[]] and notes(conn) == []              # one PASS is not enough


def test_engine_failure_then_discard_cannot_start_on_old_credit(db, bots, cfg):
    """process() raising restores the last committed state (pass 2); the loop then discards it."""
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run(eng, [RUN, RUN], T0)
    conn.execute("CREATE TEMP TRIGGER fail_n BEFORE INSERT ON notifications "
                 "BEGIN SELECT RAISE(ABORT, 'forced outbox failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        eng.process({KEY: verdict(*KEY, RUN)}, t)        # would START
    assert eng.store.get(*KEY).pass_streak == 2
    conn.execute("DROP TRIGGER fail_n")
    eng.discard_start_credit(t)
    tr, _ = run(eng, [RUN, RUN, RUN], t + STEP)
    assert tr == [[], [], ["START"]]


def test_evaluation_loop_exception_discards_start_credit(app_factory, monkeypatch):  # noqa: F811
    import app.main as main_mod

    app = app_factory(FakeDeriv(DIGIT_SYMBOLS))
    asyncio.run(app.discover())
    eng = app.engine
    for i in range(2):
        eng.process({KEY: verdict(*KEY, RUN)}, T0 + i * STEP)
    assert eng.store.get(*KEY).pass_streak == 2

    def boom(*a, **k):
        raise RuntimeError("analyzer crashed")

    async def stop_after_one(_s):
        raise asyncio.CancelledError

    monkeypatch.setattr(app.analyzer, "evaluate", boom)
    monkeypatch.setattr(main_mod.asyncio, "sleep", stop_after_one)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app.evaluation_loop())
    assert app.eval_failures_consecutive == 1
    assert eng.store.get(*KEY).pass_streak == 0
    assert stored(app.conn) == (0, 0, 0)
    tr = eng.process({KEY: verdict(*KEY, RUN)}, T0 + 10 * STEP)
    assert tr == [] and notes(app.conn, "START") == []


# --------------------------------------------------------------- active signals are untouched
def activate(eng, t=T0, others=()):
    tr, t = run(eng, [RUN] * 3, t, others=others)
    assert tr[-1] == ["START"]
    return t


def test_active_combination_that_disappears_still_stops_critically_once(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng, others=[OTHER])
    tr = eng.process({OTHER: verdict(*OTHER, WAIT)}, t, {KEY: "market no longer listed by Deriv active_symbols"})
    assert [(x.kind, x.critical, x.reason) for x in tr] == \
        [("STOP", True, "market no longer listed by Deriv active_symbols")]
    s = eng.store.get(*KEY)
    assert (s.active, s.pass_streak, s.fail_streak, s.last_status) == (False, 0, 0, "UNMONITORED")
    assert s.last_stop_at == int(t)
    tr, _ = run(eng, [None] * 5, t + STEP, others=[OTHER])
    assert tr == [[]] * 5 and notes(conn) == ["START", "STOP"]


def test_discard_start_credit_does_not_touch_active_signals(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    _, t = run(eng, [WAIT, WAIT], t)                     # 2 of 3 failures
    assert eng.discard_start_credit(t) == 0
    s = eng.store.get(*KEY)
    assert (s.active, s.fail_streak) == (True, 2)        # active flag and fail streak kept
    tr, _ = run(eng, [WAIT], t + STEP)
    assert tr == [["STOP"]]                              # STOP behaviour unchanged


def test_active_signal_stays_active_while_another_combination_is_missing(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    _, t = run(eng, [RUN, RUN], t, key=OTHER, others=[KEY], others_status=RUN)
    tr, t = run(eng, [RUN] * 10, t)                       # OTHER missing, KEY keeps passing
    assert tr == [[]] * 10 and eng.store.get(*KEY).active
    assert eng.store.get(*OTHER).pass_streak == 0
    assert notes(conn) == ["START"]


# --------------------------------------------------------------- STOP / critical STOP unchanged
def test_three_failures_stop_and_brief_dip_does_not(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr, t = run(eng, [WAIT, WAIT, RUN, WAIT, WAIT], t)
    assert tr == [[]] * 5
    tr, _ = run(eng, [WAIT], t)
    assert tr == [["STOP"]] and notes(conn) == ["START", "STOP"]


@pytest.mark.parametrize("ck", ["contract", "data"])
def test_critical_stop_is_immediate(db, bots, cfg, ck):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)                   # started long ago: no start-up grace
    t = activate(eng)
    tr = eng.process({KEY: verdict(*KEY, AVOID, critical_kind=ck)}, t)
    assert kinds(tr) == ["STOP"] and tr[0].critical


# --------------------------------------------------------------- cooldown unchanged
def test_cooldown_still_blocks_start_after_reappearing(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr, t = run(eng, [WAIT] * 3, t)
    stop_at = t - STEP
    assert tr[-1] == ["STOP"]
    tr, t = run(eng, [RUN, RUN], t)                      # credit built inside the cooldown ...
    tr, t = run(eng, [None] * 3, t)                      # ... is lost when the combination drops out
    tr, t = run(eng, [RUN] * 3, t)                       # 3 fresh passes, still inside the cooldown
    assert tr == [[]] * 3 and t - STEP - stop_at < 1800
    while t - stop_at < 1800:
        tr, t = run(eng, [RUN], t)
        assert tr == [[]]
    tr, _ = run(eng, [RUN], t)                           # first check after 30 min
    assert tr == [["START"]] and t - stop_at >= 1800


def test_cooldown_expired_while_missing_still_needs_three_fresh_passes(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr, t = run(eng, [WAIT] * 3, t)
    assert tr[-1] == ["STOP"]
    tr, t = run(eng, [RUN] * 25, t)                      # pass streak 25 during the cooldown
    tr, t = run(eng, [None] * 10, t)                     # missing until the cooldown is over
    tr, _ = run(eng, [RUN] * 3, t)
    assert tr == [[], [], ["START"]]


# --------------------------------------------------------------- restart recovery unchanged
def test_reset_survives_restart_and_restart_keeps_active_flag(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng, others=[])                         # KEY active
    _, t = run(eng, [RUN, RUN], t, key=OTHER, others=[KEY], others_status=RUN)
    _, t = run(eng, [RUN], t)                            # OTHER missing -> reset and stored
    assert stored(conn, OTHER) == (0, 0, 0)
    eng2 = make_engine(conn, bots, cfg, started_at=t + 30)
    assert eng2.on_startup(last_alive=t, now=t + 30, max_offline_s=120) == []
    assert eng2.store.get(*KEY).active and eng2.store.get(*OTHER).pass_streak == 0
    tr, t2 = run(eng2, [RUN], t + 30, key=OTHER, others=[KEY], others_status=RUN)
    assert tr == [[]]
    tr, _ = run(eng2, [RUN, RUN], t2, key=OTHER, others=[KEY], others_status=RUN)
    assert tr == [[], ["START"]] and notes(conn) == ["START", "START"]


def test_long_downtime_still_stops_active_and_starts_cooldown(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    eng2 = make_engine(conn, bots, cfg, started_at=t + 900)
    tr = eng2.on_startup(last_alive=t, now=t + 900, max_offline_s=120)
    assert kinds(tr) == ["STOP"] and tr[0].critical
    tr, _ = run(eng2, [RUN] * 3, t + 900)                # inside the 30-min cooldown
    assert tr == [[]] * 3 and notes(conn) == ["START", "STOP"]

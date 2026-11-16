"""Every START / STOP state transition, with mocked verdicts, a mocked Telegram transport and
the real analyzer for the end-to-end feed-failure case.

State per (bot, market): INACTIVE or ACTIVE, plus pass / fail confirmation counters.
Only two messages exist for a combination: START (INACTIVE -> ACTIVE) and STOP (ACTIVE -> INACTIVE).
"""
import asyncio
import time

import numpy as np
import pytest

from app.analytics.payout import payout_key
from app.analytics.validation import AVOID, INSUFFICIENT, RUN, WAIT
from app.signals.engine import SignalEngine
from app.signals.state import SignalStateStore
from app.signals.telegram import MessageFormatter, TelegramSender

from tests.test_signals import T0, make_engine, notes, run_cycles, verdict
from tests.test_validation import _analyzer_setup, q

KEY = ("aria", "1HZ10V")


def kinds(tr):
    return [t.kind for t in tr]


def activate(eng, t=T0):
    tr, t = run_cycles(eng, [RUN] * 3, t=t)
    assert kinds(tr) == ["START"]
    return t


# --------------------------------------------------------------------- INACTIVE
@pytest.mark.parametrize("seq", [[RUN], [RUN, RUN], [RUN, RUN, WAIT, RUN, RUN], [RUN, AVOID, RUN, RUN],
                                 [RUN, RUN, INSUFFICIENT, RUN]])
def test_inactive_no_start_without_three_consecutive_run(db, bots, cfg, seq):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    tr, _ = run_cycles(eng, seq)
    assert tr == [] and notes(conn) == []


def test_inactive_broken_streak_restarts_count(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    tr, t = run_cycles(eng, [RUN, RUN, WAIT, RUN, RUN])
    assert tr == []
    tr, _ = run_cycles(eng, [RUN], t=t)
    assert kinds(tr) == ["START"]


@pytest.mark.parametrize("status", [WAIT, AVOID, INSUFFICIENT])
def test_inactive_non_run_statuses_send_nothing(db, bots, cfg, status):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    tr, _ = run_cycles(eng, [status] * 50)
    assert tr == [] and notes(conn) == []          # no WAIT / AVOID / INSUFFICIENT messages, ever


@pytest.mark.parametrize("ck", ["contract", "data"])
def test_inactive_critical_fault_sends_nothing(db, bots, cfg, ck):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    tr, _ = run_cycles(eng, [AVOID] * 5, critical_kind=ck)
    assert tr == [] and notes(conn) == []


def test_inactive_data_dip_hold_blocks_start(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN, RUN])
    tr, t = run_cycles(eng, [WAIT], t=t, hold=True)      # unclean data resets the pass streak
    tr2, t = run_cycles(eng, [RUN, RUN], t=t)
    assert tr == [] and tr2 == []
    tr, _ = run_cycles(eng, [RUN], t=t)
    assert kinds(tr) == ["START"]


def test_inactive_cooldown_after_stop(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr, t = run_cycles(eng, [WAIT] * 3, t=t)
    assert kinds(tr) == ["STOP"]
    stop_t = t - 60
    tr, t = run_cycles(eng, [RUN] * 25, t=t)                 # 25 min of RUN: still inside 30-min cooldown
    assert tr == []
    tr, _ = run_cycles(eng, [RUN], t=stop_t + 1800)         # cooldown over, streak already >= 3
    assert kinds(tr) == ["START"]


# ----------------------------------------------------------------------- ACTIVE
def test_active_run_never_repeats_start(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr, _ = run_cycles(eng, [RUN] * 100, t=t)
    assert tr == [] and notes(conn) == ["START"]


def test_active_failures_must_be_consecutive(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr, _ = run_cycles(eng, [WAIT, WAIT, RUN, AVOID, INSUFFICIENT, RUN, WAIT, WAIT, RUN], t=t)
    assert tr == [] and notes(conn) == ["START"]


@pytest.mark.parametrize("seq", [[WAIT] * 3, [AVOID] * 3, [INSUFFICIENT] * 3, [WAIT, AVOID, INSUFFICIENT]])
def test_active_three_consecutive_failures_stop_once(db, bots, cfg, seq):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr, t = run_cycles(eng, seq, t=t)
    assert kinds(tr) == ["STOP"] and not tr[0].critical
    tr, _ = run_cycles(eng, [WAIT, AVOID, INSUFFICIENT] * 20, t=t)    # keeps failing: no second STOP
    assert tr == [] and notes(conn) == ["START", "STOP"]


def test_active_contract_withdrawn_stops_immediately(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr, t = run_cycles(eng, [AVOID], t=t, critical_kind="contract")
    assert kinds(tr) == ["STOP"] and tr[0].critical
    tr, _ = run_cycles(eng, [AVOID] * 5, t=t, critical_kind="contract")
    assert tr == [] and notes(conn) == ["START", "STOP"]


def test_active_feed_fault_after_grace_stops_immediately(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)                        # started 10,000 s ago: no grace
    t = activate(eng)
    tr, _ = run_cycles(eng, [AVOID], t=t, critical_kind="data")
    assert kinds(tr) == ["STOP"] and tr[0].critical
    row = conn.execute("SELECT text FROM notifications WHERE kind='STOP'").fetchone()
    assert "critical" in row[0]


def test_active_feed_fault_during_grace_is_held_then_stops(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    eng2 = make_engine(conn, bots, cfg, started_at=t)          # restarted: 300 s grace
    tr, _ = run_cycles(eng2, [AVOID] * 4, t=t + 1, critical_kind="data")
    assert tr == []
    tr, _ = run_cycles(eng2, [AVOID], t=t + 301, critical_kind="data")   # still faulted after grace
    assert kinds(tr) == ["STOP"] and tr[0].critical


def test_active_data_quality_dip_holds_indefinitely_then_real_failure_stops(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr, t = run_cycles(eng, [WAIT] * 100, t=t, hold=True)
    assert tr == []
    tr, _ = run_cycles(eng, [WAIT] * 3, t=t)                   # a real (statistical) failure
    assert kinds(tr) == ["STOP"]


def test_active_combination_no_longer_monitored_stops_once(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    tr = eng.process({}, t, {KEY: "market no longer listed by Deriv active_symbols"})
    assert kinds(tr) == ["STOP"] and tr[0].critical
    for i in range(5):
        assert eng.process({}, t + 60 * (i + 1)) == []
    assert notes(conn) == ["START", "STOP"]


# -------------------------------------------------------------- independence
def test_one_markets_stop_does_not_touch_other_markets_or_bots(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    keys = [("aria", "1HZ10V"), ("aria", "R_100"), ("delta", "1HZ10V"), ("delta", "R_10")]
    for i in range(3):
        eng.process({k: verdict(k[0], k[1], RUN) for k in keys}, T0 + i * 60)
    assert notes(conn) == ["START"] * 4
    # feed fault on 1HZ10V only: both bots on 1HZ10V stop, R_100 / R_10 stay active
    tr = eng.process({k: verdict(k[0], k[1], AVOID, critical_kind="data") if k[1] == "1HZ10V"
                      else verdict(k[0], k[1], RUN) for k in keys}, T0 + 600)
    assert sorted((t.bot, t.symbol) for t in tr) == [("aria", "1HZ10V"), ("delta", "1HZ10V")]
    assert eng.store.get("aria", "R_100").active and eng.store.get("delta", "R_10").active
    # a statistical failure of one bot on one market stops only that combination
    for i in range(3):
        tr = eng.process({("aria", "R_100"): verdict("aria", "R_100", WAIT),
                          ("delta", "R_10"): verdict("delta", "R_10", RUN)}, T0 + 700 + i * 60)
    assert eng.store.get("delta", "R_10").active and not eng.store.get("aria", "R_100").active
    assert notes(conn) == ["START"] * 4 + ["STOP"] * 3


# ------------------------------------------------------------------- restarts
def test_restart_keeps_active_and_sends_no_duplicate_start(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    eng2 = make_engine(conn, bots, cfg, started_at=t + 30)
    assert eng2.on_startup(last_alive=t, now=t + 30, max_offline_s=120) == []    # 30 s restart
    assert eng2.store.get(*KEY).active
    tr, _ = run_cycles(eng2, [RUN] * 20, t=t + 30)
    assert tr == [] and notes(conn) == ["START"]


def test_restart_clears_stale_confirmation_counters(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN, RUN])                          # 2 of 3 confirmations, then restart
    eng2 = make_engine(conn, bots, cfg, started_at=t + 3600)
    eng2.on_startup(last_alive=t, now=t + 3600, max_offline_s=120)
    tr, t2 = run_cycles(eng2, [RUN], t=t + 3600)
    assert tr == []                                             # the old streak does not count
    tr, _ = run_cycles(eng2, [RUN, RUN], t=t2)
    assert kinds(tr) == ["START"]


def test_restart_after_long_downtime_stops_active_signals_once(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    eng2 = make_engine(conn, bots, cfg, started_at=t + 900)
    tr = eng2.on_startup(last_alive=t, now=t + 900, max_offline_s=120)        # 15 min offline
    assert kinds(tr) == ["STOP"] and tr[0].critical and "offline for 15.0 min" in tr[0].reason
    assert not eng2.store.get(*KEY).active
    eng3 = make_engine(conn, bots, cfg, started_at=t + 960)                    # restarts again
    assert eng3.on_startup(last_alive=t + 900, now=t + 960, max_offline_s=120) == []
    assert notes(conn) == ["START", "STOP"]


def test_first_ever_start_sends_nothing(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    assert eng.on_startup(last_alive=None, now=T0, max_offline_s=120) == []
    assert notes(conn) == []


def test_transition_and_notification_are_atomic(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN, RUN])

    def boom(*a, **k):
        raise RuntimeError("formatter failed")
    eng.fmt.start = boom
    with pytest.raises(RuntimeError):
        eng.process({KEY: verdict(*KEY, RUN)}, t)
    assert not eng.store.get(*KEY).active and notes(conn) == []                  # rolled back together
    assert not conn.execute("SELECT active FROM signal_state WHERE bot='aria'").fetchone()[0]


def test_outbox_delivers_each_message_once_across_restart_and_failures(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    t = activate(eng)
    sent = []
    calls = {"n": 0}

    def flaky(text):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("Telegram network error: timed out")
        sent.append(text)

    s1 = TelegramSender(conn, "x", "y", transport=flaky)
    assert asyncio.run(s1.deliver_pending(now=t)) == 0          # first attempt fails, kept pending
    s2 = TelegramSender(conn, "x", "y", transport=flaky)          # "restarted" process
    asyncio.run(s2.deliver_pending(now=t + 3600))                 # second failure (backoff)
    asyncio.run(s2.deliver_pending(now=t + 7200))                 # delivered
    asyncio.run(s2.deliver_pending(now=t + 9000))                 # nothing left
    assert len(sent) == 1 and sent[0].startswith("🟢 START SIGNAL")
    row = conn.execute("SELECT status, attempts FROM notifications").fetchone()
    assert row[0] == "sent" and row[1] == 3


# --------------------------------------------- end to end: real analyzer + engine
def test_genuine_feed_failure_invalidates_active_signal_end_to_end(db, bots, cfg, rng):
    """Synthetic data with a real edge reaches RUN_CANDIDATE through the real analyzer, the
    engine STARTs; then the live feed goes FAULT and the same pipeline STOPs immediately."""
    digits = rng.integers(0, 5, 30000)          # only 0-4: Aria's UNDER 9 and UNDER 5 always win
    an, market, compat, now = _analyzer_setup(db, bots, cfg, digits)
    dq = dict(cfg.data_quality)
    dq["chi2_fault_p"] = 0.0                     # the uniformity alarm would (correctly) block this
    an.dq = type(cfg.data_quality)(dq)
    payouts = {payout_key("SYN", "DIGITUNDER", 9, 1): q(0.917, "SYN", "DIGITUNDER", 9, 1),
               payout_key("SYN", "DIGITUNDER", 5, 1): q(0.512, "SYN", "DIGITUNDER", 5, 1)}
    conn, _ = db
    eng = make_engine(conn, bots, cfg, started_at=now - 10_000)
    combos = [("aria", "SYN")]
    out = []
    for i in range(3):
        res = an.evaluate(combos, compat, {"SYN": market}, payouts, {"SYN": ("OK", "live")}, now)
        assert res.verdicts[("aria", "SYN")].status == RUN
        out += eng.process(res.verdicts, now + i)
    assert kinds(out) == ["START"]
    res = an.evaluate(combos, compat, {"SYN": market}, payouts,
                      {"SYN": ("FAULT", "no ticks for 180 s")}, now + 180)
    v = res.verdicts[("aria", "SYN")]
    assert v.status == AVOID and v.critical and v.critical_kind == "data"
    tr = eng.process(res.verdicts, now + 180)
    assert kinds(tr) == ["STOP"] and tr[0].critical
    text = conn.execute("SELECT text FROM notifications WHERE kind='STOP'").fetchone()[0]
    assert "SYN" in text and "no ticks for 180 s" in text

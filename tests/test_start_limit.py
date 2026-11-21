"""Per-bot START limit: at most ``signals.max_starts_per_bot`` (5) START signals per bot, all markets
together, in any rolling ``signals.start_limit_window_hours`` (12 h). STOP is never limited.
Persisted in ``signal_start_ledger`` (written in the same transaction as the START)."""
import asyncio
import logging
import sqlite3

import pytest

from app.analytics.validation import AVOID, RUN, WAIT
from app.config import DEFAULTS, Section
from app.signals.engine import LIMIT_REACHED, SignalEngine, StartLimit
from app.signals.state import SignalStateStore
from app.signals.telegram import MessageFormatter, TelegramSender
from app.storage.database import SCHEMA_VERSION, init_db, stored_schema_version

from tests.test_signals import T0, notes, verdict

MARKETS = ["1HZ10V", "1HZ50V", "R_10", "R_25", "R_50", "R_75", "R_100"]
H = 3600
STEP = 60


def signals_cfg(**over):
    d = dict(DEFAULTS["signals"])
    d.update(over)
    return Section(d)


def engine(conn, bots, started_at=T0 - 10_000, **over):
    store = SignalStateStore(conn)
    return SignalEngine(conn, store, MessageFormatter(bots, "Asia/Colombo"), signals_cfg(**over),
                        started_at=started_at)


def cycle(eng, t, statuses: dict, **kw):
    """One evaluation cycle; ``statuses`` = {(bot, symbol): status}. Combinations that are already
    active and not named keep being evaluated as RUN (a combination missing from a cycle is STOPped)."""
    statuses = {**{(s.bot, s.symbol): RUN for s in eng.store.active()}, **statuses}
    return [(x.kind, x.bot, x.symbol) for x in
            eng.process({k: verdict(k[0], k[1], st, **kw) for k, st in statuses.items()}, t)]


def cycles(eng, t, statuses: dict, n=3):
    out = []
    for _ in range(n):
        out += cycle(eng, t, statuses)
        t += STEP
    return out, t


def ledger(conn, bot=None):
    q = "SELECT bot, symbol, started_at FROM signal_start_ledger"
    rows = [tuple(r) for r in conn.execute(q + " ORDER BY started_at, symbol")]
    return [r for r in rows if bot is None or r[0] == bot]


def start_five(eng, t=T0, bot="aria"):
    """START ``bot`` on the first five markets (one after the other). Returns (start times, t)."""
    times = []
    for m in MARKETS[:5]:
        tr, t = cycles(eng, t, {(bot, m): RUN})
        assert tr == [("START", bot, m)]
        times.append(t - STEP)
    return times, t


# ------------------------------------------------------------------ config
def test_defaults_are_five_per_twelve_hours():
    lim = StartLimit.from_settings(Section(DEFAULTS["signals"]))
    assert (lim.max_starts, lim.window_s, lim.enabled) == (5, 12 * H, True)
    assert lim.text() == "max 5 START per bot in any rolling 12 h"


@pytest.mark.parametrize("over,err", [({"max_starts_per_bot": -1}, "max_starts_per_bot"),
                                      ({"start_limit_window_hours": 0}, "start_limit_window_hours"),
                                      ({"start_limit_window_hours": 1000}, "start_limit_window_hours")])
def test_invalid_limit_config_is_rejected(over, err):
    with pytest.raises(ValueError, match=err):
        StartLimit.from_settings(signals_cfg(**over))


def test_limit_is_configurable_and_zero_disables_it(db, bots):
    conn, _ = db
    eng = engine(conn, bots, max_starts_per_bot=2, start_limit_window_hours=1)
    t = T0
    got = []
    for m in MARKETS[:3]:
        tr, t = cycles(eng, t, {("aria", m): RUN})
        got += tr
    assert [x[2] for x in got] == MARKETS[:2]                 # 3rd blocked by max 2 / 1 h
    conn2 = init_db(":memory:")
    eng0 = engine(conn2, bots, max_starts_per_bot=0)
    t = T0
    for m in MARKETS:
        tr, t = cycles(eng0, t, {("aria", m): RUN})
        assert tr == [("START", "aria", m)]                   # no limit
    conn2.close()


# ------------------------------------------------------------------ the limit
def test_exactly_five_starts_in_twelve_hours_are_allowed(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    times, _ = start_five(eng)
    assert notes(conn) == ["START"] * 5
    assert [r[1] for r in ledger(conn)] == MARKETS[:5] and [r[2] for r in ledger(conn)] == times


def test_sixth_start_within_twelve_hours_is_blocked(db, bots, caplog):
    conn, _ = db
    eng = engine(conn, bots)
    _, t = start_five(eng)
    key = ("aria", MARKETS[5])
    with caplog.at_level(logging.INFO):
        tr, t = cycles(eng, t, {key: RUN}, n=10)
    assert tr == [] and notes(conn) == ["START"] * 5
    s = eng.store.get(*key)
    assert not s.active and s.last_status == LIMIT_REACHED and s.pass_streak == 10
    assert len(ledger(conn)) == 5
    ev = conn.execute("SELECT bot, symbol, reason FROM signal_events WHERE kind=?", (LIMIT_REACHED,)).fetchall()
    assert len(ev) == 1 and tuple(ev[0])[:2] == key and "limit 5" in ev[0][2]        # once per episode
    logged = [r.getMessage() for r in caplog.records if "event=signal_limit_reached" in r.getMessage()]
    assert len(logged) == 1 and "used=5" in logged[0]
    row = conn.execute("SELECT last_status FROM signal_state WHERE bot=? AND symbol=?", key).fetchone()
    assert row[0] == LIMIT_REACHED                                                     # persisted


def test_start_allowed_once_the_oldest_start_leaves_the_rolling_window(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    times, t = start_five(eng)
    key = ("aria", MARKETS[5])
    free_at = times[0] + 12 * H                     # the first START stops counting exactly 12 h later
    while t < free_at:
        assert cycle(eng, t, {key: RUN}) == []
        t += STEP
    assert t == free_at or t - free_at < STEP
    assert cycle(eng, t, {key: RUN}) == [("START", "aria", MARKETS[5])]
    assert len(ledger(conn, "aria")) == 6 and eng.starts_in_window("aria", t) == 5
    # still 5 in the window -> the 7th market is blocked until the 2nd START leaves it
    k7 = ("aria", MARKETS[6])
    t += STEP
    while t < times[1] + 12 * H:
        assert cycle(eng, t, {k7: RUN}) == []
        t += STEP
    assert t == times[1] + 12 * H
    assert cycle(eng, t, {k7: RUN}) == [("START", "aria", MARKETS[6])]


def test_rolling_window_boundary_is_exact(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    times, _ = start_five(eng)
    assert eng.starts_in_window("aria", times[0] + 12 * H - 1) == 5
    assert eng.starts_in_window("aria", times[0] + 12 * H) == 4


def test_stop_is_never_blocked_by_the_limit(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    _, t = start_five(eng)
    blocked = ("aria", MARKETS[5])
    tr, t = cycles(eng, t, {blocked: RUN})
    assert tr == []                                                    # limit reached
    tr, t = cycles(eng, t, {("aria", MARKETS[0]): WAIT})              # 3 failing checks
    assert tr == [("STOP", "aria", MARKETS[0])]
    tr = eng.process({("aria", MARKETS[1]): verdict("aria", MARKETS[1], AVOID, critical_kind="contract")}
                     | {("aria", m): verdict("aria", m, RUN) for m in MARKETS[2:5]}, t)
    assert [(x.kind, x.symbol, x.critical) for x in tr] == [("STOP", MARKETS[1], True)]   # immediate
    tr = eng.process({("aria", m): verdict("aria", m, RUN) for m in MARKETS[3:5]}, t + STEP,
                     {("aria", MARKETS[2]): "market no longer listed"})
    assert [(x.kind, x.symbol, x.critical) for x in tr] == [("STOP", MARKETS[2], True)]   # unmonitored
    assert notes(conn).count("STOP") == 3
    assert len(ledger(conn)) == 5                                      # a STOP never frees a slot


def test_one_bot_at_its_limit_does_not_affect_another_bot(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    _, t = start_five(eng, bot="aria")
    assert cycles(eng, t, {("aria", MARKETS[5]): RUN})[0] == []
    tr, t = cycles(eng, t, {("delta", m): RUN for m in MARKETS[:5]})
    assert sorted(tr) == sorted(("START", "delta", m) for m in MARKETS[:5])
    assert cycles(eng, t, {("delta", MARKETS[5]): RUN})[0] == []        # delta has its own limit
    assert len(ledger(conn, "aria")) == len(ledger(conn, "delta")) == 5


def test_starts_on_different_markets_in_the_same_cycle_share_the_bot_limit(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    tr, _ = cycles(eng, T0, {("aria", m): RUN for m in MARKETS})        # all 7 qualify at once
    assert len(tr) == 5 and {x[1] for x in tr} == {"aria"}
    assert len(ledger(conn, "aria")) == 5
    blocked = [m for m in MARKETS if not eng.store.get("aria", m).active]
    assert len(blocked) == 2 and all(eng.store.get("aria", m).last_status == LIMIT_REACHED for m in blocked)


# ------------------------------------------------------------------ persistence
def test_restart_does_not_reset_the_limit(db, bots):
    conn, path = db
    eng = engine(conn, bots)
    _, t = start_five(eng)
    conn.close()
    conn2 = init_db(path)                                                  # process restart
    try:
        eng2 = engine(conn2, bots, started_at=t + 30)
        assert eng2.on_startup(last_alive=t, now=t + 30, max_offline_s=120) == []
        assert eng2.starts_in_window("aria", int(t + 30)) == 5
        tr, _ = cycles(eng2, t + 30, {("aria", MARKETS[5]): RUN}, n=5)
        assert tr == [] and notes(conn2) == ["START"] * 5
    finally:
        conn2.close()


def test_upgrade_from_schema_2_counts_starts_sent_before_the_upgrade(tmp_path, bots):
    path = tmp_path / "old.db"
    conn = init_db(path)
    conn.execute("DROP TABLE signal_start_ledger")                       # what a v2 database looks like
    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    for i, m in enumerate(MARKETS[:5]):
        conn.execute("INSERT INTO signal_events(bot, symbol, kind, reason, created_at, details)"
                     " VALUES ('aria', ?, 'START', 'x', ?, '{}')", (m, int(T0) + i * 600))
    conn.execute("INSERT INTO signal_events(bot, symbol, kind, reason, created_at, details)"
                 " VALUES ('aria', 'R_10', 'STOP', 'x', ?, '{}')", (int(T0) + 4000,))
    conn.close()
    conn = init_db(path)
    try:
        assert stored_schema_version(conn) == SCHEMA_VERSION == 4      # v2 -> v3 back-fill -> v4 migration
        assert len(ledger(conn, "aria")) == 5                             # STARTs only, not the STOP
        conn.close()
        conn = init_db(path)                                              # idempotent
        assert len(ledger(conn, "aria")) == 5
        eng = engine(conn, bots)
        tr, _ = cycles(eng, T0 + 5000, {("aria", MARKETS[5]): RUN})
        assert tr == []
    finally:
        conn.close()


def test_new_database_gets_latest_schema(tmp_path):
    conn = init_db(tmp_path / "new.db")
    assert stored_schema_version(conn) == SCHEMA_VERSION == 4 and ledger(conn) == []
    conn.close()


# ------------------------------------------------------------------ duplicates and failures
def test_a_start_is_counted_once(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    key = ("aria", "R_10")
    tr, t = cycles(eng, T0, {key: RUN}, n=10)                          # START once, then stays active
    assert tr == [("START", *key)] and len(ledger(conn)) == 1
    conn.execute("INSERT INTO signal_start_ledger VALUES ('aria', 'R_25', ?)", (int(t + 2 * STEP),))
    tr, _ = cycles(eng, t, {("aria", "R_25"): RUN})                    # would START at t + 2 steps
    assert tr == []                                                    # row already there: no 2nd count
    assert len(ledger(conn)) == 2


def test_failed_start_transaction_does_not_consume_or_bypass_the_limit(db, bots, caplog):
    conn, _ = db
    eng = engine(conn, bots)
    times, t = start_five(eng)
    conn.execute("DELETE FROM signal_start_ledger WHERE started_at=?", (times[0],))       # 4 in window
    key = ("aria", MARKETS[5])
    cycles(eng, t, {key: RUN}, n=2)
    t += 2 * STEP
    conn.execute("CREATE TEMP TRIGGER fail_n BEFORE INSERT ON notifications "
                 "BEGIN SELECT RAISE(ABORT, 'forced outbox failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        eng.process({key: verdict(*key, RUN)}, t)                       # START rolled back as a whole
    assert len(ledger(conn)) == 4 and not eng.store.get(*key).active
    conn.execute("DROP TRIGGER fail_n")
    assert cycle(eng, t + STEP, {key: RUN}) == [("START", *key)]       # counted exactly once now
    assert len(ledger(conn)) == 5
    assert cycles(eng, t + 2 * STEP, {("aria", MARKETS[6]): RUN})[0] == []


@pytest.mark.parametrize("fault", ["drop_table", "insert_fails"])
def test_unusable_ledger_blocks_start_but_not_stop(db, bots, caplog, fault):
    conn, _ = db
    eng = engine(conn, bots)
    active = ("delta", "R_10")
    _, t = cycles(eng, T0, {active: RUN})
    assert eng.store.get(*active).active
    if fault == "drop_table":
        conn.execute("DROP TABLE signal_start_ledger")
    else:
        conn.execute("CREATE TEMP TRIGGER fail_l BEFORE INSERT ON signal_start_ledger "
                     "BEGIN SELECT RAISE(ABORT, 'forced ledger failure'); END")
    key = ("aria", "R_25")
    with caplog.at_level(logging.INFO):
        tr, t = cycles(eng, t, {key: RUN, active: WAIT})                # START due + STOP due
    assert tr == [("STOP", *active)]                                    # STOP still sent, START not
    assert not eng.store.get(*key).active and eng.store.get(*key).last_status == LIMIT_REACHED
    assert notes(conn) == ["START", "STOP"] and conn.in_transaction is False
    assert any("event=signal_limit_check_failed" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ unchanged rules
def test_three_cycle_confirmation_and_cooldown_unchanged_under_the_limit(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    key = ("aria", "R_50")
    assert cycles(eng, T0, {key: RUN}, n=2)[0] == []                     # 2 checks: no START
    tr, t = cycles(eng, T0 + 2 * STEP, {key: RUN}, n=1)
    assert tr == [("START", *key)]
    tr, t = cycles(eng, t, {key: WAIT})
    assert tr == [("STOP", *key)]
    stop_at = t - STEP
    while t - stop_at < 1800:                                            # cooldown, limit not reached
        assert cycle(eng, t, {key: RUN}) == []
        t += STEP
    assert cycle(eng, t, {key: RUN}) == [("START", *key)]
    assert len(ledger(conn)) == 2


def test_blocked_combination_needs_fresh_consecutive_passes_after_the_window_frees(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    times, t = start_five(eng)
    key = ("aria", MARKETS[5])
    cycles(eng, t, {key: RUN}, n=5)                                     # blocked, pass streak 5
    t += 5 * STEP
    cycle(eng, t, {key: WAIT})                                          # streak broken
    t += STEP
    eng.process({}, t)                                                  # and missing for a cycle
    t = max(t + STEP, times[0] + 12 * H)                                # window has a free slot now
    assert cycle(eng, t, {key: RUN}) == [] and cycle(eng, t + STEP, {key: RUN}) == []
    assert cycle(eng, t + 2 * STEP, {key: RUN}) == [("START", *key)]   # 3 fresh consecutive passes


def test_missing_blocked_combination_loses_its_streak(db, bots):
    conn, _ = db
    eng = engine(conn, bots)
    times, t = start_five(eng)
    key = ("aria", MARKETS[5])
    cycles(eng, t, {key: RUN}, n=3)
    assert eng.store.get(*key).pass_streak == 3
    eng.process({("delta", "R_10"): verdict("delta", "R_10", WAIT)}, t + 3 * STEP)
    assert eng.store.get(*key).pass_streak == 0
    eng.discard_start_credit(t + 4 * STEP)                              # evaluation failure path too
    assert eng.store.get(*key).pass_streak == 0


# ------------------------------------------------------------------ dry-run
def test_dry_run_starts_are_recorded_and_count_toward_the_limit(db, bots):
    """Dry-run changes delivery only: START / STOP are decided and stored exactly as in live mode
    (notifications are marked 'skipped', never sent), so dry-run STARTs use up the bot's limit in
    that database."""
    conn, _ = db
    eng = engine(conn, bots)
    sender = TelegramSender(conn, "123:abc", "42", enabled=True, dry_run=True)
    _, t = start_five(eng)
    asyncio.run(sender.deliver_pending(t))
    st = [r[0] for r in conn.execute("SELECT status FROM notifications WHERE kind='START'")]
    assert st == ["skipped"] * 5 and len(ledger(conn)) == 5
    assert cycles(eng, t, {("aria", MARKETS[5]): RUN})[0] == []

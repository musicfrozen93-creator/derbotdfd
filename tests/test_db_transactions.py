"""Database write failures: rollback, recovery and no false signals.

Regression tests for the pre-deployment audit finding: a failed write used to leave the SQLite
connection inside an open transaction, so every later BEGIN failed ("cannot start a transaction
within a transaction"), tick inserts and evaluations stopped while the heartbeat stayed fresh.

Failures are forced with TEMP triggers (``RAISE(ABORT, ...)``), i.e. a real SQLite error in the
middle of the real transaction, after earlier statements of that transaction already ran.
"""
import asyncio
import logging
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

from app.analytics.validation import RUN
from app.signals.telegram import TelegramSender, enqueue
from app.storage import repositories as repo
from app.storage.database import connect, transaction, write_stats

from tests.conftest import make_rows
from tests.strategy_support import make_runnable
from tests.test_digit_rule import window
from tests.test_discovery import ALLOWED, DIGIT_SYMBOLS, FOREX, NO_DIGITS, FakeDeriv, app_factory  # noqa: F401
from tests.test_per_contract import ARIA_BOTH
from tests.test_signals import RUN as S_RUN, T0, make_engine, notes, verdict

ALL_NINES = [0] * 9 + [500]        # every last digit 9: Aria's UNDER contracts lose every trade

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import healthcheck  # noqa: E402

FAIL_CONTRACT_RESULTS = ("CREATE TEMP TRIGGER fail_cr BEFORE INSERT ON contract_results "
                         "BEGIN SELECT RAISE(ABORT, 'forced contract_results write failure'); END")


def table(conn, name, order="rowid"):
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY {order}")]


def msgs(caplog, name):
    return [r.getMessage() for r in caplog.records if f"event={name}" in r.getMessage()]


def setup_app(app_factory, monkeypatch, rng, counts, start):  # noqa: F811
    """Discovered app with stored ticks, a configured bankroll and (fake, favourable) quotes for every
    stake the evaluation needs, so combinations whose own checks pass can reach RUN_CANDIDATE."""
    app = app_factory(FakeDeriv(DIGIT_SYMBOLS + NO_DIGITS + FOREX))
    asyncio.run(app.discover())
    feed(app, rng, counts, start)
    monkeypatch.setattr(app.health, "status", lambda sym, t: ("OK", "live"))
    make_runnable(app, time.time())
    return app


def feed(app, rng, counts, start):
    for sym in ALLOWED:
        repo.insert_ticks(app.conn, make_rows(sym, window(counts, rng), start_epoch=start))
        app.cache.mark_dirty(sym)


def cycle(app):
    return asyncio.run(app.evaluate_once())


# ======================================================== the audit scenario, end to end
def test_failed_contract_results_write_rolls_back_and_cannot_start(app_factory, monkeypatch, rng, caplog):  # noqa: F811
    now = int(time.time())
    app = setup_app(app_factory, monkeypatch, rng, ARIA_BOTH, now - 500)
    cycle(app)
    res = cycle(app)                                # 2 passing checks: one short of START
    run_keys = {k for k, v in res.verdicts.items() if v.status == RUN}
    assert ("aria", "R_50") in run_keys
    assert notes(app.conn, "START") == []
    ok_at = app.last_eval_ok_at
    hist_before = dict(app.history_tuples)
    before = {t: table(app.conn, t) for t in ("evaluations", "contract_results", "signal_events",
                                              "notifications", "signal_state", "strategy_evaluations",
                                              "leg_evaluations", "evaluation_history")}

    app.conn.execute(FAIL_CONTRACT_RESULTS)
    with caplog.at_level(logging.INFO):
        res3 = cycle(app)                          # 3rd passing check, but its results cannot be saved
    assert {k for k, v in res3.verdicts.items() if v.status == RUN} == run_keys

    # 1-2. rolled back: no transaction left open, previous cycle's rows untouched (every table of the cycle)
    assert app.conn.in_transaction is False
    for t in ("evaluations", "contract_results", "strategy_evaluations", "leg_evaluations", "evaluation_history"):
        assert table(app.conn, t) == before[t], t
    assert app.history_tuples == hist_before
    # 5. no false PASS / START from the failed cycle (it would have been the 3rd consecutive pass)
    assert notes(app.conn, "START") == []
    assert table(app.conn, "signal_events") == before["signal_events"]
    assert all(app.store.get(*k).pass_streak == 0 and not app.store.get(*k).active for k in run_keys)
    # 6. logged and observable
    failed = msgs(caplog, "db_write_failed")
    assert len(failed) == 1 and "what=save_strategy_cycle" in failed[0] and "forced contract_results" in failed[0]
    assert "rolled_back=True" in failed[0]
    assert [r for r in caplog.records if "event=db_write_failed" in r.getMessage()][0].exc_info is not None
    assert len(msgs(caplog, "evaluation_save_failed")) == 1
    assert app.eval_failures_consecutive == 1 and app.last_eval_ok_at == ok_at
    assert "forced contract_results write failure" in app.last_eval_error
    stats = write_stats(app.conn)
    assert stats["failed_transactions"] == 1 and "forced" in stats["last_error"]
    assert stats["consecutive_failed"] == 0       # later writes in the same cycle (engine, alert) committed
    hb = app.heartbeat_payload()
    assert hb["eval_failures_consecutive"] == 1 and "forced" in hb["db"]["last_error"]
    # the operator alert went into the outbox (dry run: logged, never sent)
    crit = app.conn.execute("SELECT dedupe_key FROM notifications WHERE kind='CRITICAL'").fetchall()
    assert len(crit) == 1 and crit[0][0].startswith("error:evaluation_save:IntegrityError:")
    # 4. history intact: only the CRITICAL alert was added
    assert table(app.conn, "notifications")[:len(before["notifications"])] == before["notifications"]

    # a second failing cycle: still no START, no duplicate alert (cooldown)
    cycle(app)
    assert notes(app.conn, "START") == [] and len(notes(app.conn, "CRITICAL")) == 1
    assert app.eval_failures_consecutive == 2 and app.conn.in_transaction is False

    # 3. the same connection keeps working for other writes
    assert repo.insert_ticks(app.conn, make_rows("R_10", [1, 2, 3], start_epoch=now + 10_000)) == 3
    repo.meta_set(app.conn, "probe", "ok")
    assert repo.meta_get(app.conn, "probe") == "ok"

    # fault removed: evaluations are saved again, and START needs 3 NEW consecutive saved passes
    app.conn.execute("DROP TRIGGER fail_cr")
    cycle(app)
    cycle(app)
    assert notes(app.conn, "START") == []
    res = cycle(app)
    starts = app.conn.execute("SELECT dedupe_key FROM notifications WHERE kind='START'").fetchall()
    got = [tuple(k[0].split(":")[1:3]) for k in starts]
    assert len(got) == len(set(got)) and set(got) <= run_keys                                  # once each
    # every passing combination STARTs, except above the per-bot limit (5 per rolling 12 h, all markets)
    per_bot = Counter(b for b, _ in run_keys)
    assert Counter(b for b, _ in got) == {b: min(n, 5) for b, n in per_bot.items()}
    assert app.eval_failures_consecutive == 0 and app.last_eval_ok_at > ok_at
    assert write_stats(app.conn)["consecutive_failed"] == 0
    # stored rows are this cycle's and agree with every verdict
    rows = repo.load_contract_results(app.conn)               # legacy rule per leg (informational)
    assert len(rows) == sum(len(v.legs) for v in res.verdicts.values())
    strat = {(r["bot"], r["symbol"]): r for r in app.conn.execute("SELECT * FROM strategy_evaluations")}
    assert set(strat) == set(res.verdicts)
    for key, v in res.verdicts.items():
        stored = [r for r in rows if (r["bot"], r["symbol"]) == key]
        assert [r["result"] for r in stored] == [lg.legacy_rule["result"] for lg in v.legs]
        assert all(r["bot_status"] == v.status for r in stored)
        s = strat[key]
        assert (s["decision"], s["entry_status"], s["statistics_status"], s["economics_status"],
                s["risk_status"]) == v.status_tuple()
        # RUN_CANDIDATE <=> every required check passes (never the legacy digit rule)
        assert (v.status == RUN) == all(g["pass"] for g in v.gates.values() if g["required"])
    # more cycles: no duplicate START
    cycle(app)
    assert len(notes(app.conn, "START")) == len(got)


def test_stop_rules_still_run_when_evaluations_cannot_be_saved(app_factory, monkeypatch, rng):  # noqa: F811
    now = int(time.time())
    app = setup_app(app_factory, monkeypatch, rng, ARIA_BOTH, now - 500)
    for _ in range(3):
        cycle(app)
    # 1HZ10V: first of aria's 7 passing markets (the per-bot limit lets 5 of them START)
    assert app.store.get("aria", "1HZ10V").active
    n_start = len(notes(app.conn, "START"))
    # 500 older ticks back-filled, every last digit 9: over the long window aria's UNDER contracts now
    # win far less often than fair, so its statistics FAIL (and its expected value is NEGATIVE)
    feed(app, rng, ALL_NINES, now - 1000)
    app.conn.execute(FAIL_CONTRACT_RESULTS)
    cycle(app)
    cycle(app)
    assert app.store.get("aria", "1HZ10V").active      # 2 failing checks: not yet (3-check STOP unchanged)
    cycle(app)
    assert not app.store.get("aria", "1HZ10V").active
    stops = app.conn.execute("SELECT dedupe_key FROM notifications WHERE kind='STOP'").fetchall()
    assert [k[0] for k in stops if k[0].startswith("STOP:aria:1HZ10V:")] and \
        len([k for k in stops if k[0].startswith("STOP:aria:1HZ10V:")]) == 1
    assert len(notes(app.conn, "START")) == n_start and app.conn.in_transaction is False


def test_dropped_contract_results_table_no_longer_blocks_the_connection(app_factory, monkeypatch, rng):  # noqa: F811
    """The exact scenario of the audit (scenario E): the table disappears."""
    now = int(time.time())
    app = setup_app(app_factory, monkeypatch, rng, ARIA_BOTH, now - 500)
    cycle(app)
    app.conn.execute("DROP TABLE contract_results")
    cycle(app)                                         # does not raise, does not wedge the connection
    assert app.conn.in_transaction is False
    cycle(app)                                         # next cycle runs (old bug: BEGIN failed here)
    assert app.eval_failures_consecutive == 2
    assert repo.insert_ticks(app.conn, make_rows("R_10", [4, 5], start_epoch=now + 20_000)) == 2
    assert notes(app.conn, "CRITICAL")                # the alert could be queued
    assert notes(app.conn, "START") == []


# ======================================================== signal engine transaction
def test_engine_write_failure_rolls_back_start_and_restores_state(db, bots, cfg, caplog):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    key = ("aria", "1HZ10V")
    for i in range(2):
        eng.process({key: verdict(*key, S_RUN)}, T0 + i * 60)
    conn.execute("CREATE TEMP TRIGGER fail_n BEFORE INSERT ON notifications WHEN NEW.kind='START' "
                 "BEGIN SELECT RAISE(ABORT, 'forced outbox failure'); END")
    with caplog.at_level(logging.INFO), pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        eng.process({key: verdict(*key, S_RUN)}, T0 + 120)       # would START
    assert conn.in_transaction is False
    assert notes(conn) == [] and conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0] == 0
    s = eng.store.get(*key)
    assert (s.active, s.pass_streak) == (False, 2)                # memory = last committed state
    assert tuple(conn.execute("SELECT active, pass_streak FROM signal_state").fetchone()) == (0, 2)
    assert msgs(caplog, "signal_state_restored") and msgs(caplog, "db_write_failed")
    conn.execute("DROP TRIGGER fail_n")
    tr = eng.process({key: verdict(*key, S_RUN)}, T0 + 180)
    assert [t.kind for t in tr] == ["START"] and notes(conn) == ["START"]
    eng.process({key: verdict(*key, S_RUN)}, T0 + 240)
    assert notes(conn) == ["START"]                               # exactly one START


def test_engine_starts_allowed_false_blocks_start_but_not_stop(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    key = ("aria", "1HZ10V")
    for i in range(5):
        eng.process({key: verdict(*key, S_RUN)}, T0 + i * 60, starts_allowed=False)
    assert notes(conn) == [] and eng.store.get(*key).pass_streak == 0
    for i in range(3):
        eng.process({key: verdict(*key, S_RUN)}, T0 + 600 + i * 60)
    assert notes(conn) == ["START"]
    for i in range(3):
        eng.process({key: verdict(*key, "WAIT")}, T0 + 900 + i * 60, starts_allowed=False)
    assert notes(conn) == ["START", "STOP"]


# ======================================================== the transaction helper itself
def test_transaction_commit_failure_is_rolled_back_and_reraised(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute("CREATE TABLE t(a)")
    real = conn.execute

    def failing_commit(sql, *a):
        if sql == "COMMIT":
            raise sqlite3.OperationalError("database is locked")
        return real(sql, *a)
    conn.execute = failing_commit                     # AppConnection instances accept attributes
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        with transaction(conn, "t"):
            conn.execute("INSERT INTO t VALUES (1)")
    del conn.execute
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    with transaction(conn, "t"):
        conn.execute("INSERT INTO t VALUES (2)")
    assert [r[0] for r in conn.execute("SELECT a FROM t")] == [2]


def test_transaction_keeps_the_original_exception_when_sqlite_already_rolled_back(tmp_path):
    conn = connect(tmp_path / "x.db")
    with pytest.raises(ValueError, match="original"):
        with transaction(conn, "t"):
            conn.execute("ROLLBACK")                  # e.g. SQLite aborted the transaction itself
            raise ValueError("original")
    assert conn.in_transaction is False


def test_transaction_recovers_a_leaked_transaction(tmp_path, caplog):
    conn = connect(tmp_path / "x.db")
    conn.execute("CREATE TABLE t(a)")
    conn.execute("BEGIN")                             # left open by code that bypassed the helper
    conn.execute("INSERT INTO t VALUES ('leaked')")
    with caplog.at_level(logging.INFO):
        with transaction(conn, "t"):
            conn.execute("INSERT INTO t VALUES ('ok')")
    assert msgs(caplog, "db_stale_transaction_rolled_back")
    assert [r[0] for r in conn.execute("SELECT a FROM t")] == ["ok"]


def test_nested_transaction_is_refused_and_outer_is_rolled_back(tmp_path):
    conn = connect(tmp_path / "x.db")
    conn.execute("CREATE TABLE t(a)")
    with pytest.raises(RuntimeError, match="nested"):
        with transaction(conn, "outer"):
            conn.execute("INSERT INTO t VALUES (1)")
            with transaction(conn, "inner"):
                pass
    assert conn.in_transaction is False and conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    with transaction(conn, "after"):
        conn.execute("INSERT INTO t VALUES (2)")


def test_transaction_works_on_a_plain_sqlite_connection(tmp_path):
    conn = sqlite3.connect(tmp_path / "x.db", isolation_level=None)
    conn.execute("CREATE TABLE t(a UNIQUE)")
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(conn, "t"):
            conn.execute("INSERT INTO t VALUES (1)")
            conn.execute("INSERT INTO t VALUES (1)")
    assert conn.in_transaction is False and conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    assert write_stats(conn)["failed_transactions"] == 0      # counters only on AppConnection


@pytest.mark.parametrize("fn", ["upsert_markets", "replace_contracts", "save_compatibility", "meta_set",
                                "open_incident", "close_incident", "deactivate_markets", "insert_ticks"])
def test_every_repository_write_rolls_back_on_failure(db, fn):
    conn, _ = db
    from app.deriv.contracts import DigitContract, SymbolContracts
    from app.deriv.symbols import MarketInfo
    m = MarketInfo("R_10", "Volatility 10 Index", "synthetic_index", "random_index", 3, True, False)
    sc = SymbolContracts("R_10", {"DIGITOVER": DigitContract("DIGITOVER", 1, 10, None)})
    repo.upsert_markets(conn, [m])
    repo.open_incident(conn, "k", "feed", "R_10", "d")
    calls = {"upsert_markets": lambda: repo.upsert_markets(conn, [m]),
             "replace_contracts": lambda: repo.replace_contracts(conn, "R_10", sc),
             "save_compatibility": lambda: repo.save_compatibility(conn, [("aria", "R_10", True, "ok")]),
             "meta_set": lambda: repo.meta_set(conn, "a", "b"),
             "open_incident": lambda: repo.open_incident(conn, "k2", "feed", "R_10", "d"),
             "close_incident": lambda: repo.close_incident(conn, "k"),
             "deactivate_markets": lambda: repo.deactivate_markets(conn, ["R_10"]),
             "insert_ticks": lambda: repo.insert_ticks(conn, make_rows("R_10", [1, 2]))}
    tables = {"upsert_markets": "markets", "replace_contracts": "contracts", "save_compatibility": "compatibility",
              "meta_set": "meta", "open_incident": "incidents", "close_incident": "incidents",
              "deactivate_markets": "markets", "insert_ticks": "ticks"}
    tbl = tables[fn]
    before = table(conn, tbl, "1")
    for op in ("INSERT", "UPDATE"):
        conn.execute(f"CREATE TEMP TRIGGER f_{op} BEFORE {op} ON {tbl} BEGIN SELECT RAISE(ABORT, 'forced'); END")
    with pytest.raises(sqlite3.IntegrityError, match="forced"):
        calls[fn]()
    assert conn.in_transaction is False and table(conn, tbl, "1") == before
    conn.execute("DROP TRIGGER f_INSERT")
    conn.execute("DROP TRIGGER f_UPDATE")
    calls[fn]()                                         # the next valid write succeeds


# ======================================================== Telegram outbox
def test_notification_delivered_but_not_recorded_is_never_sent_twice(db):
    conn, _ = db
    sent = []
    sender = TelegramSender(conn, "token", "chat", enabled=True, dry_run=False, transport=sent.append)
    enqueue(conn, "START", "START:aria:R_50:1", "start text", T0)
    conn.execute("CREATE TEMP TRIGGER f BEFORE UPDATE ON notifications BEGIN SELECT RAISE(ABORT, 'forced'); END")
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(sender.deliver_pending(T0))
    assert sent == ["start text"] and conn.in_transaction is False
    with pytest.raises(sqlite3.IntegrityError):         # still failing: retried as a DB update only
        asyncio.run(sender.deliver_pending(T0 + 5))
    assert sent == ["start text"]
    conn.execute("DROP TRIGGER f")
    asyncio.run(sender.deliver_pending(T0 + 10))
    assert sent == ["start text"]                       # never re-sent
    assert conn.execute("SELECT status FROM notifications").fetchone()[0] == "sent"


def test_enqueue_failure_is_rolled_back_and_dedupe_still_works(db):
    conn, _ = db
    conn.execute("CREATE TEMP TRIGGER f BEFORE INSERT ON notifications BEGIN SELECT RAISE(ABORT, 'forced'); END")
    with pytest.raises(sqlite3.IntegrityError):
        enqueue(conn, "STOP", "STOP:a:b:1", "x", T0)
    assert conn.in_transaction is False
    conn.execute("DROP TRIGGER f")
    assert enqueue(conn, "STOP", "STOP:a:b:1", "x", T0) is True
    assert enqueue(conn, "STOP", "STOP:a:b:1", "x", T0) is False         # deduplicated
    assert notes(conn) == ["STOP"]


# ======================================================== health check
def test_healthcheck_detects_stale_evaluations_with_startup_grace():
    now = 2_000_000_000
    base = {"written_at": now - 10, "connected": True, "last_tick_wall": now - 1, "monitored_symbols": ["R_10"]}
    # fresh start, first evaluation not done yet (discovery / initial download): healthy
    assert healthcheck.check(dict(base, started_at=now - 300, last_eval_ok_at=None), now, 120, 300)[0]
    # running for 15 min and never saved an evaluation: unhealthy
    ok, why = healthcheck.check(dict(base, started_at=now - 900, last_eval_ok_at=None,
                                     eval_failures_consecutive=14,
                                     db={"last_error": "save_evaluations: OperationalError: disk I/O error"}),
                                now, 120, 300)
    assert not ok and "since start" in why and "disk I/O error" in why
    # evaluations saved recently: healthy
    ok, why = healthcheck.check(dict(base, started_at=now - 9000, last_eval_ok_at=now - 70), now, 120, 300)
    assert ok and "last saved evaluation 70 s ago" in why
    # last saved evaluation 11 min ago (writes failing): unhealthy
    ok, why = healthcheck.check(dict(base, started_at=now - 9000, last_eval_ok_at=now - 660,
                                     last_eval_error="save_evaluations: IntegrityError: x"), now, 120, 300)
    assert not ok and "since the last good cycle" in why and "IntegrityError" in why
    # limit is configurable / can be disabled; older heartbeats (no field) are not judged on it
    assert healthcheck.check(dict(base, started_at=now - 9000, last_eval_ok_at=now - 660), now, 120, 300, 0)[0]
    assert healthcheck.check(dict(base, started_at=now - 9000), now, 120, 300)[0]


def test_evaluations_during_initial_download_keep_the_service_healthy(app_factory, monkeypatch, rng):  # noqa: F811
    """While the history is still downloading the verdicts are INSUFFICIENT_DATA, but each cycle is
    still evaluated and saved, so the stale-evaluation check stays green."""
    now = int(time.time())
    app = setup_app(app_factory, monkeypatch, rng, [10] * 10, now - 100)   # only 100 of 500 ticks so far
    res = cycle(app)
    st = {v.status for v in res.verdicts.values()}
    # not enough ticks yet -> INSUFFICIENT_DATA; a bot whose recovery sequence is UNSAFE does not need
    # ticks to be judged and is AVOID (critical risk) already. Either way: no START.
    assert st <= {"INSUFFICIENT_DATA", "AVOID"} and "INSUFFICIENT_DATA" in st
    assert all(v.critical_kind == "risk" for v in res.verdicts.values() if v.status == "AVOID")
    assert app.last_eval_ok_at is not None and app.eval_failures_consecutive == 0
    hb = dict(app.heartbeat_payload(), written_at=now)
    ok, why = healthcheck.check(hb, now + 30, 120, 300)
    assert ok, why
    ok, why = healthcheck.check(hb, now + 30 + 600, 1e9, 1e9)   # ... but not if cycles then stop
    assert not ok

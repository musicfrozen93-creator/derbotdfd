"""Schema migration v4: upgrade keeps every old record, rollback removes only v4 tables, both atomic."""
import sqlite3

import pytest

from app.storage import migrations
from app.storage.database import SCHEMA_VERSION, connect, init_db, stored_schema_version

V4_TABLES = {"quotes", "platform_limits", "strategy_evaluations", "leg_evaluations", "evaluation_history",
             "schema_migrations"}


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def make_v3_database(path):
    """A v3 database with history in every pre-existing table (as written by the previous release)."""
    conn = init_db(path, migrate_to=3)
    assert stored_schema_version(conn) == 3 and not (tables(conn) & (V4_TABLES - {"schema_migrations"}))
    conn.execute("INSERT INTO ticks(symbol, epoch, quote, digit) VALUES ('R_10', 1, '1000.123', 3)")
    conn.execute("INSERT INTO signal_events(bot, symbol, kind, reason, created_at, details)"
                 " VALUES ('aria', 'R_10', 'START', 'old rule', 100, '{}')")
    conn.execute("INSERT INTO signal_start_ledger(bot, symbol, started_at) VALUES ('aria', 'R_10', 100)")
    conn.execute("INSERT INTO signal_state(bot, symbol, active, pass_streak, fail_streak) VALUES ('aria','R_10',1,0,0)")
    conn.execute("INSERT INTO evaluations(bot, symbol, status, evaluated_at, summary) VALUES ('aria','R_10','WAIT',100,'{}')")
    conn.execute("INSERT INTO contract_results(bot, symbol, leg_id, result) VALUES ('aria','R_10','base','FAIL')")
    conn.execute("INSERT INTO notifications(kind, dedupe_key, text, created_at) VALUES ('START','k1','t',100)")
    return conn


def snapshot(conn):
    out = {}
    for t in ("ticks", "signal_events", "signal_start_ledger", "signal_state", "evaluations", "contract_results",
              "notifications"):
        out[t] = sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}"))   # ticks is WITHOUT ROWID
    return out


def test_new_database_is_created_at_latest_version(tmp_path):
    conn = init_db(tmp_path / "n.db")
    assert stored_schema_version(conn) == SCHEMA_VERSION == migrations.LATEST == 4
    assert V4_TABLES <= tables(conn)
    assert conn.execute("SELECT version FROM schema_migrations").fetchall()[0][0] == 4
    conn.close()


def test_upgrade_from_v3_keeps_every_record(tmp_path):
    path = tmp_path / "old.db"
    conn = make_v3_database(path)
    before = snapshot(conn)
    conn.close()
    conn = init_db(path)                                   # the new release starts on the old database
    try:
        assert stored_schema_version(conn) == 4 and V4_TABLES <= tables(conn)
        assert snapshot(conn) == before                     # nothing old changed or deleted
        conn.close()
        conn = init_db(path)                               # idempotent restart
        assert stored_schema_version(conn) == 4 and snapshot(conn) == before
    finally:
        conn.close()


def test_rollback_to_v3_removes_only_v4_tables_and_reupgrade_works(tmp_path):
    path = tmp_path / "r.db"
    conn = make_v3_database(path)
    conn.close()
    conn = init_db(path)
    conn.execute("INSERT INTO quotes(symbol, contract_type, barrier, duration, stake, ok, fetched_at)"
                 " VALUES ('R_10','DIGITOVER','3',1,'1.00',1,1)")
    before = snapshot(conn)
    assert migrations.rollback(conn, 3) == [4]
    assert stored_schema_version(conn) == 3
    assert not (tables(conn) & V4_TABLES)
    assert snapshot(conn) == before                         # v1-v3 data untouched by the rollback
    assert migrations.migrate(conn) == [4]
    assert stored_schema_version(conn) == 4 and V4_TABLES <= tables(conn)
    conn.close()


def test_rollback_below_base_is_refused(db):
    conn, _ = db
    with pytest.raises(ValueError):
        migrations.rollback(conn, 2)


def test_failed_migration_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "f.db"
    conn = make_v3_database(path)
    before = snapshot(conn)
    broken = migrations.Migration(4, "broken", up=migrations.V4.up[:3] + ("CREATE TABLE oops (",), down=())
    monkeypatch.setattr(migrations, "MIGRATIONS", (broken,))
    with pytest.raises(sqlite3.OperationalError):
        migrations.migrate(conn)
    assert stored_schema_version(conn) == 3                 # version not advanced
    assert not (tables(conn) & V4_TABLES)                   # the tables created before the error were rolled back
    assert snapshot(conn) == before
    assert not conn.in_transaction
    conn.close()


def test_old_release_reopening_a_v4_database_then_new_release_again(tmp_path):
    """An older release resets meta.schema_version to 3 but keeps the v4 tables; the new release then
    re-applies v4 idempotently (CREATE ... IF NOT EXISTS) without losing quotes."""
    path = tmp_path / "x.db"
    conn = init_db(path)
    conn.execute("INSERT INTO quotes(symbol, contract_type, barrier, duration, stake, ok, fetched_at)"
                 " VALUES ('R_10','DIGITOVER','3',1,'1.00',1,1)")
    conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    conn.close()
    conn = init_db(path)
    assert stored_schema_version(conn) == 4
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
    conn.close()


def test_readonly_connection_reads_old_and_new_tables(tmp_path):
    path = tmp_path / "ro.db"
    init_db(path).close()
    ro = connect(path, readonly=True)
    assert ro.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0
    assert ro.execute("SELECT COUNT(*) FROM strategy_evaluations").fetchone()[0] == 0
    ro.close()

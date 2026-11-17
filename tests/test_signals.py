"""9-10, 13-14. Signal state transitions, duplicate prevention, persistence, restart recovery."""
from app.analytics.validation import AVOID, INSUFFICIENT, RUN, WAIT, BotVerdict, SymbolQuality
from app.signals.engine import SignalEngine
from app.signals.state import SignalStateStore
from app.signals.telegram import MessageFormatter

T0 = 1_800_000_000.0


def verdict(bot="aria", sym="1HZ10V", status=RUN, critical_kind=None, hold=False):
    q = SymbolQuality(sym, "CLEAN", "clean", "OK", 100000)
    return BotVerdict(bot, bot.title(), sym, sym, status, critical_kind is not None,
                      [] if status == RUN else [f"{status} reason"], q, critical_kind != "contract",
                      "ok", critical_kind=critical_kind, hold=hold)


def make_engine(conn, bots, cfg, started_at=T0 - 10_000):
    store = SignalStateStore(conn)
    fmt = MessageFormatter(bots, "Asia/Colombo")
    return SignalEngine(conn, store, fmt, cfg.signals, started_at=started_at)


def notes(conn, kind=None):
    q = "SELECT kind FROM notifications"
    rows = conn.execute(q).fetchall()
    return [r[0] for r in rows if kind is None or r[0] == kind]


def run_cycles(eng, statuses, key=("aria", "1HZ10V"), t=T0, step=60, **kw):
    out = []
    for i, st in enumerate(statuses):
        out += eng.process({key: verdict(key[0], key[1], st, **kw)}, t + i * step)
    return out, t + len(statuses) * step


def test_start_needs_confirmation_and_is_sent_once(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    tr, _ = run_cycles(eng, [RUN, RUN])
    assert tr == [] and notes(conn) == []
    tr, _ = run_cycles(eng, [RUN, RUN, RUN, RUN, RUN], t=T0 + 120)
    assert [t.kind for t in tr] == ["START"]
    assert notes(conn) == ["START"]


def test_no_wait_messages_and_stop_once(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN] * 3)
    tr, t = run_cycles(eng, [WAIT, WAIT], t=t)
    assert tr == []                                  # not yet confirmed
    tr, t = run_cycles(eng, [WAIT] * 10 + [INSUFFICIENT, AVOID], t=t)
    assert [x.kind for x in tr] == ["STOP"]
    assert notes(conn) == ["START", "STOP"]
    assert not any(k.startswith("WAIT") for k in notes(conn))


def test_brief_dip_does_not_stop(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN] * 3)
    tr, t = run_cycles(eng, [WAIT, WAIT, RUN, WAIT, RUN, RUN], t=t)
    assert tr == [] and notes(conn) == ["START"]


def test_critical_contract_fault_stops_immediately(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN] * 3)
    tr, t = run_cycles(eng, [AVOID], t=t, critical_kind="contract")
    assert [x.kind for x in tr] == ["STOP"] and tr[0].critical


def test_data_fault_during_startup_grace_is_frozen(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN] * 3)
    eng2 = make_engine(conn, bots, cfg, started_at=t)      # simulated restart
    tr, t2 = run_cycles(eng2, [AVOID] * 4, t=t + 1, critical_kind="data")
    assert tr == []                                         # grace: no STOP on data catch-up
    tr, _ = run_cycles(eng2, [AVOID], t=t + 600, critical_kind="data")
    assert [x.kind for x in tr] == ["STOP"]                 # after grace: immediate critical STOP


def test_data_quality_dip_hold_keeps_active(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN] * 3)
    tr, t = run_cycles(eng, [WAIT] * 10, t=t, hold=True)
    assert tr == []
    # but an inactive combination cannot START on unclean data
    for i in range(5):
        tr = eng.process({("aria", "1HZ10V"): verdict("aria", "1HZ10V", WAIT, hold=True),
                          ("delta", "R_10"): verdict("delta", "R_10", WAIT, hold=True)}, t + i * 60)
        assert tr == []
    assert eng.store.get("aria", "1HZ10V").active and not eng.store.get("delta", "R_10").active


def test_restart_cooldown_prevents_oscillation(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN] * 3)
    _, t = run_cycles(eng, [WAIT] * 3, t=t)
    tr, t = run_cycles(eng, [RUN] * 10, t=t)                 # within 30 min of STOP
    assert tr == []
    tr, _ = run_cycles(eng, [RUN] * 3, t=t + 1800)
    assert [x.kind for x in tr] == ["START"]
    assert notes(conn) == ["START", "STOP", "START"]


def test_independent_combinations(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    keys = [("aria", "1HZ10V"), ("aria", "1HZ25V"), ("aria", "R_100"), ("delta", "R_10")]
    for i in range(3):
        eng.process({k: verdict(k[0], k[1], RUN) for k in keys[:3]} | {keys[3]: verdict("delta", "R_10", WAIT)},
                    T0 + i * 60)
    assert notes(conn) == ["START"] * 3
    # one market fails: only that one stops
    for i in range(3):
        eng.process({keys[0]: verdict("aria", "1HZ10V", RUN), keys[1]: verdict("aria", "1HZ25V", AVOID),
                     keys[2]: verdict("aria", "R_100", RUN)}, T0 + 600 + i * 60)
    rows = conn.execute("SELECT kind, text FROM notifications ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["START", "START", "START", "STOP"]
    assert "1HZ25V" in rows[3][1] and "Aria" in rows[3][1]


def test_restart_recovery_no_duplicate_start(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN] * 4)
    eng2 = make_engine(conn, bots, cfg, started_at=T0 - 10_000)     # new process, same database
    assert eng2.store.get("aria", "1HZ10V").active
    tr, _ = run_cycles(eng2, [RUN] * 10, t=t)
    assert tr == [] and notes(conn) == ["START"]


def test_active_but_unmonitored_combination_stops(db, bots, cfg):
    conn, _ = db
    eng = make_engine(conn, bots, cfg)
    _, t = run_cycles(eng, [RUN] * 3)
    tr = eng.process({}, t, {("aria", "1HZ10V"): "market no longer listed by Deriv active_symbols"})
    assert [x.kind for x in tr] == ["STOP"]
    tr = eng.process({}, t + 60)
    assert tr == [] and notes(conn) == ["START", "STOP"]

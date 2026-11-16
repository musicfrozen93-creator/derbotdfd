"""12-14. Historical back-fill, tick processing, database persistence, restart recovery."""
import asyncio
import time

import numpy as np

from app.analytics.analyzer import SeriesCache
from app.config import Section
from app.deriv.symbols import MarketInfo
from app.market.collector import Collector
from app.market.health import FAULT, OK, STALE, HealthMonitor
from app.market.processor import TickProcessor
from app.storage import repositories as repo
from app.storage.database import connect, init_db

from tests.conftest import FakeHistoryClient

M = MarketInfo("R_10", "Volatility 10 Index", "synthetic_index", "random_index", 3, True, False)


def coll_cfg(**kw):
    base = dict(history_page_size=1000, bootstrap_target_ticks=3500, bootstrap_max_days=14,
                gap_audit_lookback_hours=6, gap_audit_interval_minutes=10, flush_interval_s=1)
    base.update(kw)
    return Section(base)


def dataset(n, end=None, step=2):
    end = end or int(time.time())
    epochs = [end - (n - 1 - i) * step for i in range(n)]
    quotes = [f"4849.{(i * 7) % 1000:03d}" for i in range(n)]
    return epochs, quotes


def test_wal_and_tick_dedupe(db):
    conn, path = db
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    rows = [("R_10", 1, "4849.430", 0), ("R_10", 2, "4849.431", 1)]
    assert repo.insert_ticks(conn, rows) == 2
    assert repo.insert_ticks(conn, rows + [("R_10", 3, "4849.432", 2)]) == 1
    e, d, p = repo.load_series_rows(conn, "R_10", 2)
    assert e.tolist() == [2, 3] and d.tolist() == [1, 2] and p.tolist() == [4849431, 4849432]
    conn.close()
    conn2 = connect(path)                         # persisted across connections
    assert repo.tick_bounds(conn2, "R_10") == (1, 3, 3)
    assert repo.get_tick(conn2, "R_10", 1)["quote"] == "4849.430"


def test_processor_digits_duplicates_gaps_and_restart(db):
    conn, path = db
    h = HealthMonitor()
    h.set_nominal("R_10", 2)
    p = TickProcessor(conn, h)
    p.set_symbol("R_10", 3)
    from decimal import Decimal
    p.on_tick({"symbol": "R_10", "epoch": 100, "quote": Decimal("4849.430"), "pip_size": 3})
    p.on_tick({"symbol": "R_10", "epoch": 100, "quote": Decimal("4849.430"), "pip_size": 3})   # duplicate
    p.on_tick({"symbol": "R_10", "epoch": 102, "quote": 4849.5, "pip_size": 3})                # float input
    p.on_tick({"symbol": "R_10", "epoch": 120, "quote": Decimal("4849.6"), "pip_size": 3})     # missing ticks
    p.on_tick({"symbol": "R_10", "epoch": 110, "quote": Decimal("4849.7"), "pip_size": 3})     # late / out of order
    p.on_tick({"symbol": "R_10", "epoch": "x"})                                                # malformed
    p.on_tick({"symbol": "OTHER", "epoch": 1, "quote": 1, "pip_size": 2})                      # not monitored
    assert p.flush() == 4
    hh = h.get("R_10")
    assert hh.duplicates == 1 and hh.missing_events == 1 and hh.out_of_order == 1 and hh.malformed == 1
    rows = conn.execute("SELECT epoch, quote, digit FROM ticks ORDER BY epoch").fetchall()
    assert [tuple(r) for r in rows] == [(100, "4849.430", 0), (102, "4849.500", 0), (110, "4849.700", 0),
                                        (120, "4849.600", 0)]
    # restart: a new processor resumes from the stored last epoch, so a replayed tick is a duplicate
    p2 = TickProcessor(conn, HealthMonitor())
    p2.set_symbol("R_10", 3)
    assert p2.last_epoch["R_10"] == 120
    p2.on_tick({"symbol": "R_10", "epoch": 120, "quote": Decimal("4849.6"), "pip_size": 3})
    assert p2.flush() == 0


def test_two_second_index_is_not_reported_as_missing_ticks(db):
    """R_10 ticks every 2 s. Before the interval is known nothing is flagged; once learned from
    live ticks (or from stored history) a regular 2 s spacing is clean and a real gap is flagged."""
    from decimal import Decimal
    conn, _ = db
    h = HealthMonitor()
    p = TickProcessor(conn, h)
    p.set_symbol("R_10", 3)
    for e in range(1000, 1040, 2):
        p.on_tick({"symbol": "R_10", "epoch": e, "quote": Decimal("4849.430"), "pip_size": 3})
    assert h.get("R_10").nominal_interval == 2 and h.get("R_10").missing_events == 0
    p.on_tick({"symbol": "R_10", "epoch": 1060, "quote": Decimal("4849.431"), "pip_size": 3})
    assert h.get("R_10").missing_events == 1
    p.flush()
    # a new process seeds the interval from the stored ticks
    h2 = HealthMonitor()
    TickProcessor(conn, h2).set_symbol("R_10", 3)
    assert h2.get("R_10").nominal_known and h2.get("R_10").nominal_interval == 2


def test_health_states():
    h = HealthMonitor(stale_factor=10, critical_stale_s=120, critical_disconnect_s=120)
    h.set_connected(True, 1000)
    h.on_subscribed("R_10", 1000)
    assert h.status("R_10", 1001)[0] == STALE          # subscribed, no tick yet
    h.on_tick("R_10", 5, 1001)
    h.set_nominal("R_10", 2)
    assert h.status("R_10", 1005)[0] == OK
    assert h.status("R_10", 1030)[0] == STALE
    assert h.status("R_10", 1125)[0] == FAULT
    h.set_connected(False, 2000)
    assert not h.connection_fault(2100) and h.connection_fault(2121)


def _collector(conn, client, cfg=None, inserted=None):
    h = HealthMonitor()
    p = TickProcessor(conn, h)
    c = Collector(client, conn, p, h, cfg or coll_cfg(),
                  on_history_inserted=(lambda s, e: inserted.append((s, e))) if inserted is not None else None)
    return c


def test_bootstrap_backfill_pages_backwards(db):
    conn, _ = db
    ep, qs = dataset(5000)
    client = FakeHistoryClient(ep, qs, pip=3)
    inserted = []
    c = _collector(conn, client, inserted=inserted)

    async def go():
        c.symbols = {"R_10": M}
        c.processor.set_symbol("R_10", 3)
        await c._bootstrap("R_10")
    asyncio.run(go())
    first, last, count = repo.tick_bounds(conn, "R_10")
    assert count >= 3500 and last == ep[-1]
    assert len([x for x in client.calls if "ticks_history" in x]) == 4    # 1,000 per page
    assert inserted and "R_10" in c.bootstrap_done
    # all digits come from correctly formatted 3-decimal quotes
    q = conn.execute("SELECT quote, digit FROM ticks WHERE symbol='R_10' LIMIT 50").fetchall()
    assert all(len(r[0].split(".")[1]) == 3 and int(r[0][-1]) == r[1] for r in q)


def test_gap_fill_after_reconnect(db):
    conn, _ = db
    ep, qs = dataset(3000)
    client = FakeHistoryClient(ep, qs, pip=3)
    # stored: first 1000 and last 500 -> a hole in the middle
    rows = [("R_10", e, q, int(q[-1])) for e, q in zip(ep, qs)]
    repo.insert_ticks(conn, rows[:1000] + rows[2500:])
    c = _collector(conn, client)

    async def go():
        c.symbols = {"R_10": M}
        await c._fill_range("R_10", ep[1000], ep[2499])
    asyncio.run(go())
    assert repo.tick_bounds(conn, "R_10")[2] == 3000
    eps = repo.epochs_between(conn, "R_10", ep[0], ep[-1])
    assert np.all(np.diff(eps) == 2)


def test_gap_audit_queues_holes_once(db):
    conn, _ = db
    ep, qs = dataset(3000)
    rows = [("R_10", e, q, int(q[-1])) for e, q in zip(ep, qs)]
    repo.insert_ticks(conn, rows[:1000] + rows[1100:])
    c = _collector(conn, FakeHistoryClient(ep, qs, pip=3))
    c.symbols = {"R_10": M}

    async def go():
        await c.audit_gaps(lambda s, e: 20)
        n1 = c.queue.qsize()
        await c.audit_gaps(lambda s, e: 20)
        return n1, c.queue.qsize()
    n1, n2 = asyncio.run(go())
    assert n1 == 1 and n2 == 1
    job = c.queue.get_nowait()
    assert job == ("gap", "R_10", ep[999] + 1, ep[1100] - 1)


def test_set_symbols_subscribes_and_removes(db):
    conn, _ = db
    client = FakeHistoryClient([], [], pip=3)
    c = _collector(conn, client)
    m2 = MarketInfo("1HZ10V", "Volatility 10 (1s) Index", "synthetic_index", "random_index", 2, True, False)

    async def go():
        await c.set_symbols({"R_10": M, "1HZ10V": m2}, ["1HZ10V"])
        assert client.subscribed == ["1HZ10V", "R_10"]          # priority order
        await c.set_symbols({"1HZ10V": m2}, ["1HZ10V"])
        assert "R_10" not in c.subscriptions and "R_10" not in c.processor.allowed
    asyncio.run(go())


def test_restart_queues_downtime_gap_before_bootstrap(db):
    conn, _ = db
    repo.insert_ticks(conn, [("R_10", 1000, "4849.430", 0)])     # left by a previous run
    c = _collector(conn, FakeHistoryClient([], [], pip=3))
    asyncio.run(c.set_symbols({"R_10": M}, []))
    jobs = [c.queue.get_nowait() for _ in range(c.queue.qsize())]
    assert jobs[0][:3] == ("gap", "R_10", 1001) and jobs[0][3] >= int(time.time()) - 5
    assert jobs[1] == ("bootstrap", "R_10")


def test_series_cache_incremental_and_dirty_reload(tmp_path):
    path = tmp_path / "c.db"
    conn = init_db(path)
    repo.insert_ticks(conn, [("S", e, f"1.{e % 10}0", 0) for e in range(100, 200)])
    cache = SeriesCache(path, 50)
    s = cache.get("S")
    assert s.n == 50 and s.epochs[-1] == 199
    repo.insert_ticks(conn, [("S", e, "1.00", 0) for e in range(200, 210)])
    s = cache.get("S")
    assert s.n == 50 and s.epochs[-1] == 209 and s.epochs[0] == 160
    cache.mark_history("S", 150)          # back-fill inside the cached range -> reload
    assert "S" in cache._dirty
    cache.get("S")
    assert "S" not in cache._dirty
    conn.close()


def test_series_load_in_chunks_matches_rows(db):
    """load_series_rows reads 20,000 rows at a time; results must be identical across chunk edges."""
    conn, _ = db
    n = 45_001
    rows = [("C", 1000 + i, f"{i // 100}.{i % 100:02d}", i % 10) for i in range(n)]
    repo.insert_ticks(conn, rows)
    e, d, p = repo.load_series_rows(conn, "C", 50_000)
    assert e.size == n and e[0] == 1000 and e[-1] == 1000 + n - 1 and np.all(np.diff(e) == 1)
    assert d.tolist() == [i % 10 for i in range(n)] and p.tolist() == list(range(n))
    e2, _, _ = repo.load_series_rows(conn, "C", 30_000)                 # newest 30k only
    assert e2.size == 30_000 and e2[-1] == 1000 + n - 1
    e3, _, p3 = repo.load_series_rows(conn, "C", 50_000, after_epoch=1000 + n - 3)
    assert e3.tolist() == [1000 + n - 2, 1000 + n - 1] and p3.tolist() == [n - 2, n - 1]
    assert repo.load_series_rows(conn, "NONE", 10)[0].size == 0

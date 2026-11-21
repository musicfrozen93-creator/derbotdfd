"""START / STOP with bot-specific verdicts (StrategyVerdict): critical risk STOP, cooldown, the per-bot
START limit across restarts, no stale START credit, no duplicates, and the Telegram texts."""
import re

import pytest

from app.analytics.validation import AVOID, RUN, WAIT, SymbolQuality
from app.economics import ev as E
from app.risk import engine as R
from app.signals.engine import LIMIT_REACHED, SignalEngine
from app.signals.state import SignalStateStore
from app.signals.telegram import MessageFormatter, TelegramSender
from app.strategy_model.evaluate import StrategyVerdict

from tests.strategy_support import deriv_like_payout, favourable_payout
from tests.test_strategy_evaluation import ARIA_GOOD, Setup, digits_with

T0 = 1_800_000_000.0
MARKETS = ["R_10", "R_25", "R_50", "R_75", "R_100", "1HZ10V", "1HZ50V"]


def sv(bot="aria", sym="1HZ10V", status=RUN, critical_kind=None, hold=False, reasons=None):
    """Minimal verdict: the engine reads only status / critical / hold / reasons / summary()."""
    q = SymbolQuality(sym, "CLEAN", "clean", "OK", 100000)
    return StrategyVerdict(bot, bot.title(), sym, sym, status=status, critical=critical_kind is not None,
                           critical_kind=critical_kind, hold=hold, quality=q,
                           reasons=reasons if reasons is not None else ([] if status == RUN else [f"{status} reason"]))


def engine(conn, bots, cfg, started_at=T0 - 10_000):
    fmt = MessageFormatter(bots, "Asia/Colombo", cooldown_minutes=float(cfg.signals.restart_cooldown_minutes))
    return SignalEngine(conn, SignalStateStore(conn), fmt, cfg.signals, started_at=started_at)


def kinds(conn, kind=None):
    rows = conn.execute("SELECT kind, dedupe_key, text FROM notifications ORDER BY id").fetchall()
    return [r for r in rows if kind is None or r["kind"] == kind]


def start(eng, key=("aria", "1HZ10V"), t=T0):
    out = []
    for i in range(3):
        out += eng.process({key: sv(*key)}, t + 60 * i)
    assert [x.kind for x in out] == ["START"]
    return t + 180


# ---------------------------------------------------------------- critical risk STOP
def test_risk_critical_stop_is_immediate_even_in_the_startup_grace(db, bots, cfg):
    conn, _ = db
    t = start(engine(conn, bots, cfg))
    eng = engine(conn, bots, cfg, started_at=t)                      # restarted: grace period running
    tr = eng.process({("aria", "1HZ10V"): sv(status=AVOID, critical_kind="data")}, t + 10)
    assert tr == []                                                   # data faults are frozen in grace ...
    tr = eng.process({("aria", "1HZ10V"): sv(status=AVOID, critical_kind="risk",
                                             reasons=["risk UNSAFE: trade 7 is refused"])}, t + 70)
    assert [(x.kind, x.critical) for x in tr] == [("STOP", True)]    # ... a risk fault is not
    assert "risk UNSAFE" in tr[0].reason


def test_non_critical_failures_need_three_cycles(db, bots, cfg):
    conn, _ = db
    eng = engine(conn, bots, cfg)
    t = start(eng)
    key = ("aria", "1HZ10V")
    assert eng.process({key: sv(status=WAIT, reasons=["economics NEGATIVE: ..."])}, t) == []
    assert eng.process({key: sv(status=WAIT)}, t + 60) == []
    tr = eng.process({key: sv(status=WAIT)}, t + 120)
    assert [(x.kind, x.critical) for x in tr] == [("STOP", False)]


# ---------------------------------------------------------------- cooldown blocks only START
def test_cooldown_blocks_only_start(db, bots, cfg):
    conn, _ = db
    eng = engine(conn, bots, cfg)
    a, b = ("aria", "1HZ10V"), ("aria", "R_10")
    for i in range(3):
        eng.process({a: sv(*a), b: sv(*b)}, T0 + 60 * i)
    t = T0 + 180
    assert eng.store.get(*a).active and eng.store.get(*b).active
    tr = eng.process({a: sv(*a, status=AVOID, critical_kind="risk"), b: sv(*b)}, t)
    assert [(x.kind, x.symbol) for x in tr] == [("STOP", "1HZ10V")]
    for i in range(1, 29):                                            # 28 min of RUN: still cooling down
        assert eng.process({a: sv(*a), b: sv(*b)}, t + 60 * i) == []
    # b's STOP is not delayed by a's cooldown
    tr = eng.process({a: sv(*a), b: sv(*b, status=AVOID, critical_kind="risk")}, t + 60 * 29)
    assert [(x.kind, x.symbol) for x in tr] == [("STOP", "R_10")]
    tr = eng.process({a: sv(*a), b: sv(*b, status=AVOID, critical_kind="risk")}, t + 60 * 30)
    assert [(x.kind, x.symbol) for x in tr] == [("START", "1HZ10V")]   # 30 min after its STOP: START again


# ---------------------------------------------------------------- per-bot START limit
def test_start_limit_is_per_bot_durable_and_never_blocks_stop(db, bots, cfg):
    conn, _ = db
    eng = engine(conn, bots, cfg)
    aria = [("aria", s) for s in MARKETS[:6]]
    delta = ("delta", "R_10")

    def cycle(e, t, over=None):                    # every monitored combination is in every cycle
        return e.process({k: (over or {}).get(k, sv(*k)) for k in aria + [delta]}, t)

    for i in range(3):
        cycle(eng, T0 + 60 * i)
    starts = kinds(conn, "START")
    assert len(starts) == 6                        # 5 aria (its limit) + delta (not limited by aria)
    assert sum(1 for r in starts if r["dedupe_key"].startswith("START:aria:")) == 5
    blocked = [k for k in aria if not eng.store.get(*k).active]
    assert len(blocked) == 1 and eng.store.get(*blocked[0]).last_status == LIMIT_REACHED
    # restart: the ledger in SQLite still holds aria's 5 STARTs
    eng2 = engine(conn, bots, cfg, started_at=T0 + 1000)
    for i in range(3):
        assert not [x for x in cycle(eng2, T0 + 1000 + 60 * i) if x.kind == "START"]
    # at the limit, a STOP is still sent immediately
    first = [k for k in aria if eng2.store.get(*k).active][0]
    tr = cycle(eng2, T0 + 1300, {first: sv(*first, status=AVOID, critical_kind="risk")})
    assert [(x.kind, (x.bot, x.symbol)) for x in tr] == [("STOP", first)]
    # 12 h after the first STARTs the slots are free again: the blocked market STARTs
    later = T0 + 12 * 3600 + 120
    got = []
    for i in range(3):
        got += [(x.kind, (x.bot, x.symbol)) for x in cycle(eng2, later + 60 * i)]
    assert ("START", blocked[0]) in got and not [g for g in got if g[0] == "STOP"]


# ---------------------------------------------------------------- no stale START credit
def test_missing_or_failed_cycle_removes_start_credit(db, bots, cfg):
    conn, _ = db
    eng = engine(conn, bots, cfg)
    key, other = ("aria", "1HZ10V"), ("delta", "R_10")
    eng.process({key: sv(*key)}, T0)
    eng.process({key: sv(*key)}, T0 + 60)
    eng.process({other: sv(*other, status=WAIT)}, T0 + 120)          # key missing from this cycle
    assert eng.store.get(*key).pass_streak == 0
    eng.process({key: sv(*key)}, T0 + 180)
    eng.process({key: sv(*key)}, T0 + 240, starts_allowed=False)     # results could not be saved
    assert eng.store.get(*key).pass_streak == 0
    eng.process({key: sv(*key)}, T0 + 300)
    eng.discard_start_credit(T0 + 330, "evaluation failed")           # cycle failed before verdicts
    assert eng.store.get(*key).pass_streak == 0 and kinds(conn, "START") == []


# ---------------------------------------------------------------- duplicates
def test_no_duplicate_start_or_stop_and_outbox_dedupe(db, bots, cfg):
    conn, _ = db
    eng = engine(conn, bots, cfg)
    key = ("aria", "1HZ10V")
    t = start(eng, key)
    for i in range(5):                                                 # still RUN: no second START
        assert eng.process({key: sv(*key)}, t + 60 * i) == []
    t += 300
    assert [x.kind for x in eng.process({key: sv(*key, status=AVOID, critical_kind="risk")}, t)] == ["STOP"]
    for i in range(1, 5):                                              # still failing: no second STOP
        assert eng.process({key: sv(*key, status=AVOID, critical_kind="risk")}, t + 60 * i) == []
    assert [r["kind"] for r in kinds(conn)] == ["START", "STOP"]
    keys = [r["dedupe_key"] for r in kinds(conn)]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------- Telegram texts
@pytest.fixture
def real_verdicts(db, bots, rng):
    good = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=favourable_payout, bankroll=100_000).run(["aria"])
    return good["aria"]


def test_start_message_shows_every_check(bots, real_verdicts):
    v = real_verdicts
    assert v.status == RUN
    text = MessageFormatter(bots, "Asia/Colombo").start(v, T0)
    for part in ("🟢 START SIGNAL", "Bot: Aria", f"Market: {v.symbol_name}", f"Symbol: {v.symbol}",
                 "Contracts (from Aria.xml):", "• base: DIGITUNDER, prediction 9, 1 tick",
                 "• recovery: DIGITUNDER, prediction 5, 1 tick", "Wins if the settlement tick's last digit is 0-8",
                 "Entry condition (none: it purchases on every tick): PASS",
                 "Statistics (this bot's own entries, last 2,000 ticks):", "(99% CI",
                 "Quoted payout: 1.17 for stake 1.00 (net win +0.17, net loss -1.00, break-even win rate 85.47%)",
                 "Expected value per trade: +", "— POSITIVE",
                 "Checks: Entry condition PASS ✅ · Contract statistics PASS ✅ · Payout / expected value POSITIVE ✅ · "
                 "Risk SAFE ✅",
                 "Risk: SAFE", "• Next trade: DIGITUNDER 9 1t at stake 1.00 (quoted payout 1.17)",
                 "• Next stake after a loss: 2.10 (DIGITUNDER 5 1t); loss so far -1.00",
                 "• Bankroll needed for that streak: 521.14 (configured: 100,000.00)",
                 "Reason for START: every required check (entry, statistics, economics, risk) passed on 3 consecutive",
                 "This is not a prediction that the bot will win"):
        assert part in text, part
    assert len(text) <= 4000
    assert not re.search(r"\bwill win\b", text.replace("not a prediction that the bot will win", ""))


def test_stop_message_states_reason_failed_checks_payout_risk_and_cooldown(db, bots, rng):
    s = Setup(db, bots, digits_with(ARIA_GOOD, rng, 4), quotes=deriv_like_payout, bankroll=100_000)
    vs = s.run(["predator", "delta"])
    fmt = MessageFormatter(bots, "Asia/Colombo", cooldown_minutes=30)
    p = vs["predator"]
    assert (p.status, p.critical_kind, p.risk_status) == (AVOID, "risk", R.UNSAFE)
    text = fmt.stop(p, T0, critical=True)
    for part in ("🔴 STOP SIGNAL (critical)", "Bot: Predator", "Reason for STOP: risk UNSAFE",
                 "Checks not passing:", "• Risk: UNSAFE", "Risk issue: UNSAFE", "payout",
                 "Cooldown: applies — no new START for Predator on R_50 before", "30 min",
                 "never delay a STOP", "ACTION: Stop running Predator on R_50."):
        assert part in text, part
    d = vs["delta"]
    assert d.status == WAIT and d.economics_status in (E.NEGATIVE, E.INCONCLUSIVE, E.NOT_CHECKED)
    text = fmt.stop(d, T0)
    assert "🔴 STOP SIGNAL\n" in text and "Payout / expected value:" in text and "Payout issue:" in text


def test_unmonitored_stop_and_no_cooldown_configured(bots):
    fmt = MessageFormatter(bots, "Asia/Colombo", cooldown_minutes=0)
    text = fmt.stop_unmonitored("aria", "R_10", "market no longer listed", T0)
    assert "Signal status: STOP — market no longer listed" in text and "Cooldown: none configured" in text


def test_dry_run_never_sends(db, bots, cfg):
    conn, _ = db
    eng = engine(conn, bots, cfg)
    start(eng)
    sent = []
    sender = TelegramSender(conn, "123:abc", "42", dry_run=True, transport=sent.append)
    import asyncio
    asyncio.run(sender.deliver_pending(T0 + 400))
    assert sent == [] and conn.execute("SELECT status FROM notifications").fetchone()[0] == "skipped"

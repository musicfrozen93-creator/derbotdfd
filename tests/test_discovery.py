"""Market coverage: every allowed, compatible market is monitored for every bot, markets are added
and removed dynamically, and a failed contracts_for call never drops a market (mocked Deriv API).

Only Continuous Indices (API submarket ``random_index``) are allowed, plus the 1-second indices the
bot XML files use (1HZ10V, 1HZ50V). The fake API below labels every symbol with the category the
live Deriv API returned on 2026-11-17."""
import asyncio
import copy

import pytest

from app.config import load_settings
from app.deriv.client import DerivAPIError
from app.main import Application

from tests.test_compatibility import FULL

ALLOWED = [f"R_{n}" for n in (10, 25, 50, 75, 100)] + ["1HZ10V", "1HZ50V"]      # 7 markets
OTHER_1S = [f"1HZ{n}V" for n in (15, 25, 30, 75, 90, 100)]                           # Continuous, 1 s
JUMP = [f"JD{n}" for n in (10, 25, 50, 75, 100)]
EXCLUDED_WITH_DIGITS = OTHER_1S + JUMP + ["RDBEAR", "RDBULL"]   # would be compatible if allowed
NO_DIGITS = ["CRASH500", "BOOM500", "RB100", "stpRNG", "WLDEUR", "WLDXAU"]
DIGIT_SYMBOLS = ALLOWED + EXCLUDED_WITH_DIGITS                    # 20 digit-capable symbols
FOREX = ["frxEURUSD"]

# (market, submarket) exactly as the live active_symbols reply labels them
CATEGORY = {**{s: ("synthetic_index", "random_index") for s in ALLOWED + OTHER_1S},
            **{s: ("synthetic_index", "jump_index") for s in JUMP},
            "RDBEAR": ("synthetic_index", "random_daily"), "RDBULL": ("synthetic_index", "random_daily"),
            "CRASH500": ("synthetic_index", "crash_index"), "BOOM500": ("synthetic_index", "crash_index"),
            "RB100": ("synthetic_index", "range_index"), "stpRNG": ("synthetic_index", "step_index"),
            "WLDEUR": ("synthetic_index", "forex_basket"), "WLDXAU": ("synthetic_index", "commodity_basket"),
            "frxEURUSD": ("forex", "major_pairs"),
            "R_150": ("synthetic_index", "random_index"), "1HZ150V": ("synthetic_index", "random_index")}


def name_of(sym):
    if sym.startswith("1HZ"):
        return f"Volatility {sym[3:-1]} (1s) Index"
    if sym.startswith("R_"):
        return f"Volatility {sym[2:]} Index"
    return sym
NO_DIGIT_LISTING = {"contracts_for": {"available": [
    {"contract_type": "CALL", "expiry_type": "tick", "min_contract_duration": "1t",
     "max_contract_duration": "10t"}]}}


class FakeDeriv:
    def __init__(self, symbols, fail=()):
        self.symbols = list(symbols)
        self.fail = set(fail)
        self.connected = True
        self.subscribed = []
        self.forgotten = []
        self.contracts_asked = []

    async def wait_connected(self, timeout=None):
        return None

    async def subscribe_ticks(self, symbol):
        self.subscribed.append(symbol)
        return f"sub-{symbol}"

    async def forget(self, sub):
        self.forgotten.append(sub)

    async def request(self, payload, **kw):
        if "active_symbols" in payload:
            return {"active_symbols": [
                {"underlying_symbol": s, "underlying_symbol_name": name_of(s), "market": CATEGORY[s][0],
                 "submarket": CATEGORY[s][1], "pip_size": 0.01, "exchange_is_open": 1,
                 "is_trading_suspended": 0} for s in self.symbols]}
        if "contracts_for" in payload:
            sym = payload["contracts_for"]
            self.contracts_asked.append(sym)
            if sym in self.fail:
                raise DerivAPIError("RateLimit", "You have reached the rate limit for contracts_for.")
            return copy.deepcopy(NO_DIGIT_LISTING if sym in NO_DIGITS else FULL)
        raise AssertionError(payload)


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""
storage: {{db_path: "{(tmp_path / 'a.db').as_posix()}"}}
logging: {{dir: "{(tmp_path / 'logs').as_posix()}"}}
heartbeat: {{file: "{(tmp_path / 'hb.json').as_posix()}"}}
""", encoding="utf-8")
    monkeypatch.delenv("ANALYZER_CONFIG", raising=False)
    made = []

    def make(client):
        app = Application(load_settings(cfg, dry_run=True))
        app.client = app.collector.client = client
        made.append(app)
        return app
    yield make
    for a in made:
        try:
            a.conn.close()
        except Exception:
            pass


def test_production_config_has_no_market_cap_below_discovery():
    s = load_settings()                                  # the shipped config.yaml
    assert int(s.discovery.max_symbols) >= 40
    assert list(s.discovery.markets) == ["synthetic_index"]
    assert list(s.discovery.include_submarkets) == ["random_index"]        # Continuous Indices only
    assert list(s.discovery.one_second_symbols) == ["1HZ10V", "1HZ50V"]
    assert list(s.discovery.exclude_symbols) == []
    assert list(s.strategies.enabled) == ["all"]
    assert int(s.signal_rule.window_ticks) == 500 and str(s.signal_rule.mode) == "individual"


def test_every_allowed_market_is_monitored_for_all_ten_bots(app_factory):
    client = FakeDeriv(DIGIT_SYMBOLS + NO_DIGITS + FOREX)
    app = app_factory(client)
    asyncio.run(app.discover())
    assert sorted(app.markets) == sorted(ALLOWED)                # 7 of 27 listed
    assert sorted(client.subscribed) == sorted(ALLOWED)
    combos, missing = app.combos()
    assert len(app.bots) == 10 and len(combos) == 70 and missing == {}
    assert {b for b, _ in combos} == set(app.bot_map) and {s for _, s in combos} == set(ALLOWED)


def test_new_continuous_index_is_added_automatically(app_factory):
    client = FakeDeriv(DIGIT_SYMBOLS)
    app = app_factory(client)
    asyncio.run(app.discover())
    assert "R_150" not in app.markets
    client.symbols = DIGIT_SYMBOLS + ["R_150", "1HZ150V"]           # Deriv lists two new indices
    asyncio.run(app.discover())
    assert "R_150" in app.markets and "R_150" in client.subscribed  # new Continuous Index: picked up
    assert "1HZ150V" not in app.markets and "1HZ150V" not in client.contracts_asked   # 1 s: not allowed
    assert len(app.combos()[0]) == 80


def test_delisted_market_stops_its_active_signals_only(app_factory, bots):
    client = FakeDeriv(DIGIT_SYMBOLS)
    app = app_factory(client)
    asyncio.run(app.discover())
    for key in [("delta", "R_10"), ("delta", "R_100")]:          # pretend both are active
        st = app.store.get(*key)
        st.active = True
        app.store.save(st, 0)
    client.symbols = [s for s in DIGIT_SYMBOLS if s != "R_10"]   # Deriv delists R_10
    asyncio.run(app.discover())
    assert "R_10" not in app.markets and "sub-R_10" in client.forgotten
    combos, missing = app.combos()
    assert missing == {("delta", "R_10"): "market no longer listed by Deriv active_symbols"}
    from tests.test_signals import verdict
    tr = app.engine.process({("delta", "R_100"): verdict("delta", "R_100")}, 1_800_000_000, missing)
    assert [(t.kind, t.bot, t.symbol, t.critical) for t in tr] == [("STOP", "delta", "R_10", True)]
    assert app.store.get("delta", "R_100").active


def test_failed_contracts_for_keeps_the_market(app_factory):
    client = FakeDeriv(DIGIT_SYMBOLS)
    app = app_factory(client)
    asyncio.run(app.discover())
    client.fail = {"R_100", "1HZ10V"}                           # transient RateLimit on re-discovery
    asyncio.run(app.discover())
    assert "R_100" in app.markets and "1HZ10V" in app.markets
    assert len(app.combos()[0]) == 70


def test_failed_contracts_for_after_restart_uses_stored_listing(app_factory):
    client = FakeDeriv(DIGIT_SYMBOLS)
    asyncio.run(app_factory(client).discover())
    client2 = FakeDeriv(DIGIT_SYMBOLS, fail={"R_100"})           # new process, first call fails
    app2 = app_factory(client2)
    asyncio.run(app2.discover())
    assert "R_100" in app2.markets and len(app2.combos()[0]) == 70


def test_contract_withdrawn_marks_combination_incompatible(app_factory):
    client = FakeDeriv(DIGIT_SYMBOLS)
    app = app_factory(client)
    asyncio.run(app.discover())
    st = app.store.get("pulse2", "R_50")
    st.active = True
    app.store.save(st, 0)
    orig = client.request

    async def no_even(payload, **kw):                           # R_50 stops offering DIGITEVEN
        msg = await orig(payload, **kw)
        if payload.get("contracts_for") == "R_50":
            msg["contracts_for"]["available"] = [c for c in msg["contracts_for"]["available"]
                                                 if c["contract_type"] != "DIGITEVEN"]
        return msg
    client.request = no_even
    asyncio.run(app.discover())
    assert not app.matrix[("pulse2", "R_50")].compatible and app.matrix[("aria", "R_50")].compatible
    combos, _ = app.combos()
    assert ("pulse2", "R_50") in combos                         # still evaluated -> contract STOP


def test_app_restart_check_uses_stored_last_alive_time(app_factory):
    from app.storage import repositories as repo
    app = app_factory(FakeDeriv(DIGIT_SYMBOLS))
    t = 1_800_000_000
    assert app.startup_check(now=t) == []                       # first ever start
    st = app.store.get("delta", "R_10")
    st.active, st.pass_streak = True, 2
    app.store.save(st, t)
    app.mark_alive(t + 600)
    app2 = app_factory(FakeDeriv(DIGIT_SYMBOLS))                  # restart 60 s later: kept
    assert app2.startup_check(now=t + 660) == []
    assert app2.store.get("delta", "R_10").active and app2.store.get("delta", "R_10").pass_streak == 0
    assert repo.meta_get(app2.conn, "last_alive") == str(t + 660)
    app3 = app_factory(FakeDeriv(DIGIT_SYMBOLS))                  # server down for 2 hours
    tr = app3.startup_check(now=t + 660 + 7200)
    assert [(x.kind, x.bot, x.symbol) for x in tr] == [("STOP", "delta", "R_10")]
    row = app3.conn.execute("SELECT kind, text FROM notifications").fetchall()
    assert [r[0] for r in row] == ["STOP"] and "offline for 120.0 min" in row[0][1]

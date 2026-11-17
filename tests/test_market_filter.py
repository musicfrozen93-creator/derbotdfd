"""Market filter: Continuous Indices only (API submarket ``random_index``), plus the 1-second
indices the bot XML files trade (1HZ10V, 1HZ50V). Everything else is rejected before
contracts_for, so it can never reach tick subscriptions or evaluation."""
import asyncio
import copy
import json
from pathlib import Path

import pytest

from app.config import load_settings
from app.deriv.contracts import parse_contracts_for
from app.deriv.symbols import MarketInfo, classify_market, parse_active_symbols, select_candidates
from app.strategies.compatibility import check

from tests.test_compatibility import FULL, _avail
from tests.test_discovery import ALLOWED, DIGIT_SYMBOLS, FOREX, NO_DIGITS, FakeDeriv, app_factory  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
LIVE = json.loads((ROOT / "tests" / "data" / "active_symbols_live_2026-11-17.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rules():
    d = load_settings(ROOT / "config.yaml").discovery          # the shipped production config
    return dict(include_markets=list(d.markets), include_submarkets=list(d.include_submarkets),
                exclude_symbols=list(d.exclude_symbols), one_second_symbols=list(d.one_second_symbols))


def mk(symbol, submarket, market="synthetic_index", name=None):
    return MarketInfo(symbol, name or symbol, market, submarket, 2, True, False)


# --------------------------------------------------------------- the live listing
def test_live_listing_allows_exactly_the_continuous_indices_and_two_1s_indices(rules):
    listed = parse_active_symbols(LIVE)
    assert len(listed) == 95
    allowed = sorted(m.symbol for m in select_candidates(listed, **rules))
    assert allowed == ["1HZ10V", "1HZ50V", "R_10", "R_100", "R_25", "R_50", "R_75"]


def test_live_listing_rejections_carry_the_api_category(rules):
    by_reason = {}
    for m in parse_active_symbols(LIVE):
        ok, why = classify_market(m, **rules)
        if not ok:
            by_reason.setdefault(m.submarket if m.market == "synthetic_index" else m.market, set()).add((m.symbol, why))
    syms = {k: sorted(s for s, _ in v) for k, v in by_reason.items()}
    assert syms["jump_index"] == ["JD10", "JD100", "JD25", "JD50", "JD75"]
    assert syms["random_daily"] == ["RDBEAR", "RDBULL"]
    assert syms["range_index"] == ["RB100", "RB200"]
    assert syms["step_index"] == ["stpRNG", "stpRNG2", "stpRNG3", "stpRNG4", "stpRNG5"]
    assert syms["forex_basket"] == ["WLDAUD", "WLDEUR", "WLDGBP", "WLDUSD"]
    assert syms["commodity_basket"] == ["WLDXAU"]
    assert len(syms["crash_index"]) == 20 and all(s.startswith(("CRASH", "BOOM")) for s in syms["crash_index"])
    assert syms["random_index"] == ["1HZ100V", "1HZ15V", "1HZ25V", "1HZ30V", "1HZ75V", "1HZ90V"]
    assert all("not in discovery.one_second_symbols" in why for _, why in by_reason["random_index"])
    assert all("Jump Indices" in why for _, why in by_reason["jump_index"])
    for other in ("forex", "commodities", "indices", "cryptocurrency"):
        assert syms[other] and all("discovery.markets" in why for _, why in by_reason[other])


# ------------------------------------------------------------------ unit cases
@pytest.mark.parametrize("sym,name", [("R_10", "Volatility 10 Index"), ("R_25", "Volatility 25 Index"),
                                      ("R_50", "Volatility 50 Index"), ("R_75", "Volatility 75 Index"),
                                      ("R_100", "Volatility 100 Index"),
                                      ("1HZ10V", "Volatility 10 (1s) Index"),
                                      ("1HZ50V", "Volatility 50 (1s) Index")])
def test_accepted(rules, sym, name):
    assert classify_market(mk(sym, "random_index", name=name), **rules)[0]


@pytest.mark.parametrize("sym,submarket", [
    ("JD10", "jump_index"), ("JD25", "jump_index"), ("JD50", "jump_index"), ("JD75", "jump_index"),
    ("JD100", "jump_index"),
    ("CRASH500", "crash_index"), ("BOOM1000", "crash_index"),
    ("RB100", "range_index"), ("RB200", "range_index"),
    ("RDBEAR", "random_daily"), ("RDBULL", "random_daily"),
    ("WLDEUR", "forex_basket"), ("WLDUSD", "forex_basket"),
    ("WLDXAU", "commodity_basket"),
    ("stpRNG", "step_index"), ("stpRNG5", "step_index"),
    ("1HZ100V", "random_index"), ("1HZ15V", "random_index"), ("1HZ25V", "random_index"),
])
def test_rejected(rules, sym, submarket):
    ok, why = classify_market(mk(sym, submarket), **rules)
    assert not ok and why


def test_other_market_types_rejected(rules):
    for m in (mk("frxEURUSD", "major_pairs", "forex"), mk("frxXAUUSD", "metals", "commodities"),
              mk("OTC_NDX", "americas_OTC", "indices"), mk("cryBTCUSD", "non_stable_coin", "cryptocurrency")):
        assert not classify_market(m, **rules)[0]


def test_category_comes_from_the_api_not_the_symbol_name(rules):
    # A familiar name in a different API category is rejected ...
    assert not classify_market(mk("R_10", "jump_index"), **rules)[0]
    assert not classify_market(mk("1HZ10V", "crash_index"), **rules)[0]
    # ... and a new, unfamiliar name in the Continuous Indices category is accepted (dynamic discovery).
    assert classify_market(mk("R_150", "random_index", name="Volatility 150 Index"), **rules)[0]
    # A new 1-second index is not allowed automatically (identified by its "(1s)" name too).
    assert not classify_market(mk("VOL150S", "random_index", name="Volatility 150 (1s) Index"), **rules)[0]


def test_exclude_symbols_still_applies(rules):
    r = dict(rules, exclude_symbols=["R_75"])
    assert not classify_market(mk("R_75", "random_index"), **r)[0]


# ------------------------------------------------- nothing excluded gets further
def test_excluded_symbols_never_reach_contracts_ticks_or_evaluation(app_factory):
    client = FakeDeriv(DIGIT_SYMBOLS + NO_DIGITS + FOREX)
    app = app_factory(client)
    seen_cache, seen_combos = [], []
    orig_get, orig_eval = app.cache.get, app.analyzer.evaluate

    def spy_get(sym):
        seen_cache.append(sym)
        return orig_get(sym)

    def spy_eval(combos, *a, **kw):
        seen_combos.extend(combos)
        return orig_eval(combos, *a, **kw)
    app.cache.get, app.analyzer.evaluate = spy_get, spy_eval

    asyncio.run(app.discover())
    result = asyncio.run(app.evaluate_once())
    allowed = set(ALLOWED)
    assert set(client.contracts_asked) == allowed                       # contracts_for
    assert set(client.subscribed) == allowed                            # tick subscriptions
    assert {s for _, s in seen_combos} == allowed and len(seen_combos) == 70   # evaluation input
    assert set(seen_cache) == allowed                                   # tick series loaded
    assert {s for _, s in result.verdicts} == allowed                   # verdicts / START-STOP engine
    assert {s for _, s in app.matrix} == allowed                        # compatibility matrix
    rows = app.conn.execute("SELECT DISTINCT symbol FROM compatibility").fetchall()
    assert {r[0] for r in rows} == allowed
    jump = [syms for why, syms in app.excluded_markets.items() if why.startswith("Jump Indices")]
    assert jump and sorted(jump[0]) == ["JD10", "JD100", "JD25", "JD50", "JD75"]


def test_market_stored_before_the_filter_is_deactivated_and_its_signal_stopped(app_factory):
    """Upgrade path: a database from the old version may hold JD10 as an active market and an
    active signal on it. After the first discovery the market is inactive and the signal gets
    the existing critical STOP for combinations that are no longer monitored."""
    from app.storage import repositories as repo
    app = app_factory(FakeDeriv(DIGIT_SYMBOLS))
    repo.upsert_markets(app.conn, [mk("JD10", "jump_index")])          # left over from the old version
    st = app.store.get("delta", "JD10")
    st.active = True
    app.store.save(st, 0)
    asyncio.run(app.discover())
    assert "JD10" not in repo.active_market_symbols(app.conn)
    combos, missing = app.combos()
    assert ("delta", "JD10") not in combos
    assert missing == {("delta", "JD10"): "market no longer compatible / monitored"}
    tr = app.engine.process({}, 1_800_000_000, missing)
    assert [(t.kind, t.bot, t.symbol, t.critical) for t in tr] == [("STOP", "delta", "JD10", True)]


# ------------------------------------- each bot only on markets that fit its contracts
def test_bots_only_run_where_their_own_contracts_are_offered(bots):
    m = mk("R_25", "random_index")
    short = copy.deepcopy(FULL)                         # digits offered only up to 2 ticks
    short["contracts_for"]["available"] = [
        _avail("DIGITOVER", hi="2t", rng=list(range(9))), _avail("DIGITUNDER", hi="2t", rng=list(range(1, 10))),
        _avail("DIGITEVEN", hi="2t", barriers=0), _avail("DIGITODD", hi="2t", barriers=0)]
    sc = parse_contracts_for("R_25", short)
    ok = {k: check(b, m, sc).compatible for k, b in bots.items()}
    assert ok == {"aria": True, "aria_v2": True, "crysis": False, "delta": True, "navix": True,
                  "predator": True, "predator_v2": True, "pulse2": True, "voltrix": False, "zony_ai": True}
    no_even = copy.deepcopy(FULL)
    no_even["contracts_for"]["available"] = [c for c in no_even["contracts_for"]["available"]
                                             if c["contract_type"] != "DIGITEVEN"]
    sc2 = parse_contracts_for("R_25", no_even)
    assert [k for k, b in bots.items() if not check(b, m, sc2).compatible] == ["pulse2"]
    no_over8 = copy.deepcopy(FULL)                      # OVER barrier 8 not offered -> only Zony AI
    no_over8["contracts_for"]["available"][0]["last_digit_range"] = list(range(8))
    sc3 = parse_contracts_for("R_25", no_over8)
    assert [k for k, b in bots.items() if not check(b, m, sc3).compatible] == ["zony_ai"]

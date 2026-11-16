"""3. Contract compatibility (mocked contracts_for responses)."""
import copy

from app.deriv.contracts import parse_contracts_for, parse_tick_duration
from app.deriv.symbols import MarketInfo, parse_active_symbols, select_candidates
from app.strategies.compatibility import build_matrix, check, format_matrix


def _avail(ctype, lo="1t", hi="10t", rng=None, barriers=1):
    d = {"contract_type": ctype, "contract_category": "digits", "expiry_type": "tick",
         "min_contract_duration": lo, "max_contract_duration": hi, "barriers": barriers}
    if rng is not None:
        d["last_digit_range"] = rng
    return d


FULL = {"contracts_for": {"available": [
    _avail("DIGITOVER", rng=list(range(9))), _avail("DIGITUNDER", rng=list(range(1, 10))),
    _avail("DIGITEVEN", barriers=0), _avail("DIGITODD", barriers=0),
    {"contract_type": "CALL", "expiry_type": "intraday", "min_contract_duration": "15m",
     "max_contract_duration": "1d"},
]}}

MARKET = MarketInfo("1HZ10V", "Volatility 10 (1s) Index", "synthetic_index", "random_index", 2, True, False)


def test_parse_durations():
    assert parse_tick_duration("1t") == 1
    assert parse_tick_duration("10t") == 10
    assert parse_tick_duration("15m") is None


def test_all_bots_compatible_on_full_listing(bots):
    sc = parse_contracts_for("1HZ10V", FULL)
    assert set(sc.contracts) == {"DIGITOVER", "DIGITUNDER", "DIGITEVEN", "DIGITODD"}
    for bot in bots.values():
        res = check(bot, MARKET, sc)
        assert res.compatible, (bot.key, res.reasons)


def test_missing_contract_type(bots):
    msg = copy.deepcopy(FULL)
    msg["contracts_for"]["available"] = [a for a in msg["contracts_for"]["available"]
                                         if a["contract_type"] != "DIGITUNDER"]
    sc = parse_contracts_for("X", msg)
    assert not check(bots["aria"], MARKET, sc).compatible
    assert not check(bots["navix"], MARKET, sc).compatible       # base leg is UNDER 8
    assert check(bots["delta"], MARKET, sc).compatible
    assert not check(bots["pulse2"], MARKET, parse_contracts_for("X", {"contracts_for": {"available": [
        _avail("DIGITOVER", rng=list(range(9)))]}})).compatible


def test_duration_out_of_range(bots):
    msg = {"contracts_for": {"available": [_avail("DIGITOVER", hi="3t", rng=list(range(9)))]}}
    sc = parse_contracts_for("X", msg)
    assert check(bots["voltrix"], MARKET, sc).compatible            # 3 ticks
    res = check(bots["crysis"], MARKET, sc)                          # 5 ticks
    assert not res.compatible and "duration" in res.reason_text


def test_barrier_not_offered(bots):
    msg = {"contracts_for": {"available": [_avail("DIGITOVER", rng=[0, 1, 2, 3, 4, 5, 6, 7])]}}
    sc = parse_contracts_for("X", msg)
    res = check(bots["zony_ai"], MARKET, sc)       # needs OVER 8 on step 6
    assert not res.compatible and "step6" in res.reason_text
    assert check(bots["predator_v2"], MARKET, sc).compatible


def test_closed_or_suspended_market(bots):
    sc = parse_contracts_for("X", FULL)
    closed = MarketInfo("X", "X", "synthetic_index", "random_index", 2, False, False)
    susp = MarketInfo("X", "X", "synthetic_index", "random_index", 2, True, True)
    assert not check(bots["aria"], closed, sc).compatible
    assert not check(bots["aria"], susp, sc).compatible
    assert not check(bots["aria"], MARKET, None).compatible


def test_no_digit_contracts_symbol(bots):
    boom = {"contracts_for": {"available": [{"contract_type": "MULTUP", "expiry_type": "no_expiry"}]}}
    sc = parse_contracts_for("BOOM1000", boom)
    for bot in bots.values():
        assert not check(bot, MARKET, sc).compatible


def test_matrix_and_discovery_selection(bots):
    msg = {"active_symbols": [
        {"underlying_symbol": "1HZ10V", "underlying_symbol_name": "Volatility 10 (1s) Index", "market": "synthetic_index",
         "submarket": "random_index", "pip_size": 0.01, "exchange_is_open": 1, "is_trading_suspended": 0},
        {"underlying_symbol": "BOOM1000", "underlying_symbol_name": "Boom 1000 Index", "market": "synthetic_index",
         "submarket": "crash_index", "pip_size": 0.001, "exchange_is_open": 1, "is_trading_suspended": 0},
        {"underlying_symbol": "frxEURUSD", "underlying_symbol_name": "EUR/USD", "market": "forex",
         "submarket": "major_pairs", "pip_size": 0.00001, "exchange_is_open": 1, "is_trading_suspended": 0},
    ]}
    markets = parse_active_symbols(msg)
    assert [m.pip_decimals for m in markets] == [2, 3, 5]
    cands = select_candidates(markets, include_markets=["synthetic_index"], include_submarkets=[], exclude_symbols=[])
    assert [m.symbol for m in cands] == ["1HZ10V", "BOOM1000"]
    contracts = {"1HZ10V": parse_contracts_for("1HZ10V", FULL),
                 "BOOM1000": parse_contracts_for("BOOM1000", {"contracts_for": {"available": []}})}
    matrix = build_matrix(list(bots.values()), {m.symbol: m for m in cands}, contracts)
    assert all(matrix[(b, "1HZ10V")].compatible for b in bots)
    assert not any(matrix[(b, "BOOM1000")].compatible for b in bots)
    table = format_matrix(list(bots.values()), matrix)
    assert "| Aria | 1HZ10V | DIGITUNDER 9, DIGITUNDER 5 | 1t | Yes |" in table

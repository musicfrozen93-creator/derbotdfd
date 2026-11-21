"""Recovery and risk engine: each bot's streak against platform refusals, minimum stake and bankroll.

Quotes are produced by a fake Deriv (the payout formula validated on 168 live quotes, with the
public $1,000 maximum payout), standing in for the live endpoint.
"""
import pytest

from app.economics.ev import model_payout
from app.economics.quotes import QuoteBook, QuoteKey, parse_error, parse_ok
from app.risk.engine import BLOCKED, SAFE, UNKNOWN, UNSAFE, RiskConfig, evaluate_risk, ladder_quote_needs
from app.strategy_model.bots import BOTS, win_digits

NOW = 1_800_000_000.0
SYM = "R_100"


def fake_quote(key, cap=1000.0, min_stake=0.35):
    p = len(win_digits(key.contract_type, key.barrier)) / 10
    if key.stake < min_stake:
        return parse_error(key, "ContractBuyValidationError", f"Please enter a stake amount that's at least {min_stake}.",
                           NOW)
    payout = model_payout(key.stake, p)
    if payout > cap:
        return parse_error(key, "ContractBuyValidationError",
                           f"Minimum stake of {min_stake} and maximum payout of {cap:.2f}. Current payout is {payout:.2f}.",
                           NOW)
    return parse_ok(key, {"proposal": {"payout": payout, "ask_price": key.stake}}, NOW)


def filled_book(db, model, base=None, cap=1000.0):
    conn, _ = db
    book = QuoteBook(conn)
    for _ in range(80):                     # walk the ladders as the quote service would
        needs = ladder_quote_needs(model, SYM, book, base)
        todo = [k for _, k in needs if book.get(k) is None]
        if not todo:
            break
        for k in todo:
            book.put(fake_quote(k, cap))
    return book


@pytest.mark.parametrize("key,status,losses,worst", [
    ("predator", UNSAFE, 6, 364.00),
    ("predator_v2", UNSAFE, 8, 510.00),
    ("navix", UNSAFE, 6, 440.95),
    ("pulse2", UNSAFE, 10, 935.80),
    ("crysis", UNSAFE, 6, 569.05),          # first streak of a run (Initial Y = 2)
])
def test_martingales_that_break_at_the_payout_cap(db, key, status, losses, worst):
    book = filled_book(db, BOTS[key])
    r = evaluate_risk(BOTS[key], SYM, book, RiskConfig(bankroll=100000), None, NOW)
    assert r.status == status
    breaking = [c for c in r.ladders if c.status == UNSAFE]
    assert breaking and breaking[0].stop_kind == "payout_cap"
    assert breaking[0].losses_before_stop == losses and breaking[0].worst_loss == pytest.approx(worst)
    assert "refused" in r.reason and "unrecoverable" in r.can_continue_after_rejection


# required bankroll = the larger of the fresh-run streak and the near-take-profit streak (minus that profit):
# Aria near TP: 721.13 - 199.99 = 521.14 > 342.92; the others are largest from a fresh run.
@pytest.mark.parametrize("key,need", [("aria", 521.14), ("aria_v2", 77.06), ("delta", 81.13),
                                      ("voltrix", 516.01), ("zony_ai", 6.0)])
def test_bots_stopped_by_their_own_rules_are_safe_with_enough_bankroll(db, key, need):
    book = filled_book(db, BOTS[key])
    r = evaluate_risk(BOTS[key], SYM, book, RiskConfig(bankroll=10000), None, NOW)
    assert r.status == SAFE, r.reason
    assert r.required_bankroll == pytest.approx(need, abs=0.01)
    kinds = {c.stop_kind for c in r.ladders}
    assert kinds <= {"xml_stop_loss", "list_end", "take_profit"}


def test_bankroll_too_small_is_unsafe_and_unknown_without_bankroll(db):
    book = filled_book(db, BOTS["aria"])
    r = evaluate_risk(BOTS["aria"], SYM, book, RiskConfig(bankroll=400), None, NOW)
    assert r.status == UNSAFE and any(c.stop_kind == "bankroll" for c in r.ladders)
    r2 = evaluate_risk(BOTS["aria"], SYM, book, RiskConfig(bankroll=None), None, NOW)
    assert r2.status == UNKNOWN and "bankroll not configured" in r2.reason
    assert r2.required_bankroll == pytest.approx(521.14, abs=0.01)


def test_first_trade_refused_is_blocked(db):
    m = BOTS["delta"]
    book = filled_book(db, m, base=0.2)
    r = evaluate_risk(m, SYM, book, RiskConfig(bankroll=1000), 0.2, NOW)
    assert r.status == BLOCKED and r.ladders[0].stop_kind == "min_stake"
    small = filled_book(db, BOTS["predator"])
    r = evaluate_risk(BOTS["predator"], SYM, small, RiskConfig(bankroll=0.5), None, NOW)
    assert r.status == BLOCKED and r.ladders[0].stop_kind == "bankroll"


def test_missing_quote_is_unknown(db):
    conn, _ = db
    r = evaluate_risk(BOTS["aria"], SYM, QuoteBook(conn), RiskConfig(bankroll=1000), None, NOW)
    assert r.status == UNKNOWN and r.ladders[0].stop_kind == "unknown_quote"


def test_account_max_payout_override(db):
    m = BOTS["predator"]
    book = filled_book(db, m)                                # public quotes refuse 729 (payout 1,191)
    r = evaluate_risk(m, SYM, book, RiskConfig(bankroll=10**9, max_payout_override=2000), None, NOW)
    # with a verified 2,000 account limit, 729 is accepted; the next stake (2,187) has no quote yet -> UNKNOWN
    assert r.ladders[0].steps[6].executable is True
    assert r.status in (UNKNOWN, UNSAFE)


def test_base_stake_changes_the_breaking_point(db):
    m = BOTS["predator"]
    book = filled_book(db, m, base=10)
    r = evaluate_risk(m, SYM, book, RiskConfig(bankroll=100000), 10, NOW)
    assert r.status == UNSAFE and r.ladders[0].losses_before_stop == 4 and r.worst_loss == pytest.approx(400)


def test_quote_needs_stop_at_first_refusal(db):
    m = BOTS["predator"]
    book = filled_book(db, m)
    stakes = [k.stake for _, k in ladder_quote_needs(m, SYM, book, None)]
    assert stakes == [1, 3, 9, 27, 81, 243, 729]            # nothing beyond the refused 729 is requested

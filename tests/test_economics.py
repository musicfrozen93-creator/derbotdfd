"""Quotes, platform limits and expected value."""
import asyncio

import pytest

from app.deriv.client import DerivAPIError
from app.economics import ev as E
from app.economics.quotes import QuoteBook, QuoteKey, QuoteNeed, QuoteService, parse_error, parse_ok

NOW = 1_800_000_000.0
K = QuoteKey("R_100", "DIGITOVER", 3, 1, 1.0)


# ------------------------------------------------------------------ parsing Deriv answers
def test_parse_successful_quote():
    q = parse_ok(K, {"proposal": {"payout": 1.63, "ask_price": 1}}, NOW)
    assert q.ok and q.payout == 1.63 and not q.refused and not q.transient


def test_parse_max_payout_refusal():
    q = parse_error(K, "ContractBuyValidationError",
                    "Minimum stake of 0.35 and maximum payout of 1000.00. Current payout is 3265.10.", NOW)
    assert q.refused and q.max_payout == 1000.0 and q.min_stake == 0.35 and q.reported_payout == 3265.10
    assert "above the maximum payout" in q.refused_reason


def test_parse_min_stake_refusal():
    q = parse_error(QuoteKey("R_100", "DIGITOVER", 3, 1, 0.34), "ContractBuyValidationError",
                    "Please enter a stake amount that's at least 0.35.", NOW)
    assert q.refused and q.min_stake == 0.35 and "below the minimum stake" in q.refused_reason


def test_rate_limit_is_transient_not_a_refusal():
    q = parse_error(K, "RateLimit", "You have reached the rate limit for proposal.", NOW)
    assert q.transient and not q.refused


def test_quote_book_persists_quotes_and_limits(db):
    conn, _ = db
    book = QuoteBook(conn)
    book.put(parse_ok(K, {"proposal": {"payout": 1.63, "ask_price": 1}}, NOW))
    big = QuoteKey("R_100", "DIGITOVER", 3, 1, 729.0)
    book.put(parse_error(big, "ContractBuyValidationError",
                         "Minimum stake of 0.35 and maximum payout of 1000.00. Current payout is 1191.18.", NOW))
    again = QuoteBook(conn)
    again.load()
    assert again.get(K).payout == 1.63 and again.get(big).refused
    lim = again.limits[("R_100", "DIGITOVER")]
    assert lim.max_payout == 1000.0 and lim.min_stake == 0.35 and "unauthenticated" in lim.context


# ------------------------------------------------------------------ expected value
def test_ev_definitions_match_deriv_payout_structure():
    # OVER 3, stake 1, payout 1.63, fair p 0.6: net win 0.63, net loss 1, EV = 0.6*0.63 - 0.4*1 = -0.022
    assert E.ev(0.6, 1.63, 1.0) == pytest.approx(-0.022)
    q = parse_ok(K, {"proposal": {"payout": 1.63, "ask_price": 1}}, NOW)
    e = E.leg_economics("main", "DIGITOVER 3 1t", 1.0, q, 0.6, wins=6000, n=10000, confidence=0.99,
                        min_outcomes=100, max_age_s=86400, now=NOW + 10)
    assert e.net_win == pytest.approx(0.63) and e.net_loss == 1.0
    assert e.break_even == pytest.approx(1 / 1.63)
    assert e.ev_fair == pytest.approx(-0.022)
    assert e.status == E.NEGATIVE                      # 60 % +- 1.1 % never reaches 61.35 %


def test_positive_only_when_lower_bound_beats_break_even():
    q = parse_ok(K, {"proposal": {"payout": 1.63, "ask_price": 1}}, NOW)
    pos = E.leg_economics("main", "c", 1.0, q, 0.6, wins=6500, n=10000, confidence=0.99, min_outcomes=100,
                          max_age_s=86400, now=NOW)
    assert pos.status == E.POSITIVE and pos.ev_low > 0
    mid = E.leg_economics("main", "c", 1.0, q, 0.6, wins=62, n=100, confidence=0.99, min_outcomes=100,
                          max_age_s=86400, now=NOW)
    assert mid.status == E.INCONCLUSIVE                # point estimate 62 % > 61.35 % is NOT enough


def test_unknown_without_quote_stale_quote_or_refusal():
    kw = dict(confidence=0.99, min_outcomes=100, max_age_s=3600, now=NOW + 7200)
    assert E.leg_economics("m", "c", 1.0, None, 0.6, 6000, 10000, **kw).status == E.UNKNOWN
    stale = parse_ok(K, {"proposal": {"payout": 1.63, "ask_price": 1}}, NOW)
    assert E.leg_economics("m", "c", 1.0, stale, 0.6, 6000, 10000, **kw).status == E.UNKNOWN
    refused = parse_error(K, "ContractBuyValidationError",
                          "Minimum stake of 0.35 and maximum payout of 1000.00. Current payout is 3265.10.", NOW + 7000)
    e = E.leg_economics("m", "c", 1.0, refused, 0.6, 6000, 10000, **kw)
    assert e.status == E.UNKNOWN and "refuses" in e.detail


def test_not_checked_with_too_few_outcomes():
    q = parse_ok(K, {"proposal": {"payout": 1.63, "ask_price": 1}}, NOW)
    e = E.leg_economics("m", "c", 1.0, q, 0.6, 30, 40, confidence=0.99, min_outcomes=100, max_age_s=86400, now=NOW)
    assert e.status == E.NOT_CHECKED and e.ev_fair is not None


def test_combine_is_conservative():
    def leg(s):
        return E.LegEconomics("x", "c", 1.0, s, "")
    assert E.combine([leg(E.POSITIVE), leg(E.POSITIVE)])[0] == E.POSITIVE
    assert E.combine([leg(E.POSITIVE), leg(E.UNKNOWN)])[0] == E.UNKNOWN
    assert E.combine([leg(E.POSITIVE), leg(E.NEGATIVE), leg(E.UNKNOWN)])[0] == E.NEGATIVE
    assert E.combine([leg(E.POSITIVE), leg(E.INCONCLUSIVE)])[0] == E.INCONCLUSIVE


def test_payout_formula_is_only_a_cross_check():
    good = [parse_ok(QuoteKey("R_100", "DIGITOVER", 3, 1, s), {"proposal": {"payout": E.model_payout(s, 0.6),
                                                                            "ask_price": s}}, NOW)
            for s in (0.35, 1.0, 10.0, 243.0)]
    assert E.validate_model(good, lambda k: 0.6)["status"] == "VALIDATED"
    bad = good + [parse_ok(QuoteKey("R_100", "DIGITOVER", 3, 1, 5.0), {"proposal": {"payout": 9.99,
                                                                                   "ask_price": 5}}, NOW)]
    v = E.validate_model(bad, lambda k: 0.6)
    assert v["status"] == "MISMATCH" and v["mismatch_count"] == 1
    assert E.validate_model([], lambda k: 0.6)["status"] == "UNVALIDATED"
    # spot values that Deriv quoted live on 2026-11-20
    assert E.model_payout(1.0, 0.9) == 1.09 and E.model_payout(0.35, 0.5) == 0.66 and E.model_payout(729, 0.6) > 1000


# ------------------------------------------------------------------ quote service
class FakeQuoteClient:
    def __init__(self, cap=1000.0):
        self.cap = cap
        self.calls = []
        self.connected = True

    async def wait_connected(self, timeout=None):
        return None

    async def request(self, payload, **kw):
        self.calls.append((payload, kw.get("bucket")))
        p = {"DIGITOVER": (9 - int(payload.get("barrier", 0))) / 10, "DIGITUNDER": int(payload.get("barrier", 0)) / 10,
             "DIGITEVEN": 0.5}[payload["contract_type"]]
        payout = E.model_payout(payload["amount"], p)
        if payout > self.cap:
            raise DerivAPIError("ContractBuyValidationError", f"Minimum stake of 0.35 and maximum payout of "
                                f"{self.cap:.2f}. Current payout is {payout:.2f}.")
        return {"proposal": {"payout": payout, "ask_price": payload["amount"]}}


def test_quote_service_fetches_in_priority_order_and_uses_the_proposal_bucket(db):
    conn, _ = db
    book = QuoteBook(conn)
    needs = [QuoteNeed(2, QuoteKey("R_10", "DIGITOVER", 3, 1, 3.0), "ladder"),
             QuoteNeed(0, QuoteKey("R_10", "DIGITOVER", 3, 1, 1.0), "ev")]
    client = FakeQuoteClient()
    svc = QuoteService(client, book, lambda: needs)
    q1 = asyncio.run(svc.run_once(NOW))
    q2 = asyncio.run(svc.run_once(NOW))
    assert q1.key.stake == 1.0 and q2.key.stake == 3.0              # EV stake first
    assert all(b == "proposal" for _, b in client.calls)
    assert asyncio.run(svc.run_once(NOW)) is None                    # nothing due until refresh
    assert svc.due(NOW + 7 * 3600).key.stake == 1.0                  # refreshed after refresh_s
    assert "buy" not in str(client.calls)


def test_quote_service_records_refusals(db):
    conn, _ = db
    book = QuoteBook(conn)
    key = QuoteKey("R_10", "DIGITOVER", 3, 1, 729.0)
    svc = QuoteService(FakeQuoteClient(), book, lambda: [QuoteNeed(1, key, "ladder")])
    q = asyncio.run(svc.run_once(NOW))
    assert q.refused and book.limits[("R_10", "DIGITOVER")].max_payout == 1000.0

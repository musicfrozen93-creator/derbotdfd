"""Payout quotes: break-even from the real proposal, RateLimit handling, no fabricated values."""
import asyncio
from decimal import Decimal

import pytest

from app.analytics.payout import PayoutBook, break_even
from app.deriv.client import DerivAPIError


class QuoteClient:
    """Answers proposals like Deriv; the first ``limited`` calls get RateLimit."""

    def __init__(self, limited=0, invalid=()):
        self.calls = 0
        self.limited = limited
        self.invalid = set(invalid)

    async def request(self, payload, **kw):
        assert payload["proposal"] == 1 and "underlying_symbol" in payload and kw.get("bucket") == "proposal"
        self.calls += 1
        if self.calls <= self.limited:
            raise DerivAPIError("RateLimit", "You have reached the rate limit for proposal.")
        if payload["contract_type"] in self.invalid:
            raise DerivAPIError("ContractBuyValidationError", "Barrier is out of range.")
        return {"proposal": {"ask_price": Decimal("10.00"), "payout": Decimal("10.90")}}


def test_break_even_from_quote():
    assert break_even(Decimal("10"), Decimal("10.90")) == pytest.approx(0.917431, abs=1e-6)
    with pytest.raises(ValueError):
        break_even(Decimal("10"), Decimal("0"))


def test_rate_limit_is_retried_not_recorded_as_error(db):
    conn, _ = db
    book = PayoutBook(conn, stake=10)
    client = QuoteClient(limited=2)
    ok = asyncio.run(book.refresh(client, {("R_10", "DIGITOVER", 3, 1)}, rate_limit_pause_s=0))
    assert ok == 1 and book.failed == 0 and client.calls == 3
    q = book.get("R_10", "DIGITOVER", 3, 1)
    assert q.payout == Decimal("10.90")


def test_persistent_rate_limit_keeps_previous_quote(db):
    conn, _ = db
    book = PayoutBook(conn, stake=10)
    asyncio.run(book.refresh(QuoteClient(), {("R_10", "DIGITOVER", 3, 1)}))
    asyncio.run(book.refresh(QuoteClient(limited=99), {("R_10", "DIGITOVER", 3, 1)}, rate_limit_pause_s=0))
    assert book.failed == 1
    assert book.get("R_10", "DIGITOVER", 3, 1).payout == Decimal("10.90")     # earlier good quote survives


def test_contract_error_gives_no_payout(db):
    conn, _ = db
    book = PayoutBook(conn, stake=10)
    asyncio.run(book.refresh(QuoteClient(invalid={"DIGITUNDER"}), {("R_10", "DIGITUNDER", 9, 1)}))
    assert book.failed == 1 and book.get("R_10", "DIGITUNDER", 9, 1) is None   # never invented

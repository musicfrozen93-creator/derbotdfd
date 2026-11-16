"""Payouts and break-even probabilities from live public ``proposal`` quotes.

Verified live (2026-11-16): the public endpoint answers one-shot ``proposal``
requests without an API token (field ``underlying_symbol``). No contract is ever
bought. Break-even win probability = stake / payout.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from app.deriv.client import DerivAPIError, NotConnectedError
from app.monitoring.logger import event
from app.storage import repositories as repo

log = logging.getLogger("analytics.payout")


def break_even(stake: Decimal, payout: Decimal) -> float:
    """Win probability at which expected profit is zero: p*payout = stake."""
    if payout is None or stake is None or Decimal(payout) <= 0:
        raise ValueError("payout must be positive")
    return float(Decimal(stake) / Decimal(payout))


@dataclass
class Quote:
    symbol: str
    contract_type: str
    barrier: int | None
    duration: int
    stake: Decimal | None
    payout: Decimal | None
    break_even: float | None
    fetched_at: float
    error: str | None = None

    @property
    def key(self) -> tuple:
        return (self.symbol, self.contract_type, "" if self.barrier is None else str(self.barrier), self.duration)


def payout_key(symbol: str, contract_type: str, barrier: int | None, duration: int) -> tuple:
    return (symbol, contract_type, "" if barrier is None else str(barrier), int(duration))


async def fetch_quote(client, symbol: str, contract_type: str, barrier: int | None, duration: int,
                      stake: float, currency: str) -> Quote:
    req = {"proposal": 1, "amount": stake, "basis": "stake", "contract_type": contract_type,
           "currency": currency, "duration": int(duration), "duration_unit": "t",
           "underlying_symbol": symbol}
    if barrier is not None:
        req["barrier"] = str(barrier)
    msg = await client.request(req, bucket="proposal")
    prop = msg.get("proposal") or {}
    ask = Decimal(str(prop.get("ask_price")))
    payout = Decimal(str(prop.get("payout")))
    return Quote(symbol, contract_type, barrier, duration, ask, payout, break_even(ask, payout), time.time())


class PayoutBook:
    def __init__(self, conn, *, stake: float = 10, currency: str = "USD", max_age_s: float = 3 * 3600) -> None:
        self.conn = conn
        self.stake = stake
        self.currency = currency
        self.max_age_s = max_age_s
        self.quotes: dict[tuple, Quote] = {}
        self.failed = 0
        self._load()

    def _load(self) -> None:
        for r in repo.load_payouts(self.conn):
            barrier = None if r["barrier"] in ("", None) else int(r["barrier"])
            q = Quote(r["symbol"], r["contract_type"], barrier, int(r["duration"]),
                      Decimal(r["stake"]) if r["stake"] else None, Decimal(r["payout"]) if r["payout"] else None,
                      r["break_even"], float(r["fetched_at"] or 0), r["error"])
            self.quotes[q.key] = q

    def get(self, symbol: str, contract_type: str, barrier: int | None, duration: int,
            now: float | None = None) -> Quote | None:
        q = self.quotes.get(payout_key(symbol, contract_type, barrier, duration))
        if q is None or q.break_even is None:
            return None
        if (now or time.time()) - q.fetched_at > self.max_age_s:
            return None
        return q

    async def refresh(self, client, needed: set[tuple], *, rate_limit_pause_s: float = 60,
                      rate_limit_retries: int = 3) -> int:
        """Fetch quotes for every (symbol, contract_type, barrier, duration) needed.

        Deriv answers bursts of proposals with ``RateLimit`` (seen live at ~80 quotes in under
        30 s). That is not a fact about the contract, so the quote is retried after a pause
        instead of being recorded as an error. ``self.failed`` counts what is still missing."""
        ok = 0
        self.failed = 0
        for (symbol, ctype, barrier, duration) in sorted(needed, key=str):
            try:
                for attempt in range(rate_limit_retries + 1):
                    try:
                        q = await fetch_quote(client, symbol, ctype, barrier, duration, self.stake,
                                              self.currency)
                        break
                    except DerivAPIError as exc:
                        if exc.code != "RateLimit" or attempt >= rate_limit_retries:
                            raise
                        event(log, "payout_rate_limited", logging.INFO, symbol=symbol, contract=ctype,
                              pause_s=rate_limit_pause_s, attempt=attempt + 1)
                        await asyncio.sleep(rate_limit_pause_s)
            except DerivAPIError as exc:
                self.failed += 1
                if exc.code == "RateLimit":       # transient: keep any previous good quote
                    event(log, "payout_quote_failed", logging.WARNING, symbol=symbol, contract=ctype,
                          barrier=barrier, duration=duration, error=str(exc))
                    continue
                event(log, "payout_quote_failed", logging.WARNING, symbol=symbol, contract=ctype,
                      barrier=barrier, duration=duration, error=str(exc))
                repo.save_payout_error(self.conn, symbol, ctype, "" if barrier is None else barrier, duration,
                                       str(exc))
                continue
            except (NotConnectedError, asyncio.TimeoutError, ValueError, ArithmeticError) as exc:
                self.failed += 1
                event(log, "payout_quote_failed", logging.WARNING, symbol=symbol, contract=ctype, error=str(exc))
                continue
            self.quotes[q.key] = q
            repo.save_payout(self.conn, symbol, ctype, "" if barrier is None else barrier, duration,
                             q.stake, q.payout, q.break_even)
            ok += 1
        event(log, "payout_refresh_done", requested=len(needed), ok=ok)
        return ok

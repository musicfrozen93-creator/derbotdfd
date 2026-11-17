"""One-shot public ``proposal`` quote (read-only diagnostic for scripts/live_api_check.py).

The analyzer no longer uses payouts or break-even for signals (the signal rule is the digit
percentage rule in app/analytics/validation.py). No contract is ever bought.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal


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

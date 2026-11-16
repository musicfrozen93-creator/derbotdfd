"""contracts_for: which digit contracts, durations and predictions a symbol offers.

Verified live (2026-11-16) on the public endpoint: digit contracts are listed with
``expiry_type: "tick"``, ``min_contract_duration: "1t"``, ``max_contract_duration:
"10t"`` and ``last_digit_range`` (DIGITOVER 0-8, DIGITUNDER 1-9). The new API
rejects a ``currency`` field on contracts_for.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("deriv.contracts")

_DUR_RE = re.compile(r"^\s*(\d+)\s*([a-zA-Z]+)\s*$")


def parse_tick_duration(value) -> int | None:
    """'10t' -> 10. Non-tick units -> None."""
    if value is None:
        return None
    m = _DUR_RE.match(str(value))
    if not m or m.group(2).lower() != "t":
        return None
    return int(m.group(1))


@dataclass
class DigitContract:
    contract_type: str
    min_ticks: int
    max_ticks: int
    digit_range: list[int] | None = None   # allowed predictions; None = no prediction

    def supports(self, duration: int, barrier: int | None) -> tuple[bool, str]:
        if not (self.min_ticks <= duration <= self.max_ticks):
            return False, f"duration {duration}t outside {self.min_ticks}-{self.max_ticks}t"
        if barrier is not None:
            if self.digit_range is None:
                return False, "prediction not accepted"
            if int(barrier) not in self.digit_range:
                return False, f"prediction {barrier} not in {self.digit_range}"
        return True, "ok"


@dataclass
class SymbolContracts:
    symbol: str
    contracts: dict[str, DigitContract] = field(default_factory=dict)

    def get(self, contract_type: str) -> DigitContract | None:
        return self.contracts.get(contract_type)


def parse_contracts_for(symbol: str, msg: dict) -> SymbolContracts:
    out = SymbolContracts(symbol=symbol)
    cf = msg.get("contracts_for") or {}
    for c in cf.get("available") or []:
        if not isinstance(c, dict):
            continue
        ctype = c.get("contract_type")
        if not ctype or c.get("expiry_type") not in (None, "tick"):
            continue
        lo = parse_tick_duration(c.get("min_contract_duration"))
        hi = parse_tick_duration(c.get("max_contract_duration"))
        if lo is None or hi is None:
            continue
        rng = c.get("last_digit_range")
        digits = sorted(int(x) for x in rng) if isinstance(rng, list) else None
        prev = out.contracts.get(ctype)
        if prev:  # merge duplicate listings
            prev.min_ticks = min(prev.min_ticks, lo)
            prev.max_ticks = max(prev.max_ticks, hi)
            if digits is not None:
                prev.digit_range = sorted(set(prev.digit_range or []) | set(digits))
        else:
            out.contracts[ctype] = DigitContract(ctype, lo, hi, digits)
    return out


async def fetch_contracts(client, symbol: str) -> SymbolContracts:
    msg = await client.request({"contracts_for": symbol})
    return parse_contracts_for(symbol, msg)

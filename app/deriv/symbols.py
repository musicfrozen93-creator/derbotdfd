"""active_symbols: market discovery and pip-precision discovery.

The new Deriv API (api.derivws.com) names the field ``underlying_symbol``; the
legacy API used ``symbol``. Both are accepted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.market.precision import decimals_from_step

log = logging.getLogger("deriv.symbols")


@dataclass(frozen=True)
class MarketInfo:
    symbol: str
    display_name: str
    market: str
    submarket: str
    pip_decimals: int
    exchange_is_open: bool
    is_trading_suspended: bool

    @property
    def tradable(self) -> bool:
        return self.exchange_is_open and not self.is_trading_suspended


def parse_active_symbols(msg: dict) -> list[MarketInfo]:
    out: list[MarketInfo] = []
    for item in msg.get("active_symbols") or []:
        if not isinstance(item, dict):
            continue
        symbol = item.get("underlying_symbol") or item.get("symbol")
        pip = item.get("pip_size", item.get("pip"))
        if not symbol or pip is None:
            continue
        try:
            decimals = decimals_from_step(pip)
        except ValueError:
            log.warning("event=bad_pip_size symbol=%s pip=%s", symbol, pip)
            continue
        out.append(MarketInfo(
            symbol=str(symbol),
            display_name=str(item.get("underlying_symbol_name") or item.get("display_name") or symbol),
            market=str(item.get("market") or ""),
            submarket=str(item.get("submarket") or ""),
            pip_decimals=decimals,
            exchange_is_open=bool(int(item.get("exchange_is_open") or 0)),
            is_trading_suspended=bool(int(item.get("is_trading_suspended") or 0)),
        ))
    return out


async def fetch_active_symbols(client) -> list[MarketInfo]:
    msg = await client.request({"active_symbols": "brief"})
    return parse_active_symbols(msg)


def select_candidates(markets: list[MarketInfo], *, include_markets: list[str],
                      include_submarkets: list[str], exclude_symbols: list[str]) -> list[MarketInfo]:
    inc_m = set(include_markets or [])
    inc_s = set(include_submarkets or [])
    exc = set(exclude_symbols or [])
    out = []
    for m in markets:
        if inc_m and m.market not in inc_m:
            continue
        if inc_s and m.submarket not in inc_s:
            continue
        if m.symbol in exc:
            continue
        out.append(m)
    return out

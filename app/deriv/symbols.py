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


# Deriv's own submarket codes (active_symbols "submarket"), with readable names for logs/reports.
CONTINUOUS_INDICES = "random_index"
SUBMARKET_LABELS = {
    "random_index": "Continuous Indices",
    "jump_index": "Jump Indices",
    "crash_index": "Crash/Boom Indices",
    "range_index": "Range Break Indices",
    "random_daily": "Daily Reset Indices",
    "step_index": "Step Indices",
    "forex_basket": "Forex Basket",
    "commodity_basket": "Commodities Basket",
}


def submarket_label(submarket: str) -> str:
    return SUBMARKET_LABELS.get(submarket, submarket or "unknown")


def is_one_second(m: MarketInfo) -> bool:
    """1-second variant of a Continuous Index (1HZ10V = "Volatility 10 (1s) Index").

    The API lists these in the same submarket as the 2-second indices (``random_index``) and has
    no field that tells them apart, so the symbol prefix and the "(1s)" in the name are used —
    only *within* the API's Continuous Indices category, never to decide the category itself."""
    return m.symbol.upper().startswith("1HZ") or "(1s)" in m.display_name.lower()


def classify_market(m: MarketInfo, *, include_markets: list[str], include_submarkets: list[str],
                    exclude_symbols: list[str], one_second_symbols: list[str] | None = None) -> tuple[bool, str]:
    """Decide whether a listed market may be monitored. Returns (allowed, reason).

    Order: API market -> API submarket -> 1-second allow-list -> explicit exclusions.
    ``one_second_symbols=None`` means no 1-second restriction."""
    inc_m = set(include_markets or [])
    inc_s = set(include_submarkets or [])
    if inc_m and m.market not in inc_m:
        return False, f"market '{m.market}' not in discovery.markets"
    if inc_s and m.submarket not in inc_s:
        return False, f"{submarket_label(m.submarket)} (submarket '{m.submarket}') not in discovery.include_submarkets"
    if one_second_symbols is not None and is_one_second(m) and m.symbol not in set(one_second_symbols):
        return False, "1-second index not in discovery.one_second_symbols"
    if m.symbol in set(exclude_symbols or []):
        return False, "listed in discovery.exclude_symbols"
    return True, f"{submarket_label(m.submarket)} (submarket '{m.submarket}')"


def select_candidates(markets: list[MarketInfo], *, include_markets: list[str],
                      include_submarkets: list[str], exclude_symbols: list[str],
                      one_second_symbols: list[str] | None = None) -> list[MarketInfo]:
    """The markets that pass :func:`classify_market` — the only symbols that ever reach
    contracts_for, the compatibility matrix, tick subscriptions and evaluation."""
    return [m for m in markets
            if classify_market(m, include_markets=include_markets, include_submarkets=include_submarkets,
                               exclude_symbols=exclude_symbols, one_second_symbols=one_second_symbols)[0]]

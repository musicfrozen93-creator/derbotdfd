"""ticks_history paging.

Verified live: the public endpoint returns at most 1,000 ticks per request, even
when a larger ``count`` is requested; ranges are end-anchored, so we page
backwards by moving ``end`` to (oldest epoch - 1).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from app.market.precision import decimals_from_count, format_quote

log = logging.getLogger("deriv.history")


@dataclass
class HistoryPage:
    symbol: str
    epochs: list[int]
    quotes: list[Decimal]
    pip_decimals: int | None


def parse_history(symbol: str, msg: dict) -> HistoryPage:
    hist = msg.get("history") or {}
    prices = hist.get("prices") or []
    times = hist.get("times") or []
    if len(prices) != len(times):
        raise ValueError(f"history length mismatch for {symbol}: {len(prices)} prices / {len(times)} times")
    pip = msg.get("pip_size")
    decimals = None
    if pip is not None:
        try:
            decimals = decimals_from_count(pip)
        except ValueError:
            decimals = None
    epochs = [int(t) for t in times]
    quotes = [p if isinstance(p, Decimal) else Decimal(str(p)) for p in prices]
    return HistoryPage(symbol, epochs, quotes, decimals)


async def fetch_page(client, symbol: str, *, end: int | str = "latest", start: int | None = None,
                     count: int = 1000) -> HistoryPage:
    req = {"ticks_history": symbol, "end": end, "count": count, "style": "ticks"}
    if start is not None:
        req["start"] = int(start)
    msg = await client.request(req)
    return parse_history(symbol, msg)


def page_rows(page: HistoryPage, decimals: int) -> list[tuple[str, int, str, int]]:
    """Convert a page into (symbol, epoch, quote_text, digit) rows."""
    dec = page.pip_decimals if page.pip_decimals is not None else decimals
    rows = []
    for epoch, quote in zip(page.epochs, page.quotes):
        text = format_quote(quote, dec)
        rows.append((page.symbol, epoch, text, int(text[-1])))
    return rows


async def fetch_range_backwards(client, symbol: str, start: int, end: int | str, *, page_size: int = 1000,
                                max_pages: int = 10_000):
    """Yield pages from ``end`` back to ``start`` (inclusive), newest first."""
    cur_end: int | str = end
    for _ in range(max_pages):
        page = await fetch_page(client, symbol, end=cur_end, start=start, count=page_size)
        if not page.epochs:
            return
        yield page
        oldest = min(page.epochs)
        if oldest <= start or len(page.epochs) < page_size:
            return
        cur_end = oldest - 1

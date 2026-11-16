"""Live tick message parsing."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.market.precision import decimals_from_count


@dataclass(frozen=True)
class RawTick:
    symbol: str
    epoch: int
    quote: Decimal
    pip_decimals: int | None


def parse_tick(tick: dict) -> RawTick:
    """Validate and parse the ``tick`` object of a ``msg_type: tick`` message.

    Raises ValueError on malformed content.
    """
    symbol = tick.get("symbol") or tick.get("underlying_symbol")
    if not symbol:
        raise ValueError("tick without symbol")
    epoch = tick.get("epoch")
    quote = tick.get("quote")
    if epoch is None or quote is None:
        raise ValueError(f"tick missing epoch/quote: {tick!r}")
    epoch_i = int(epoch)
    if epoch_i <= 0:
        raise ValueError(f"bad epoch: {epoch!r}")
    q = quote if isinstance(quote, Decimal) else Decimal(str(quote))
    if not q.is_finite():
        raise ValueError(f"bad quote: {quote!r}")
    pip = tick.get("pip_size")
    decimals = None
    if pip is not None:
        try:
            decimals = decimals_from_count(pip)
        except ValueError:
            decimals = None
    return RawTick(str(symbol), epoch_i, q, decimals)

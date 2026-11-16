"""Exact quote formatting and last-digit extraction.

Never use binary floating point for digit extraction. Quotes are parsed from the
raw JSON as :class:`decimal.Decimal` and formatted to the symbol's pip precision,
so trailing zeros are preserved (1234.50 -> last digit 0, not 5).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def _to_decimal(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # repr() of a float is the shortest string that round-trips; the pip
        # quantisation below restores any trailing zeros.
        return Decimal(repr(value))
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"invalid quote: {value!r}") from exc


def decimals_from_step(step) -> int:
    """active_symbols gives pip_size as a step, e.g. 0.001 -> 3 decimals."""
    d = _to_decimal(step)
    if d <= 0 or d > 1:
        raise ValueError(f"invalid pip step: {step!r}")
    if d == 1:
        return 0
    exp = d.normalize().as_tuple().exponent
    return int(-exp)


def decimals_from_count(count) -> int:
    """tick / ticks_history give pip_size as a count of decimals, e.g. 3 or 3.0."""
    d = _to_decimal(count)
    if d < 0 or d != d.to_integral_value():
        raise ValueError(f"invalid pip count: {count!r}")
    return int(d)


def format_quote(quote, decimals: int) -> str:
    """Format a quote to exactly ``decimals`` places, as fixed-point text."""
    if decimals < 0:
        raise ValueError("decimals must be >= 0")
    d = _to_decimal(quote)
    if not d.is_finite():
        raise ValueError(f"non-finite quote: {quote!r}")
    q = Decimal(1).scaleb(-decimals)
    return format(d.quantize(q, rounding=ROUND_HALF_UP), "f")


def last_digit(quote, decimals: int) -> int:
    text = format_quote(quote, decimals)
    ch = text[-1]
    if not ch.isdigit():  # pragma: no cover - format guarantees a digit
        raise ValueError(f"cannot extract digit from {text!r}")
    return int(ch)


def scaled_int(quote_text: str) -> int:
    """Fixed-point text -> integer in pip units ("4849.430" -> 4849430)."""
    neg = quote_text.startswith("-")
    body = quote_text[1:] if neg else quote_text
    value = int(body.replace(".", ""))
    return -value if neg else value

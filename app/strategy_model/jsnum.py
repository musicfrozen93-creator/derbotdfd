"""JavaScript number semantics used by DBot, reproduced exactly.

DBot runs the bot as JavaScript. Two operations matter for stakes:

* the stake sent with every purchase is ``+(Number(amount).toFixed(2))``
  (deriv-app ``bot-skeleton/.../trade_definition_tradeoptions.js``);
* the running total profit is rounded to the currency's decimals after every contract
  (``bot-skeleton/.../trade/Total.js``).

``Number.prototype.toFixed`` rounds the *exact* binary value of the double to the nearest
multiple of 10^-2, ties away from zero. Python floats are the same IEEE-754 doubles and every
stake update in the XML is a plain multiply / add, so Python arithmetic followed by this
rounding gives the same amounts as DBot.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Context, Decimal

_CENT = Decimal("0.01")
# Any finite double has at most 309 integer digits; keep every digit while rounding to cents.
_CTX = Context(prec=400, rounding=ROUND_HALF_UP)


def to_fixed2(x: float) -> float:
    """JavaScript ``+(Number(x).toFixed(2))`` for finite x."""
    return float(Decimal(x).quantize(_CENT, rounding=ROUND_HALF_UP, context=_CTX))


def cents(x: float) -> str:
    """Fixed two-decimal text, as Deriv shows amounts."""
    return f"{to_fixed2(x):.2f}"

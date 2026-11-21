"""Entry rules evaluated on stored tick history, one boolean per tick, with no look-ahead.

``mask[t]`` is True when the bot's ``before_purchase`` would purchase on tick ``t`` using only ticks
``<= t``. Ticks without enough history for the rule are False and are reported as not evaluable
(``first_evaluable``).

Prices: DBot receives each quote as a JSON number (an IEEE double) and computes SMA(8) as
``sum(last 8 values in order) / 8`` (``binary-indicators`` simpleMovingAverage). The stored quote is
exact decimal text; ``int / 10**decimals`` gives the same correctly rounded double as JavaScript's
parse, and the eight additions below are done in the same order, so ties resolve as in DBot.
"""
from __future__ import annotations

import numpy as np

from app.strategy_model.bots import EntryRule


def prices_as_doubles(prices_int: np.ndarray, decimals: int) -> np.ndarray:
    """Scaled integer quotes -> the doubles DBot sees."""
    return np.asarray(prices_int, dtype=np.int64) / float(10 ** int(decimals))


def entry_mask(rule: EntryRule, digits: np.ndarray, prices: np.ndarray | None = None) -> tuple[np.ndarray, int]:
    """Return (mask, first_evaluable_index)."""
    d = np.asarray(digits, dtype=np.int16)
    n = d.shape[0]
    if rule.kind == "every_tick":
        return np.ones(n, dtype=bool), 0
    if rule.kind == "last_digit_in":
        return np.isin(d, np.array(rule.digits, dtype=np.int16)), 0
    if rule.kind == "crysis_pattern":
        # X1 = d[t] ... X7 = d[t-6]; X = #(Xi < 4); purchase if X > 3 and X1 > 4
        m = np.zeros(n, dtype=bool)
        if n >= 7:
            low = (d < 4).astype(np.int16)
            c = np.cumsum(np.concatenate(([0], low)))
            x = c[7:] - c[:-7]                      # count over d[t-6 .. t] for t = 6 .. n-1
            m[6:] = (x > 3) & (d[6:] > 4)
        return m, 6
    if rule.kind == "price_ge_sma":
        if prices is None:
            raise ValueError("price_ge_sma needs prices")
        p = np.asarray(prices, dtype=np.float64)
        per = int(rule.period)
        m = np.zeros(n, dtype=bool)
        if n >= per:
            s = p[0:n - per + 1].copy()             # oldest of each window
            for j in range(1, per):                 # same left-to-right order as Array.reduce
                s = s + p[j:n - per + 1 + j]
            ma = s / per
            last = p[per - 1:]
            m[per - 1:] = ~(last < ma)              # IF tick < MA -> no purchase, ELSE purchase
        return m, per - 1
    raise ValueError(f"unsupported entry rule {rule.kind}")

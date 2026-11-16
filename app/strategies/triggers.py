"""Vectorised replay of each bot's *purchase* conditions on stored ticks.

Only conditions that gate a ``purchase`` block in the original XML are modelled.
Notification-only logic (candle colour, pivot points, random numbers, ...) is not.

Each trigger returns ``(mask, lookback)``: ``mask[i]`` is True when the bot
would buy on tick ``i``; ``lookback`` is how many earlier ticks the condition
reads (needed to reject windows that cross a data gap).
"""
from __future__ import annotations

import numpy as np


def compute_trigger(spec: dict, digits: np.ndarray, prices: np.ndarray) -> tuple[np.ndarray, int]:
    n = digits.shape[0]
    t = spec["type"]
    if t == "every_tick":
        return np.ones(n, dtype=bool), 0
    if t == "last_digit_in":
        return np.isin(digits, np.asarray(spec["digits"], dtype=digits.dtype)), 0
    if t == "window_digit_count":
        w = int(spec["window"])
        hit = np.isin(digits, np.asarray(spec["digits"], dtype=digits.dtype)).astype(np.int32)
        cs = np.concatenate(([0], np.cumsum(hit)))
        mask = np.zeros(n, dtype=bool)
        if n >= w:
            counts = cs[w:] - cs[:-w]           # counts[j] = sum(hit[j : j+w])  -> window ending at j+w-1
            mask[w - 1:] = counts >= int(spec["min_count"])
        return mask, w - 1
    if t == "price_vs_sma":
        p = int(spec["period"])
        # Exact integer arithmetic on pip-scaled prices: price >= SMA  <=>  p*price >= sum(window)
        cs = np.concatenate(([0], np.cumsum(prices, dtype=np.int64)))
        mask = np.zeros(n, dtype=bool)
        if n >= p:
            sums = cs[p:] - cs[:-p]
            cur = prices[p - 1:].astype(np.int64) * p
            op = spec["op"]
            if op == "gte":
                res = cur >= sums
            elif op == "gt":
                res = cur > sums
            elif op == "lte":
                res = cur <= sums
            else:
                res = cur < sums
            mask[p - 1:] = res
        return mask, p - 1
    if t in ("all_of", "any_of"):
        masks, lbs = [], []
        for sub in spec["conditions"]:
            m, lb = compute_trigger(sub, digits, prices)
            masks.append(m)
            lbs.append(lb)
        out = masks[0].copy()
        for m in masks[1:]:
            out = (out & m) if t == "all_of" else (out | m)
        return out, max(lbs)
    raise ValueError(f"unknown trigger type {t}")


def describe_trigger(spec: dict) -> str:
    t = spec["type"]
    if t == "every_tick":
        return "every tick (no entry filter)"
    if t == "last_digit_in":
        d = spec["digits"]
        return f"last digit = {d[0]}" if len(d) == 1 else f"last digit in {{{', '.join(map(str, d))}}}"
    if t == "window_digit_count":
        return (f"≥{spec['min_count']} of last {spec['window']} digits in "
                f"{{{', '.join(map(str, spec['digits']))}}}")
    if t == "price_vs_sma":
        sym = {"gte": "≥", "gt": ">", "lte": "≤", "lt": "<"}[spec["op"]]
        return f"last price {sym} SMA({spec['period']}) of prices"
    joiner = " AND " if t == "all_of" else " OR "
    return joiner.join(describe_trigger(s) for s in spec["conditions"])

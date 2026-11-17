"""Human-readable text of each bot's entry trigger (from its strategy YAML).

Used for the strategy inventory (``python -m app.main --validate``). The signal rule does not
use the triggers: START / STOP depend only on the digit percentages of each bot's winning digits.
"""
from __future__ import annotations

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

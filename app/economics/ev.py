"""Expected value of one contract leg from a live quote.

Deriv digit contracts pay ``payout`` on a win and nothing on a loss, and the stake is the price, so
per trade:

    net win  = payout - stake
    net loss = stake
    EV       = p * net_win - (1 - p) * net_loss
    break-even probability = stake / payout

``p`` is estimated from the bot's OWN entry ticks over the long window (outcomes settled on the
correct tick). The status uses one-sided Wilson bounds at the configured confidence, so it never
calls a contract profitable from a point estimate:

* POSITIVE     - EV is positive even at the lower probability bound
* NEGATIVE     - EV is negative even at the upper probability bound
* INCONCLUSIVE - the interval straddles break-even
* NOT_CHECKED  - fewer settled outcomes than ``min_outcomes``
* UNKNOWN      - no valid, fresh quote for this exact stake (never treated as PASS)

``ev_fair`` is the EV if the digits are uniform (p = winning digits / 10), shown for reference.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from app.economics.quotes import Quote
from app.strategy_model.outcomes import wilson, z_one_sided

POSITIVE, NEGATIVE, INCONCLUSIVE, NOT_CHECKED, UNKNOWN = "POSITIVE", "NEGATIVE", "INCONCLUSIVE", "NOT_CHECKED", "UNKNOWN"


@dataclass
class LegEconomics:
    leg_id: str
    contract: str
    stake: float
    status: str
    detail: str
    payout: float | None = None
    quote_age_s: float | None = None
    net_win: float | None = None
    net_loss: float | None = None
    break_even: float | None = None
    p_fair: float | None = None
    ev_fair: float | None = None
    n: int = 0
    p_hat: float | None = None
    p_low: float | None = None
    p_high: float | None = None
    ev_hat: float | None = None
    ev_low: float | None = None
    ev_high: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def ev(p: float, payout: float, stake: float) -> float:
    return p * (payout - stake) - (1 - p) * stake


def leg_economics(leg_id: str, contract: str, stake: float, quote: Quote | None, p_fair: float,
                  wins: int, n: int, *, confidence: float, min_outcomes: int, max_age_s: float,
                  now: float) -> LegEconomics:
    e = LegEconomics(leg_id, contract, stake, UNKNOWN, "", p_fair=p_fair, n=n)
    if n:
        e.p_hat = wins / n
        e.p_low, e.p_high = wilson(wins, n, z_one_sided(confidence))
    if quote is None:
        e.detail = "no Deriv quote yet for this exact stake"
        return e
    e.quote_age_s = quote.age(now)
    if not quote.ok:
        e.detail = ("Deriv refuses this stake: " + (quote.refused_reason or "")) if quote.refused else \
            f"last quote failed ({quote.error_code}); waiting for a fresh one"
        return e
    if e.quote_age_s > max_age_s:
        e.detail = f"quote is {e.quote_age_s / 3600:.1f} h old (limit {max_age_s / 3600:g} h)"
        return e
    e.payout = quote.payout
    e.net_win = quote.payout - stake
    e.net_loss = stake
    e.break_even = stake / quote.payout
    e.ev_fair = ev(p_fair, quote.payout, stake)
    if n < min_outcomes:
        e.status = NOT_CHECKED
        e.detail = f"only {n} settled outcomes of this bot's entries (need {min_outcomes})"
        return e
    e.ev_hat = ev(e.p_hat, quote.payout, stake)
    e.ev_low = ev(e.p_low, quote.payout, stake)
    e.ev_high = ev(e.p_high, quote.payout, stake)
    if e.ev_low > 0:
        e.status, e.detail = POSITIVE, (f"win rate {e.p_hat:.2%} (lower bound {e.p_low:.2%}) above break-even "
                                        f"{e.break_even:.2%}")
    elif e.ev_high < 0:
        e.status, e.detail = NEGATIVE, (f"win rate {e.p_hat:.2%} (upper bound {e.p_high:.2%}) below break-even "
                                        f"{e.break_even:.2%}")
    else:
        e.status, e.detail = INCONCLUSIVE, (f"win rate {e.p_hat:.2%} ({e.p_low:.2%} - {e.p_high:.2%}) straddles "
                                            f"break-even {e.break_even:.2%}")
    return e


def combine(legs: list[LegEconomics]) -> tuple[str, str]:
    """A bot can trade any of its legs, so it is POSITIVE only if every leg is."""
    if not legs:
        return UNKNOWN, "no legs"
    st = [x.status for x in legs]
    if any(s == NEGATIVE for s in st):
        bad = [x for x in legs if x.status == NEGATIVE]
        return NEGATIVE, "; ".join(f"{x.contract}: {x.detail}" for x in bad)
    if any(s == UNKNOWN for s in st):
        return UNKNOWN, "; ".join(f"{x.contract}: {x.detail}" for x in legs if x.status == UNKNOWN)
    if all(s == POSITIVE for s in st):
        return POSITIVE, "every contract the bot can buy has a positive expected value (lower confidence bound)"
    if any(s == NOT_CHECKED for s in st):
        return NOT_CHECKED, "; ".join(f"{x.contract}: {x.detail}" for x in legs if x.status == NOT_CHECKED)
    return INCONCLUSIVE, "; ".join(f"{x.contract}: {x.detail}" for x in legs if x.status == INCONCLUSIVE)


# ------------------------------------------------------------------ payout formula cross-check (reporting only)
def model_payout(stake: float, p: float) -> float:
    """Payout formula inferred on 2026-11-20 from 168 quotes: commission = max(1.2 % of payout, 0.02).
    Used only to cross-check live quotes; never used in place of a quote."""
    raw = min(stake / (p + 0.012), (stake - 0.02) / p)
    return round(raw * 100 + 1e-7) / 100


def validate_model(quotes: list[Quote], p_of) -> dict:
    """Compare every successful live quote with the formula. ``p_of(key) -> p``."""
    checked, mism = 0, []
    for q in quotes:
        if not q.ok or q.payout is None:
            continue
        checked += 1
        m = model_payout(q.key.stake, p_of(q.key))
        if abs(m - q.payout) > 0.005:
            mism.append({"key": q.key.db, "quoted": q.payout, "formula": m})
    status = "UNVALIDATED" if checked == 0 else ("VALIDATED" if not mism else "MISMATCH")
    return {"status": status, "checked": checked, "mismatches": mism[:20], "mismatch_count": len(mism)}

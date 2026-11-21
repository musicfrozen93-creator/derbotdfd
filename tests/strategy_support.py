"""Test support for the bot-specific evaluator: a fake Deriv quote source and runnable settings.

``deriv_like_payout`` is the payout formula validated against 168 live Deriv quotes (2026-11-20).
``favourable_payout`` is NOT a Deriv price: it pays 5 % more than fair odds (stake / p * 1.05), which
no real digit contract does. It exists only so tests can reach the START path, which needs a
POSITIVE expected value. Nothing here talks to Deriv.
"""
from __future__ import annotations

from app.config import DEFAULTS, Section
from app.economics.ev import model_payout
from app.economics.quotes import parse_error, parse_ok
from app.strategy_model.bots import win_digits


def fair_p(key) -> float:
    return len(win_digits(key.contract_type, key.barrier)) / 10


def deriv_like_payout(stake: float, p: float) -> float:
    return model_payout(stake, p)


def favourable_payout(stake: float, p: float) -> float:
    """NOT a real Deriv price (see module docstring)."""
    return round(stake / p * 1.05, 2)


def fake_quote(key, now: float, *, payout_fn=deriv_like_payout, cap: float | None = 1000.0, min_stake: float = 0.35):
    if key.stake < min_stake:
        return parse_error(key, "ContractBuyValidationError",
                           f"Please enter a stake amount that's at least {min_stake}.", now)
    payout = payout_fn(key.stake, fair_p(key))
    if cap is not None and payout > cap:
        return parse_error(key, "ContractBuyValidationError",
                           f"Minimum stake of {min_stake} and maximum payout of {cap:.2f}. Current payout is "
                           f"{payout:.2f}.", now)
    return parse_ok(key, {"proposal": {"payout": payout, "ask_price": key.stake}}, now)


def fill_quotes(analyzer, book, combos, now: float, **kw) -> int:
    """Answer every quote the evaluation asks for, the way the quote service would (a ladder advances
    one accepted stake at a time and stops at the first refusal). Returns the number of quotes."""
    n = 0
    for _ in range(300):
        todo = {need.key.db: need.key for need in analyzer.quote_needs(combos) if book.get(need.key) is None}
        if not todo:
            return n
        for k in todo.values():
            book.put(fake_quote(k, now, **kw))
            n += 1
    raise AssertionError("quote needs did not converge")


def risk_settings(**over) -> Section:
    return Section({**DEFAULTS["risk"], **over})


def make_runnable(app, now: float, *, bankroll: float | None = 100_000, payout_fn=favourable_payout,
                  cap: float | None = 1000.0) -> int:
    """Configure a bankroll and answer every needed quote, so the four gates CAN pass (each still has to
    pass on the stored ticks). Returns the number of quotes created."""
    app.analyzer.risk_settings = risk_settings(bankroll_usd=bankroll)
    combos, _ = app.combos()
    return fill_quotes(app.analyzer, app.quote_book, combos, now, payout_fn=payout_fn, cap=cap)

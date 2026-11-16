"""Stake-ladder (martingale) risk, reproduced from each bot's after-purchase logic.

These are *theoretical* figures from the XML code and the theoretical win
probability of each leg; they are not trading results.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.strategies.loader import BotSpec


@dataclass
class Ladder:
    stakes: list[float]          # stake of the k-th consecutive losing trade (1-based -> index k-1)
    cumulative: list[float]      # total lost after k consecutive losses
    p_streak: list[float]        # P(k consecutive losses) from a fresh base trade
    stop_loss_breach: int | None  # loss number at which the stop-loss triggers (None = never/no SL)
    resets: list[int]            # loss numbers after which the ladder resets (e.g. Predator v2)


def stake_sequence(stake: dict, k: int) -> tuple[list[float], list[int]]:
    """Stakes for k consecutive losing trades, per the bot's stake model."""
    model = stake.get("model", "flat")
    base = float(stake.get("base", 1))
    out, resets = [], []
    if model == "flat":
        return [base] * k, resets
    if model == "multiply":
        factor = float(stake["factor"])
        flat_losses = int(stake.get("flat_losses", 0))
        reset_gte = stake.get("reset_if_single_loss_gte")
        s = base
        for j in range(1, k + 1):
            out.append(round(s, 10))
            if reset_gte is not None and s >= float(reset_gte):
                resets.append(j)
                s = base
            elif j > flat_losses:
                s = s * factor
        return out, resets
    if model == "crysis":
        # Loss: Y = Y + |loss|; stake = 2*Y.  Steady state after a win: Y = 0, stake = base.
        y = float(stake.get("steady_state_y", 0))
        s = base
        for _ in range(k):
            out.append(round(s, 10))
            y += s
            s = 2 * y
        return out, resets
    raise ValueError(f"unknown stake model {model}")


def ladder(bot: BotSpec, k: int = 12, win_probs: dict[str, float] | None = None) -> Ladder:
    stakes, resets = stake_sequence(bot.stake, k)
    cumulative, total = [], 0.0
    for s in stakes:
        total += s
        cumulative.append(round(total, 2))
    steps = bot.steps
    p, p_streak = 1.0, []
    overflow = bot.sequence.get("overflow", "hold_last")
    for j in range(k):
        idx = j if j < len(steps) else (len(steps) - 1 if overflow == "hold_last" else None)
        if idx is None:
            p_streak.append(float("nan"))
            continue
        leg = steps[idx]
        pw = (win_probs or {}).get(leg.id, leg.p0)
        p *= (1 - pw)
        p_streak.append(p)
    breach = None
    if bot.stop_loss:
        for j, c in enumerate(cumulative, start=1):
            if c >= float(bot.stop_loss):
                breach = j
                break
    return Ladder(stakes, cumulative, p_streak, breach, resets)


def one_in(p: float) -> str:
    if p != p or p <= 0:  # NaN or zero
        return "n/a"
    return f"1 in {round(1 / p):,}"


def risk_lines(bot: BotSpec, streak: int = 6) -> list[str]:
    lad = ladder(bot, max(streak + 2, 12))
    lines = []
    p = lad.p_streak[streak - 1] if streak - 1 < len(lad.p_streak) else float("nan")
    lines.append(f"{streak} losses in a row ≈ {one_in(p)} sequences (theoretical)")
    if bot.sequence.get("overflow") == "error_reset" and streak >= len(bot.steps):
        lines.append(f"after {len(bot.steps)} losses the prediction list runs out (DBot error)")
    if streak < len(lad.stakes):
        lines.append(f"stake on loss {streak + 1}: {lad.stakes[streak]:,.2f} · lost after {streak}: "
                     f"{lad.cumulative[streak - 1]:,.2f}")
    if bot.stop_loss:
        if lad.stop_loss_breach:
            lines.append(f"stop-loss {bot.stop_loss:g} breached at loss #{lad.stop_loss_breach} "
                         f"(−{lad.cumulative[lad.stop_loss_breach - 1]:,.2f})")
        else:
            lines.append(f"stop-loss {bot.stop_loss:g} not reached within {len(lad.stakes)} losses")
    else:
        lines.append("no working stop-loss in the original bot")
    if lad.resets:
        lines.append(f"ladder resets after loss #{lad.resets[0]} (single-loss limit)")
    return lines

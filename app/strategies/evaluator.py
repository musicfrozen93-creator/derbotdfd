"""Replay engine: score each bot leg's exact entry trigger on stored ticks.

Tick alignment (verified against live ``proposal`` responses, 2026-11-16): a tick
contract bought while tick *i* is the latest tick settles on tick *i + duration*
(1-tick contract on 1HZ10V: date_start = spot_time, date_expiry = next tick).
Because purchase latency could push settlement one tick later, the validator can
also require the edge to survive at *i + duration + 1* ("latency robustness").

Events whose look-back window or settlement tick crosses a data gap are dropped.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

import numpy as np

from app.analytics import statistics as st
from app.strategies.loader import BotSpec, LegSpec
from app.strategies.triggers import compute_trigger


@dataclass
class Series:
    symbol: str
    epochs: np.ndarray
    digits: np.ndarray
    prices: np.ndarray

    @property
    def n(self) -> int:
        return int(self.epochs.shape[0])

    def nominal_interval(self) -> float:
        if self.n < 3:
            return 1.0
        d = np.diff(self.epochs[-5000:])
        d = d[d > 0]
        return float(np.median(d)) if d.size else 1.0

    def segments(self, gap_threshold: float) -> np.ndarray:
        """Segment id per tick; a new segment starts after every gap."""
        if self.n == 0:
            return np.zeros(0, dtype=np.int64)
        gaps = np.diff(self.epochs) > gap_threshold
        return np.concatenate(([0], np.cumsum(gaps))).astype(np.int64)


@dataclass
class LegStats:
    n_ticks: int = 0
    candidates: int = 0
    n_events: int = 0
    wins: int = 0
    wr: float | None = None
    ci_lo: float | None = None
    ci_hi: float | None = None
    disc_n: int = 0
    disc_k: int = 0
    disc_wr: float | None = None
    disc_lo: float | None = None
    oos_n: int = 0
    oos_k: int = 0
    oos_wr: float | None = None
    recent_n: int = 0
    recent_k: int = 0
    prior_n: int = 0
    prior_k: int = 0
    stability_p: float | None = None
    runs_z: float | None = None
    runs_p: float | None = None
    longest_loss_run: int = 0
    longest_run_p: float | None = None
    lat_n: int = 0
    lat_k: int = 0
    lat_lo: float | None = None
    comp_n: int = 0
    comp_k: int = 0
    comp_wr: float | None = None
    # filled by validation once the payout is known
    p0: float | None = None
    p_be: float | None = None
    disc_p: float | None = None
    oos_p: float | None = None
    q_value: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _rate(k: int, n: int) -> float | None:
    return (k / n) if n else None


def leg_events(series: Series, seg: np.ndarray, leg: LegSpec, *, extra_offset: int = 0,
               trigger_cache: dict | None = None):
    """Return (event_indices, wins, lookback, trigger_mask) for one leg."""
    key = None
    if trigger_cache is not None:
        key = json.dumps(leg.trigger, sort_keys=True)
    if key is not None and key in trigger_cache:
        mask, lookback = trigger_cache[key]
    else:
        mask, lookback = compute_trigger(leg.trigger, series.digits, series.prices)
        if key is not None:
            trigger_cache[key] = (mask, lookback)
    s = leg.duration_ticks + extra_offset
    n = series.n
    if n <= s + lookback:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=bool), lookback, mask
    cand = np.arange(lookback, n - s, dtype=np.int64)
    ok = mask[cand] & (seg[cand - lookback] == seg[cand + s])
    ev = cand[ok]
    wins = leg.win_table[series.digits[ev + s]]
    return ev, wins, lookback, mask


def compute_leg_stats(series: Series, seg: np.ndarray, leg: LegSpec, cfg, *,
                      trigger_cache: dict | None = None, latency_check: bool = True) -> tuple[LegStats, np.ndarray, np.ndarray]:
    """Compute replay statistics for one leg on one symbol.

    Returns (stats, event_indices, wins) — the arrays feed the bot simulation.
    """
    conf = float(cfg.confidence)
    ls = LegStats(n_ticks=series.n, p0=leg.p0)
    ev, wins, lookback, mask = leg_events(series, seg, leg, trigger_cache=trigger_cache)
    s = leg.duration_ticks
    n = series.n
    ls.candidates = max(0, n - s - lookback)
    ls.n_events = int(ev.size)
    if ev.size == 0:
        return ls, ev, wins
    ls.wins = int(wins.sum())
    ls.wr = ls.wins / ls.n_events
    ls.ci_lo, ls.ci_hi = st.wilson_interval(ls.wins, ls.n_events, conf)

    # walk-forward split: discovery = older block, confirmation = later block (non-overlapping)
    split = int(n * (1 - float(cfg.oos_fraction)))
    disc = (ev + s) < split
    oos = (ev - lookback) >= split
    ls.disc_n = int(disc.sum())
    ls.disc_k = int(wins[disc].sum())
    ls.disc_wr = _rate(ls.disc_k, ls.disc_n)
    if ls.disc_n:
        ls.disc_lo = st.wilson_interval(ls.disc_k, ls.disc_n, conf)[0]
    ls.oos_n = int(oos.sum())
    ls.oos_k = int(wins[oos].sum())
    ls.oos_wr = _rate(ls.oos_k, ls.oos_n)

    # stability: most recent events vs the block of events just before them
    r, p = int(cfg.stability_recent), int(cfg.stability_prior)
    if ev.size >= r + p:
        recent = wins[-r:]
        prior = wins[-(r + p):-r]
        ls.recent_n, ls.recent_k = r, int(recent.sum())
        ls.prior_n, ls.prior_k = p, int(prior.sum())
        ls.stability_p = st.two_proportion_z(ls.recent_k, r, ls.prior_k, p)[1]

    # independence of the outcome sequence (losing streaks)
    ls.runs_z, ls.runs_p = st.runs_test(wins)
    ls.longest_loss_run = st.longest_run(wins, value=False)
    q = 1 - ls.wr
    ls.longest_run_p = st.p_longest_run_at_least(ls.n_events, q, ls.longest_loss_run)

    # latency robustness: same trigger, settled one tick later
    if latency_check:
        ev2, wins2, _, _ = leg_events(series, seg, leg, extra_offset=1, trigger_cache=trigger_cache)
        d2 = (ev2 + s + 1) < split
        ls.lat_n = int(d2.sum())
        ls.lat_k = int(wins2[d2].sum())
        if ls.lat_n:
            ls.lat_lo = st.wilson_interval(ls.lat_k, ls.lat_n, conf)[0]

    # comparator for filtered legs (report: Predator SMA-true vs SMA-false, Delta/Crysis vs unconditional)
    if leg.compare_to in ("complement", "unconditional"):
        cand = np.arange(lookback, n - s, dtype=np.int64)
        valid = seg[cand - lookback] == seg[cand + s]
        cand = cand[valid & ((cand + s) < split)]
        if leg.compare_to == "complement":
            cand = cand[~mask[cand]]
        cw = leg.win_table[series.digits[cand + s]]
        ls.comp_n = int(cw.size)
        ls.comp_k = int(cw.sum())
        ls.comp_wr = _rate(ls.comp_k, ls.comp_n)
    return ls, ev, wins


# ------------------------------------------------------------------ simulation
@dataclass
class SimResult:
    trades: int = 0
    wins: int = 0
    longest_losing_streak: int = 0
    streak_counts: dict = field(default_factory=dict)   # length -> occurrences
    leg_trades: dict = field(default_factory=dict)
    leg_wins: dict = field(default_factory=dict)
    ladder_overflows: int = 0
    ticks: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def simulate_bot(bot: BotSpec, leg_data: dict[str, tuple[np.ndarray, np.ndarray]], start_index: int,
                 next_trade_gap: int = 1) -> SimResult:
    """Sequential replay of the bot's state machine (one open contract at a time).

    ``leg_data[leg_id] = (event_indices, wins)`` from :func:`compute_leg_stats`.
    After a contract settles on tick ``i + d`` the next purchase can trigger on
    tick ``i + d + next_trade_gap`` at the earliest.
    """
    steps = bot.steps
    overflow = bot.sequence.get("overflow", "hold_last")
    on_win = bot.sequence.get("on_win", "reset")
    res = SimResult()
    cursor = start_index
    step = 0
    streak = 0
    while True:
        if step >= len(steps):
            if overflow == "error_reset":
                res.ladder_overflows += 1
                step = 0
            else:
                step = len(steps) - 1
        leg = steps[step]
        ev, wins = leg_data[leg.id]
        pos = int(np.searchsorted(ev, cursor))
        if pos >= ev.size:
            break
        i = int(ev[pos])
        win = bool(wins[pos])
        res.trades += 1
        res.leg_trades[leg.id] = res.leg_trades.get(leg.id, 0) + 1
        if win:
            res.wins += 1
            res.leg_wins[leg.id] = res.leg_wins.get(leg.id, 0) + 1
            if streak:
                res.streak_counts[streak] = res.streak_counts.get(streak, 0) + 1
            streak = 0
            step = 0 if on_win == "reset" else step
        else:
            streak += 1
            res.longest_losing_streak = max(res.longest_losing_streak, streak)
            step += 1
        cursor = i + leg.duration_ticks + next_trade_gap
    if streak:
        res.streak_counts[streak] = res.streak_counts.get(streak, 0) + 1
    return res

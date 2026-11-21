"""Contract outcomes of a bot's own entry ticks, and their statistics.

A contract bought on trigger tick ``t`` enters on tick ``t + gap`` (``gap`` = 1: the first tick after
the purchase) and an N-tick digit contract settles on the last digit of tick ``t + gap + N - 1``.
Only settlements that are already in the data are counted (no look-ahead). A contract whose ticks
span a feed gap is skipped: the stored next tick is then not the tick the contract would have
used.

Statistics use Wilson score intervals. Overlapping windows from consecutive evaluation cycles share
most of their data, so successive results are NOT independent evidence; confirmation over several
cycles is a debounce, not a statistical test.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from statistics import NormalDist

import numpy as np


def z_two_sided(confidence: float) -> float:
    return NormalDist().inv_cdf(1 - (1 - confidence) / 2)


def z_one_sided(confidence: float) -> float:
    return NormalDist().inv_cdf(confidence)


def wilson(wins: int, n: int, z: float) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - h) / d), min(1.0, (c + h) / d)


def gap_ok_steps(epochs: np.ndarray, max_step_s: float) -> np.ndarray:
    """ok[i] is True when ticks i and i+1 are consecutive (no feed gap between them)."""
    e = np.asarray(epochs, dtype=np.int64)
    if e.shape[0] < 2:
        return np.zeros(0, dtype=bool)
    return np.diff(e) <= max_step_s


def settle(mask: np.ndarray, digits: np.ndarray, gap_ok: np.ndarray, duration: int, gap: int,
           win_digits: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For every trigger tick: (trigger_index, settle_index, win) of the contracts that have settled
    within the data and whose ticks span no feed gap."""
    d = np.asarray(digits, dtype=np.int16)
    n = d.shape[0]
    idx = np.nonzero(mask)[0]
    s = idx + gap + duration - 1
    keep = s < n
    idx, s = idx[keep], s[keep]
    if gap_ok.shape[0]:
        bad = np.concatenate(([0], np.cumsum(~gap_ok)))   # bad steps before index i
        clean = (bad[s] - bad[idx]) == 0
        idx, s = idx[clean], s[clean]
    table = np.zeros(10, dtype=bool)
    table[list(win_digits)] = True
    return idx, s, table[d[s]]


@dataclass
class WindowStats:
    window_ticks: int                 # ticks in the window
    triggers: int                     # entry ticks in the window
    settled: int                      # their contracts that settled inside the window (no gap)
    wins: int
    win_rate: float | None
    ci_low: float | None              # two-sided Wilson interval at the configured confidence
    ci_high: float | None
    p_fair: float
    z_vs_fair: float | None
    trigger_rate: float | None        # triggers / evaluable ticks

    def to_dict(self) -> dict:
        return asdict(self)


def window_stats(trig_idx: np.ndarray, settle_idx: np.ndarray, wins: np.ndarray, start: int, end: int,
                 mask: np.ndarray, first_evaluable: int, p_fair: float, z2: float) -> WindowStats:
    """Statistics for triggers in [start, end) whose settlement index is also < end."""
    start = max(start, 0)
    sel = (trig_idx >= start) & (trig_idx < end) & (settle_idx < end)
    n = int(sel.sum())
    w = int(wins[sel].sum())
    lo, hi = wilson(w, n, z2)
    rate = (w / n) if n else None
    z = ((rate - p_fair) / math.sqrt(p_fair * (1 - p_fair) / n)) if n and 0 < p_fair < 1 else None
    ev_start = max(start, first_evaluable)
    evaluable = max(0, end - ev_start)
    trig = int(mask[ev_start:end].sum()) if evaluable else 0
    return WindowStats(end - start, trig, n, w, rate, lo, hi, p_fair, z, (trig / evaluable) if evaluable else None)

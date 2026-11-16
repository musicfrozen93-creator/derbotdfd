"""Statistical primitives used by the validation engine."""
from __future__ import annotations

import math

import numpy as np
from scipy import stats


def z_for(confidence: float) -> float:
    return float(stats.norm.ppf(1 - (1 - confidence) / 2))


def wilson_interval(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    z = z_for(confidence)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def margin_of_error(p: float, n: int, confidence: float = 0.95) -> float:
    if n <= 0:
        return float("nan")
    return z_for(confidence) * math.sqrt(p * (1 - p) / n)


def binom_p_greater(k: int, n: int, p: float) -> float:
    """One-sided exact binomial p-value for H1: true rate > p  (P[X >= k])."""
    if n <= 0:
        return 1.0
    return float(stats.binom.sf(k - 1, n, p))


def binom_p_less(k: int, n: int, p: float) -> float:
    """One-sided exact binomial p-value for H1: true rate < p  (P[X <= k])."""
    if n <= 0:
        return 1.0
    return float(stats.binom.cdf(k, n, p))


def chi2_uniform(digits: np.ndarray) -> tuple[float, float, np.ndarray]:
    """Chi-square goodness-of-fit of digits 0-9 against uniform (9 d.f.)."""
    counts = np.bincount(np.asarray(digits, dtype=np.int64), minlength=10)[:10]
    n = counts.sum()
    if n == 0:
        return float("nan"), 1.0, counts
    res = stats.chisquare(counts)
    return float(res.statistic), float(res.pvalue), counts


def benjamini_hochberg(pvalues: list[float]) -> np.ndarray:
    """Benjamini–Hochberg adjusted q-values (same order as input)."""
    p = np.asarray(pvalues, dtype=float)
    m = p.size
    if m == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(m)
    out[order] = np.minimum(q, 1.0)
    return out


def runs_test(seq: np.ndarray) -> tuple[float, float]:
    """Wald–Wolfowitz runs test on a boolean sequence.

    Returns (z, p_clustered): p_clustered is the one-sided p-value for *fewer*
    runs than independence predicts, i.e. wins/losses bunching into long runs.
    """
    s = np.asarray(seq, dtype=bool)
    n = s.size
    n1 = int(s.sum())
    n2 = n - n1
    if n1 == 0 or n2 == 0 or n < 3:
        return 0.0, 1.0
    runs = 1 + int(np.count_nonzero(s[1:] != s[:-1]))
    mu = 2.0 * n1 * n2 / n + 1
    var = 2.0 * n1 * n2 * (2.0 * n1 * n2 - n) / (n * n * (n - 1))
    if var <= 0:
        return 0.0, 1.0
    z = (runs - mu) / math.sqrt(var)
    return float(z), float(stats.norm.cdf(z))


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """Two-sided two-proportion z-test. Returns (z, p)."""
    if n1 <= 0 or n2 <= 0:
        return 0.0, 1.0
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 0.0, 1.0
    z = (k1 / n1 - k2 / n2) / se
    return float(z), float(2 * stats.norm.sf(abs(z)))


def longest_run(seq: np.ndarray, value: bool = False) -> int:
    s = np.asarray(seq, dtype=bool) == value
    if s.size == 0 or not s.any():
        return 0
    padded = np.concatenate(([False], s, [False])).astype(np.int8)
    d = np.diff(padded)
    starts = np.nonzero(d == 1)[0]
    ends = np.nonzero(d == -1)[0]
    return int((ends - starts).max())


def run_length_counts(seq: np.ndarray, value: bool = False) -> dict[int, int]:
    s = np.asarray(seq, dtype=bool) == value
    if s.size == 0 or not s.any():
        return {}
    padded = np.concatenate(([False], s, [False])).astype(np.int8)
    d = np.diff(padded)
    lengths = np.nonzero(d == -1)[0] - np.nonzero(d == 1)[0]
    vals, cnts = np.unique(lengths, return_counts=True)
    return {int(v): int(c) for v, c in zip(vals, cnts)}


def p_longest_run_at_least(n: int, q: float, length: int) -> float:
    """Approximate P(longest run of an event with per-trial prob q is >= length)
    in n independent trials (Poisson approximation of run starts)."""
    if length <= 0 or n <= 0:
        return 1.0
    if q <= 0:
        return 0.0
    if q >= 1:
        return 1.0
    lam = n * (1 - q) * q ** length + q ** length   # + a run starting at trial 1
    return float(1 - math.exp(-lam))

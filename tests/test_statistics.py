"""7-8. Statistical calculations and break-even."""
from decimal import Decimal

import numpy as np
import pytest

from app.analytics import statistics as st
from app.analytics.payout import break_even
from app.analytics.risk import ladder, stake_sequence


def test_wilson_known_values():
    lo, hi = st.wilson_interval(50, 100)
    assert lo == pytest.approx(0.4038, abs=1e-4) and hi == pytest.approx(0.5962, abs=1e-4)
    lo, hi = st.wilson_interval(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)


def test_margin_of_error_matches_audit_table():
    assert 100 * st.margin_of_error(0.5, 100) == pytest.approx(9.8, abs=0.05)
    assert 100 * st.margin_of_error(0.9, 20000) == pytest.approx(0.4, abs=0.05)


def test_binomial_tests():
    assert st.binom_p_greater(60, 100, 0.5) == pytest.approx(0.0284, abs=1e-3)
    assert st.binom_p_less(40, 100, 0.5) == pytest.approx(0.0284, abs=1e-3)
    assert st.binom_p_greater(0, 0, 0.5) == 1.0


def test_chi2_uniform():
    stat, p, counts = st.chi2_uniform(np.repeat(np.arange(10), 100))
    assert stat == 0 and p == pytest.approx(1.0)
    _, p2, _ = st.chi2_uniform(np.array([9] * 500 + list(range(10)) * 50))
    assert p2 < 1e-10


def test_benjamini_hochberg():
    q = st.benjamini_hochberg([0.01, 0.04, 0.03, 0.005])
    assert q.tolist() == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert st.benjamini_hochberg([]).size == 0


def test_runs_test():
    alt = np.array([True, False] * 200)
    z, p = st.runs_test(alt)
    assert z > 5 and p > 0.99                      # far more runs than chance: not clustered
    blocks = np.array([True] * 200 + [False] * 200)
    z, p = st.runs_test(blocks)
    assert p < 1e-10                               # strongly clustered


def test_two_proportion_and_runs():
    z, p = st.two_proportion_z(50, 100, 50, 100)
    assert z == 0 and p == pytest.approx(1.0)
    assert st.longest_run(np.array([1, 0, 0, 0, 1, 0], bool), value=False) == 3
    assert st.run_length_counts(np.array([1, 0, 0, 1, 0], bool)) == {1: 1, 2: 1}
    assert 0 < st.p_longest_run_at_least(1000, 0.5, 10) < 1


def test_break_even_from_live_quote():
    # live public quote 2026-11-16: DIGITOVER 3, 1 tick, stake 10.00 -> payout 16.34
    assert break_even(Decimal("10.00"), Decimal("16.34")) == pytest.approx(0.61199, abs=1e-5)
    assert break_even(Decimal("1"), Decimal("1.09")) == pytest.approx(0.917, abs=1e-3)
    with pytest.raises(ValueError):
        break_even(Decimal("1"), Decimal("0"))


def test_stake_ladders_match_audit(bots):
    assert stake_sequence(bots["aria"].stake, 7)[0][-1] == pytest.approx(85.77, abs=0.01)
    lad = ladder(bots["aria"])
    assert lad.cumulative[7] == pytest.approx(342.94, abs=0.01) and lad.stop_loss_breach == 8
    assert ladder(bots["aria_v2"]).stop_loss_breach == 6
    assert ladder(bots["navix"]).cumulative[5] == pytest.approx(440.94, abs=0.01)
    assert ladder(bots["navix"]).stakes[6] == pytest.approx(1102.96, abs=0.01)
    assert ladder(bots["predator"]).cumulative[7] == pytest.approx(3280)
    assert ladder(bots["crysis"]).stakes[3] == pytest.approx(6.30)
    assert ladder(bots["crysis"]).cumulative[3] == pytest.approx(9.45)
    assert ladder(bots["delta"]).stop_loss_breach == 6
    assert ladder(bots["voltrix"]).cumulative[5] == pytest.approx(2.33, abs=0.01)
    assert ladder(bots["predator_v2"]).resets == [10]
    assert ladder(bots["pulse2"]).cumulative[5] == pytest.approx(52.30, abs=0.01)
    assert 1 / ladder(bots["aria"]).p_streak[5] == pytest.approx(320)
    assert round(1 / ladder(bots["zony_ai"]).p_streak[5]) == 66
    assert round(1 / ladder(bots["navix"]).p_streak[5]) == 2058
    assert round(1 / ladder(bots["predator_v2"]).p_streak[5]) == 21

"""Strategy model vs the XML logic: ladders, entry rules, settlement timing and the YAML cross-check.

Expected values were derived by hand from the refactored XML files and checked independently with a
JavaScript re-implementation and live Deriv quotes (COMPLETE_BOT_ANALYSIS, 2026-11-20).
"""
import numpy as np
import pytest

from app.strategy_model.bots import BOTS, StrategyModelError, check_against_yaml, load_models, win_digits
from app.strategy_model.entry import entry_mask, prices_as_doubles
from app.strategy_model.jsnum import to_fixed2
from app.strategy_model.outcomes import gap_ok_steps, settle, wilson, window_stats, z_two_sided


def amounts(ladder):
    return [s.amount for s in ladder.steps]


# ------------------------------------------------------------------ JS rounding
def test_js_to_fixed_matches_dbot():
    assert to_fixed2(0.6 * 3.5 * 3.5 * 3.5) == 25.73      # Node: +(25.725000000000001).toFixed(2)
    assert to_fixed2(9.261) == 9.26
    assert to_fixed2(3.125) == 3.13                       # exact tie -> away from zero
    assert to_fixed2(-1.125) == -1.13                     # JS: (-1.125).toFixed(2) === "-1.13"
    assert to_fixed2(1.5375) == 1.54


# ------------------------------------------------------------------ ladders (all 10 bots)
def test_aria_ladder_fresh_and_near_take_profit():
    fresh, near = BOTS["aria"].ladders()
    assert amounts(fresh) == [1.0, 2.1, 4.41, 9.26, 19.45, 40.84, 85.76, 180.1]
    assert [s.barrier for s in fresh.steps] == [9, 5, 5, 5, 5, 5, 5, 5]
    assert fresh.xml_stop_kind == "stop_loss" and fresh.xml_stop_after == 8
    assert round(sum(amounts(fresh)), 2) == 342.92
    # a run already near its +200 take-profit survives the 8th loss: one more trade (378.21) before the SL
    assert near.start_total == 199.99 and near.xml_stop_after == 9 and amounts(near)[-1] == 378.21


def test_aria_v2_ladder():
    fresh, near = BOTS["aria_v2"].ladders()
    assert amounts(fresh) == [1.0, 2.1, 4.41, 9.26, 19.45, 40.84] and fresh.xml_stop_after == 6
    assert round(sum(amounts(fresh)), 2) == 77.06
    assert near.xml_stop_after == 6


def test_crysis_ladders_include_initial_y_two():
    first, later = BOTS["crysis"].ladders()
    assert amounts(first)[:7] == [0.35, 4.7, 14.1, 42.3, 126.9, 380.7, 1142.1]
    assert amounts(later)[:9] == [0.35, 0.7, 2.1, 6.3, 18.9, 56.7, 170.1, 510.3, 1530.9]
    assert first.xml_stop_kind == "none" and later.xml_stop_kind == "none"
    assert all(s.duration == 5 and s.barrier == 3 for s in first.steps)


def test_delta_ladder():
    fresh, _ = BOTS["delta"].ladders()
    assert amounts(fresh) == [0.5, 1.25, 3.13, 7.82, 19.55, 48.88]
    assert fresh.xml_stop_after == 6 and round(sum(amounts(fresh)), 2) == 81.13
    assert all(s.duration == 2 for s in fresh.steps)


def test_navix_ladder_switches_contract():
    (fresh,) = BOTS["navix"].ladders()
    assert amounts(fresh)[:7] == [0.6, 2.1, 7.35, 25.73, 90.04, 315.13, 1102.96]
    assert [(s.contract_type, s.barrier) for s in fresh.steps[:3]] == [("DIGITUNDER", 8), ("DIGITOVER", 2),
                                                                       ("DIGITOVER", 2)]
    assert fresh.xml_stop_kind == "none"


def test_predator_ladders():
    (p1,) = BOTS["predator"].ladders()
    assert amounts(p1)[:7] == [1, 3, 9, 27, 81, 243, 729]
    (p2,) = BOTS["predator_v2"].ladders()
    a = amounts(p2)
    assert a[:11] == [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2]   # the XML resets after a single loss >= 1000


def test_pulse2_ladder():
    fresh, _ = BOTS["pulse2"].ladders()
    assert amounts(fresh)[:11] == [0.75, 1.54, 3.15, 6.46, 13.25, 27.16, 55.68, 114.14, 233.99, 479.68, 983.34]
    assert fresh.xml_stop_after == 12          # the 3,000 max loss is reached only after the 12th loss


def test_voltrix_ladder():
    fresh, near = BOTS["voltrix"].ladders()
    a = amounts(fresh)
    assert a[:7] == [0.37, 0.37, 0.37, 0.37, 0.37, 0.48, 0.63]
    assert fresh.xml_stop_after == 27 and a[26] == 119.02 and round(sum(a), 2) == 516.01
    assert all(s.duration == 3 and s.barrier == 7 for s in fresh.steps)
    assert near.xml_stop_after == 27


def test_zony_ladder_and_list_end():
    (fresh,) = BOTS["zony_ai"].ladders()
    assert [s.barrier for s in fresh.steps] == [0, 4, 5, 6, 7, 8] and amounts(fresh) == [1.0] * 6
    assert fresh.xml_stop_kind == "list_end" and fresh.xml_stop_after == 6


def test_base_stake_override_changes_ladder():
    (p,) = BOTS["predator"].ladders(base_stake=10)
    assert amounts(p)[:5] == [10, 30, 90, 270, 810]
    fresh, _ = BOTS["delta"].ladders(base_stake=2)
    assert fresh.xml_stop_after == 4                 # SL 50 is absolute: reached after fewer losses


# ------------------------------------------------------------------ entry rules
def test_last_digit_rules():
    d = np.array([1, 2, 1, 4, 6, 0, 3, 9, 5])
    assert entry_mask(BOTS["delta"].leg("main").entry, d)[0].tolist() == [True, False, True, False, False, False,
                                                                          False, False, False]
    assert entry_mask(BOTS["navix"].leg("base").entry, d)[0].tolist() == [x == 4 for x in d]
    assert entry_mask(BOTS["navix"].leg("recovery").entry, d)[0].tolist() == [x == 6 for x in d]
    assert entry_mask(BOTS["pulse2"].leg("main").entry, d)[0].tolist() == [x in (0, 3, 6, 9) for x in d]
    m, first = entry_mask(BOTS["aria"].leg("base").entry, d)
    assert m.all() and first == 0


def test_crysis_pattern_matches_xml():
    rule = BOTS["crysis"].leg("main").entry
    # window 2,0,3,8,1,6,7 -> X = 4 (2,0,3,1) and X1 = 7 -> purchase
    m, first = entry_mask(rule, np.array([2, 0, 3, 8, 1, 6, 7]))
    assert first == 6 and m.tolist() == [False] * 6 + [True]
    # X = 3 -> no purchase
    assert not entry_mask(rule, np.array([2, 0, 3, 8, 5, 6, 7]))[0][-1]
    # X1 = 4 -> no purchase (needs X1 > 4)
    assert not entry_mask(rule, np.array([0, 0, 0, 0, 8, 6, 4]))[0][-1]


def test_predator_sma_uses_dbot_float_arithmetic():
    rule = BOTS["predator"].leg("main").entry
    rising = prices_as_doubles(np.arange(1000, 1010) * 100, 2)
    assert entry_mask(rule, np.zeros(10), rising)[0][7:].all()
    falling = rising[::-1].copy()
    assert not entry_mask(rule, np.zeros(10), falling)[0].any()
    # equal prices: the JS sum (0.1 added 8 times) / 8 is 0.09999999999999999 < 0.1 -> NOT tick < MA -> purchase
    flat = np.array([0.1] * 8)
    assert entry_mask(rule, np.zeros(8), flat)[0].tolist() == [False] * 7 + [True]
    s = 0.0
    for x in flat:
        s += x
    assert not (0.1 < s / 8)                 # same as Node: 0.1 < 0.09999999999999999 === false


# ------------------------------------------------------------------ settlement timing and statistics
def test_settlement_uses_duration_and_skips_gaps():
    digits = np.array([1, 9, 0, 9, 1, 2, 7, 1, 9, 9])
    epochs = np.array([0, 2, 4, 6, 8, 10, 12, 14, 40, 42])          # gap between index 7 and 8
    mask = digits == 1
    ok = gap_ok_steps(epochs, 20)
    idx, s, w = settle(mask, digits, ok, duration=2, gap=1, win_digits=win_digits("DIGITOVER", 3))
    # trigger 0 -> settles on index 2 (digit 0: loss); trigger 4 -> index 6 (digit 7: win);
    # trigger 7 -> index 9 crosses the gap -> skipped
    assert idx.tolist() == [0, 4] and s.tolist() == [2, 6] and w.tolist() == [False, True]


def test_window_stats_has_no_look_ahead():
    digits = np.array([1, 5, 1, 5, 1])
    mask = digits == 1
    idx, s, w = settle(mask, digits, gap_ok_steps(np.arange(5), 10), 1, 1, win_digits("DIGITOVER", 3))
    st = window_stats(idx, s, w, 0, 5, mask, 0, 0.6, z_two_sided(0.99))
    assert st.triggers == 3 and st.settled == 2 and st.wins == 2     # the last trigger has not settled yet


def test_wilson_interval():
    lo, hi = wilson(60, 100, 1.959964)
    assert 0.50 < lo < 0.51 and 0.69 < hi < 0.70
    assert wilson(0, 0, 1.96) == (None, None)


# ------------------------------------------------------------------ YAML cross-check
def test_models_match_strategy_yaml(bots):
    models = load_models(list(bots.values()))
    assert set(models) == set(bots) == set(BOTS)
    for key, spec in bots.items():
        assert check_against_yaml(models[key], spec) == []


def test_yaml_mismatch_is_refused(bots):
    import dataclasses
    spec = bots["delta"]
    bad_leg = dataclasses.replace(spec.legs[0], duration_ticks=1)
    bad = dataclasses.replace(spec, legs=[bad_leg])
    with pytest.raises(StrategyModelError):
        load_models([bad])

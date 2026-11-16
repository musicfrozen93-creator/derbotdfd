"""4-6. Every bot's entry conditions, contract duration and tick alignment."""
import numpy as np
import pytest

from app.strategies.evaluator import Series, compute_leg_stats, leg_events, simulate_bot
from app.strategies.loader import win_digits
from app.strategies.triggers import compute_trigger


def series(digits, prices=None, epochs=None):
    d = np.asarray(digits, dtype=np.int8)
    n = d.size
    p = np.asarray(prices if prices is not None else np.arange(n) * 10 + d, dtype=np.int64)
    e = np.asarray(epochs if epochs is not None else np.arange(n) + 1_000_000, dtype=np.int64)
    return Series("TEST", e, d, p)


def mask(bot, leg_id, s):
    m, lb = compute_trigger(bot.leg(leg_id).trigger, s.digits, s.prices)
    return m, lb


def test_inventory_counts(bots):
    assert len(bots) == 10
    assert sum(len(b.legs) for b in bots.values()) == 18
    pairs = {(l.contract_type, l.barrier) for b in bots.values() for l in b.legs}
    assert len(pairs) == 12          # audit section 4: twelve contract/prediction pairs


@pytest.mark.parametrize("key,leg,ctype,barrier,dur", [
    ("aria", "base", "DIGITUNDER", 9, 1), ("aria", "recovery", "DIGITUNDER", 5, 1),
    ("aria_v2", "base", "DIGITUNDER", 9, 1), ("aria_v2", "recovery", "DIGITUNDER", 5, 1),
    ("predator", "main", "DIGITOVER", 3, 1), ("predator_v2", "main", "DIGITOVER", 5, 1),
    ("voltrix", "main", "DIGITOVER", 7, 3), ("delta", "main", "DIGITOVER", 3, 2),
    ("crysis", "main", "DIGITOVER", 3, 5), ("navix", "base", "DIGITUNDER", 8, 1),
    ("navix", "recovery", "DIGITOVER", 2, 1), ("zony_ai", "step1", "DIGITOVER", 0, 1),
    ("zony_ai", "step6", "DIGITOVER", 8, 1), ("pulse2", "main", "DIGITEVEN", None, 1),
])
def test_contract_spec_matches_xml(bots, key, leg, ctype, barrier, dur):
    l = bots[key].leg(leg)
    assert (l.contract_type, l.barrier, l.duration_ticks) == (ctype, barrier, dur)


def test_original_symbols(bots):
    assert {k: b.original["symbol"] for k, b in bots.items()} == {
        "aria": "1HZ10V", "aria_v2": "1HZ10V", "predator": "1HZ10V", "predator_v2": "R_100", "voltrix": "R_100",
        "delta": "R_10", "crysis": "R_10", "navix": "R_100", "zony_ai": "1HZ50V", "pulse2": "R_100"}


def test_win_digits_settlement_rules():
    assert win_digits("DIGITOVER", 3) == [4, 5, 6, 7, 8, 9]
    assert win_digits("DIGITUNDER", 9) == list(range(9))
    assert 3 not in win_digits("DIGITOVER", 3) and 3 not in win_digits("DIGITUNDER", 3)   # equal digit loses
    assert win_digits("DIGITEVEN", None) == [0, 2, 4, 6, 8]


def test_unfiltered_bots_buy_every_tick(bots):
    s = series([0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    for key, leg in [("aria", "base"), ("aria", "recovery"), ("aria_v2", "base"), ("predator_v2", "main"),
                     ("voltrix", "main"), ("zony_ai", "step3")]:
        m, lb = mask(bots[key], leg, s)
        assert m.all() and lb == 0


def test_delta_last_digit_1(bots):
    s = series([1, 2, 1, 0, 11 % 10, 9])
    m, _ = mask(bots["delta"], "main", s)
    assert m.tolist() == [True, False, True, False, True, False]


def test_navix_base_4_recovery_6(bots):
    s = series([4, 6, 5, 4, 6])
    assert mask(bots["navix"], "base", s)[0].tolist() == [True, False, False, True, False]
    assert mask(bots["navix"], "recovery", s)[0].tolist() == [False, True, False, False, True]


def test_pulse2_digits(bots):
    s = series(list(range(10)))
    assert np.nonzero(mask(bots["pulse2"], "main", s)[0])[0].tolist() == [0, 3, 6, 9]


def test_crysis_window_rule(bots):
    bot = bots["crysis"]
    # last 7 digits: 0,1,2,9,9,3,7 -> 4 low digits (0,1,2,3) and last digit 7 >= 5 -> buy
    s = series([0, 1, 2, 9, 9, 3, 7])
    m, lb = mask(bot, "main", s)
    assert lb == 6 and m.tolist() == [False] * 6 + [True]
    # only 3 low digits -> no buy
    assert not mask(bot, "main", series([0, 1, 9, 9, 9, 3, 7]))[0][-1]
    # last digit 4 is not > 4 -> no buy
    assert not mask(bot, "main", series([0, 1, 2, 3, 9, 9, 4]))[0][-1]
    # last digit 5 -> buy (X1 > 4)
    assert mask(bot, "main", series([0, 1, 2, 3, 9, 9, 5]))[0][-1]
    # first 6 ticks never trigger (DBot's null-history bug is not replayed)
    assert not mask(bot, "main", series([0, 0, 0, 0, 5, 5]))[0].any()


def test_predator_sma_rule_exact(bots):
    bot = bots["predator"]
    prices = [100, 100, 100, 100, 100, 100, 100, 100, 101, 99, 100]
    s = series([0] * len(prices), prices=prices)
    m, lb = mask(bot, "main", s)
    assert lb == 7
    # index 7: price == SMA -> NOT (tick < MA) -> buy ; index 8: above -> buy ; index 9: below -> no buy
    assert m[:7].tolist() == [False] * 7
    assert m[7] and m[8] and not m[9]
    # index 10: window [100x5,101,99,100] mean 100 == price -> buy
    assert m[10]


@pytest.mark.parametrize("key,dur", [("predator_v2", 1), ("delta", 2), ("voltrix", 3), ("crysis", 5)])
def test_settlement_tick_alignment(bots, key, dur, rng):
    """A contract triggered on tick i is settled by the digit of tick i + duration."""
    digits = rng.integers(0, 10, 500)
    s = series(digits)
    leg = bots[key].legs[0]
    seg = s.segments(10)
    ev, wins, lb, _ = leg_events(s, seg, leg)
    assert ev.size > 0
    expected = leg.win_table[digits[ev + dur]]
    assert np.array_equal(wins, expected)
    assert ev.max() + dur < s.n
    # latency-shifted scoring uses i + duration + 1
    ev2, wins2, _, _ = leg_events(s, seg, leg, extra_offset=1)
    assert np.array_equal(wins2, leg.win_table[digits[ev2 + dur + 1]])


def test_events_across_gaps_are_dropped(bots):
    digits = [1] * 20
    epochs = list(range(1000, 1010)) + list(range(2000, 2010))    # 990 s gap between index 9 and 10
    s = series(digits, epochs=epochs)
    leg = bots["delta"].legs[0]                                     # 2-tick contract
    ev, _, _, _ = leg_events(s, s.segments(10), leg)
    assert 8 not in ev and 9 not in ev                              # would settle after the gap
    assert 7 in ev and 10 in ev


def test_compute_leg_stats_split_and_comparator(bots, cfg, rng):
    digits = rng.integers(0, 10, 30000)
    s = series(digits)
    st, ev, wins = compute_leg_stats(s, s.segments(10), bots["delta"].legs[0], cfg.analysis)
    assert st.n_events == ev.size and st.wins == wins.sum()
    assert st.disc_n + st.oos_n <= st.n_events
    assert abs(st.wr - 0.6) < 0.05
    assert st.comp_n > 0 and abs(st.comp_wr - 0.6) < 0.02
    assert st.ci_lo < st.wr < st.ci_hi


def test_simulation_recovery_switch(bots):
    # Aria: base UNDER 9 loses only on 9; recovery UNDER 5.
    digits = [0, 9, 0, 7, 0, 3, 0, 0, 0]
    s = series(digits)
    bot = bots["aria"]
    seg = s.segments(10)
    data = {l.id: leg_events(s, seg, l)[:2] for l in bot.legs}
    sim = simulate_bot(bot, data, 0, next_trade_gap=1)
    # trade@0 settles d1=9 -> loss; trade@2 (recovery, UNDER 5) settles d3=7 -> loss;
    # trade@4 settles d5=3 -> win (recovery); trade@6 (base) settles d7=0 -> win
    assert sim.trades == 4 and sim.wins == 2
    assert sim.leg_trades == {"base": 2, "recovery": 2}
    assert sim.longest_losing_streak == 2


def test_simulation_zony_overflow(bots):
    digits = [0] * 40          # OVER n never wins on digit 0
    s = series(digits)
    bot = bots["zony_ai"]
    seg = s.segments(10)
    data = {l.id: leg_events(s, seg, l)[:2] for l in bot.legs}
    sim = simulate_bot(bot, data, 0)
    assert sim.wins == 0 and sim.ladder_overflows >= 1

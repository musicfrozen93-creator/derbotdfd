"""Bot inventory and contract specs as parsed from the original XML-derived strategy YAMLs."""
import pytest

from app.strategies.loader import win_digits


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

"""1-2. Last-digit extraction and decimal precision."""
from decimal import Decimal

import pytest

from app.deriv.client import loads
from app.deriv.ticks import parse_tick
from app.market.precision import (decimals_from_count, decimals_from_step, format_quote, last_digit,
                                  scaled_int)


def test_trailing_zero_preserved_from_float():
    # 1234.50 arrives as float 1234.5 in a naive parser; formatting to pip restores the 0
    assert format_quote(1234.5, 2) == "1234.50"
    assert last_digit(1234.5, 2) == 0


def test_trailing_zero_preserved_from_decimal_json():
    msg = loads('{"tick": {"symbol": "R_10", "epoch": 1, "quote": 4849.430, "pip_size": 3}}')
    q = msg["tick"]["quote"]
    assert isinstance(q, Decimal) and str(q) == "4849.430"
    t = parse_tick(msg["tick"])
    assert last_digit(t.quote, t.pip_decimals) == 0


@pytest.mark.parametrize("quote,dec,digit", [
    ("9537.28", 2, 8), ("9537.2", 2, 0), ("203041.95", 2, 5), ("4849.4", 3, 0),
    ("1.23450", 4, 5), ("1.2345", 4, 5), ("582.1", 2, 0), ("100", 2, 0), ("7.0001", 4, 1),
])
def test_last_digit_table(quote, dec, digit):
    assert last_digit(Decimal(quote), dec) == digit


def test_float_artifacts_do_not_change_digit():
    # 0.1 + 0.2 = 0.30000000000000004 in binary float; at 2 decimals the digit is 0
    assert last_digit(0.1 + 0.2, 2) == 0
    assert last_digit(9537.279999999999, 2) == 8


def test_pip_step_and_count():
    assert decimals_from_step("0.001") == 3
    assert decimals_from_step(Decimal("0.01")) == 2
    assert decimals_from_step("0.1") == 1
    assert decimals_from_step("0.0001") == 4
    assert decimals_from_step("0.00001") == 5
    assert decimals_from_count(2) == 2
    assert decimals_from_count("3.0") == 3
    with pytest.raises(ValueError):
        decimals_from_step("0")
    with pytest.raises(ValueError):
        decimals_from_count("2.5")


def test_scaled_int():
    assert scaled_int("4849.430") == 4849430
    assert scaled_int("9537.28") == 953728
    assert scaled_int("-1.50") == -150


def test_malformed_tick_rejected():
    with pytest.raises(ValueError):
        parse_tick({"symbol": "R_10", "epoch": 5})
    with pytest.raises(ValueError):
        parse_tick({"epoch": 5, "quote": 1})

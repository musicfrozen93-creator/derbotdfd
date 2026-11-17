"""break_even() helper (kept for scripts/live_api_check.py; not used by the signal rule)."""
from decimal import Decimal

import pytest

from app.analytics.payout import break_even


def test_break_even_from_quote():
    assert break_even(Decimal("10"), Decimal("10.90")) == pytest.approx(0.917431, abs=1e-6)
    with pytest.raises(ValueError):
        break_even(Decimal("10"), Decimal("0"))

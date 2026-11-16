import sys
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DEFAULTS, Section  # noqa: E402
from app.storage.database import init_db  # noqa: E402
from app.strategies.loader import load_strategies  # noqa: E402


@pytest.fixture
def cfg():
    return Section(DEFAULTS)


@pytest.fixture
def bots():
    return {b.key: b for b in load_strategies(ROOT / "strategies")}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "t.db"
    conn = init_db(path)
    yield conn, path
    conn.close()


def make_rows(symbol, digits, start_epoch=1_000_000, step=1, base="1000"):
    """(symbol, epoch, quote_text, digit) rows whose quote ends with the given digits."""
    rows = []
    for i, d in enumerate(digits):
        q = f"{base}.{(i % 10)}{int(d)}"
        rows.append((symbol, start_epoch + i * step, q, int(d)))
    return rows


class FakeHistoryClient:
    """Minimal stand-in for DerivClient answering ticks_history / contracts / proposal."""

    def __init__(self, epochs, quotes, pip=2):
        self.epochs = list(epochs)
        self.quotes = list(quotes)
        self.pip = pip
        self.connected = True
        self.calls = []
        self.subscribed = []

    async def wait_connected(self, timeout=None):
        return None

    async def subscribe_ticks(self, symbol):
        self.subscribed.append(symbol)
        return f"sub-{symbol}"

    async def forget(self, sub):
        return None

    async def request(self, payload, **kw):
        self.calls.append(payload)
        if "ticks_history" in payload:
            end = payload.get("end")
            start = payload.get("start")
            count = payload.get("count", 1000)
            idx = [i for i, e in enumerate(self.epochs)
                   if (end == "latest" or e <= end) and (start is None or e >= start)]
            idx = idx[-count:]
            return {"history": {"times": [self.epochs[i] for i in idx],
                                "prices": [Decimal(self.quotes[i]) for i in idx]},
                    "pip_size": self.pip}
        raise AssertionError(f"unexpected request {payload}")


@pytest.fixture
def rng():
    return np.random.default_rng(12345)

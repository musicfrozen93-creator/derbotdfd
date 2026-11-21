"""End to end through the real Application wiring: discovery -> quote service (fake Deriv proposals)
-> bot-specific evaluation -> SQLite -> heartbeat. Only read-only calls are ever made."""
import asyncio
import time

from app.analytics.validation import AVOID, RUN
from app.deriv.client import DerivAPIError
from app.economics import ev as E
from app.risk import engine as R
from app.storage import repositories as repo

from tests.conftest import make_rows
from tests.strategy_support import fair_p, deriv_like_payout
from tests.test_digit_rule import window
from tests.test_discovery import ALLOWED, DIGIT_SYMBOLS, FakeDeriv, app_factory  # noqa: F401
from tests.test_per_contract import ARIA_BOTH
from tests.test_signals import notes

READ_ONLY = {"active_symbols", "contracts_for", "proposal"}


class QuotingDeriv(FakeDeriv):
    """FakeDeriv that also answers ``proposal`` like Deriv (validated payout formula, 1,000 cap)."""

    def __init__(self, symbols):
        super().__init__(symbols)
        self.calls = []

    async def request(self, payload, **kw):
        self.calls.append(next(k for k in payload if k in READ_ONLY | {"buy", "sell", "authorize"}))
        if "proposal" in payload:
            assert payload["basis"] == "stake" and payload["duration_unit"] == "t"

            class K:                                   # minimal key for fair_p()
                contract_type = payload["contract_type"]
                barrier = int(payload["barrier"]) if "barrier" in payload else None
            stake = float(payload["amount"])
            if stake < 0.35:
                raise DerivAPIError("ContractBuyValidationError", "Please enter a stake amount that's at least 0.35.")
            payout = deriv_like_payout(stake, fair_p(K))
            if payout > 1000:
                raise DerivAPIError("ContractBuyValidationError",
                                    f"Minimum stake of 0.35 and maximum payout of 1000.00. Current payout is {payout:.2f}.")
            return {"proposal": {"payout": payout, "ask_price": stake}}
        return await super().request(payload, **kw)


def test_quotes_flow_into_the_evaluation_and_nothing_is_bought(app_factory, monkeypatch, rng):  # noqa: F811
    client = QuotingDeriv(DIGIT_SYMBOLS)
    app = app_factory(client)
    app.quote_service.client = client
    asyncio.run(app.discover())
    now = int(time.time())
    for sym in ALLOWED:
        repo.insert_ticks(app.conn, make_rows(sym, window(ARIA_BOTH, rng), start_epoch=now - 500))
        app.cache.mark_dirty(sym)
    monkeypatch.setattr(app.health, "status", lambda sym, t: ("OK", "live"))

    async def drain():
        n = 0
        while await app.quote_service.run_once() is not None:
            n += 1
        return n
    n = asyncio.run(drain())
    assert n > 70 and app.quote_service.stats["ok"] > 0 and app.quote_service.stats["refused"] > 0
    assert set(client.calls) <= READ_ONLY                         # never buy / sell / authorize
    assert app.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == n
    lim = app.conn.execute("SELECT max_payout, min_stake FROM platform_limits").fetchall()
    assert lim and all(tuple(r) == ("1000.00", "0.35") for r in lim)

    res = asyncio.run(app.evaluate_once())
    vs = res.verdicts
    assert len(vs) == 70
    for k in ("predator", "predator_v2", "navix", "pulse2", "crysis"):     # break at the payout cap
        assert all(vs[(k, s)].status == AVOID and vs[(k, s)].critical_kind == "risk" for s in ALLOWED), k
    for (bot, sym), v in vs.items():
        assert v.status != RUN                                    # default config: no bankroll -> no START
        for lg in v.legs:
            assert lg.economics.status != E.UNKNOWN or "Deriv refuses" in lg.economics.detail, (bot, sym, lg.leg_id)
    a = vs[("aria", "R_50")]
    assert a.risk_status == R.UNKNOWN and "bankroll not configured" in a.risk.reason
    assert a.legs[0].economics.payout == 1.09
    # three cycles: still no START, every check stored
    asyncio.run(app.evaluate_once())
    asyncio.run(app.evaluate_once())
    assert notes(app.conn, "START") == []
    hb = app.heartbeat_payload()
    assert hb["gate_counts"]["risk"].get("UNSAFE", 0) == 35 and hb["quotes"]["stored"] == n
    assert app.conn.execute("SELECT COUNT(*) FROM strategy_evaluations").fetchone()[0] == 70


def test_quote_service_survives_transient_errors(app_factory):  # noqa: F811
    class Flaky(QuotingDeriv):
        fails = 3

        async def request(self, payload, **kw):
            if "proposal" in payload and self.fails:
                self.fails -= 1
                raise asyncio.TimeoutError()
            return await super().request(payload, **kw)
    client = Flaky(DIGIT_SYMBOLS)
    app = app_factory(client)
    app.quote_service.client = client
    asyncio.run(app.discover())
    q = asyncio.run(app.quote_service.run_once())
    assert q is not None and q.transient and not q.ok
    assert app.quote_service.stats["errors"] == 1
    # a transient failure is retried after retry_seconds, not treated as a refusal
    assert app.quote_service.due(time.time() + 1).key.db != q.key.db

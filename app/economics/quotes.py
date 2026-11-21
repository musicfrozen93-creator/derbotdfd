"""Deriv ``proposal`` price quotes (public endpoint, no token; nothing is ever bought).

A quote is the payout Deriv offers for one exact stake. Refusals are kept too: Deriv's
``ContractBuyValidationError`` message states the platform limits that applied, e.g.

    "Minimum stake of 0.35 and maximum payout of 1000.00. Current payout is 3265.10."
    "Please enter a stake amount that's at least 0.35."

Those limits are recorded per market and contract as *observed for the public, unauthenticated
quote context*; an account can have different limits (``risk.max_payout_override``).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from app.monitoring.logger import event
from app.storage.database import transaction

log = logging.getLogger("economics.quotes")

LIMITS_RE = re.compile(r"Minimum stake of\s*([0-9]+(?:\.[0-9]+)?)\s*and maximum payout of\s*([0-9]+(?:\.[0-9]+)?)"
                       r"(?:\.\s*Current payout is\s*([0-9]+(?:\.[0-9]+)?))?", re.I)
MIN_STAKE_RE = re.compile(r"at least\s*([0-9]+(?:\.[0-9]+)?)", re.I)
REFUSAL_CODES = {"ContractBuyValidationError", "InputValidationFailed", "OfferingsValidationError"}
CONTEXT = "Deriv public quote endpoint, no account (unauthenticated), currency USD, basis stake"


@dataclass(frozen=True)
class QuoteKey:
    symbol: str
    contract_type: str
    barrier: int | None
    duration: int
    stake: float

    @property
    def db(self) -> tuple:
        return (self.symbol, self.contract_type, "" if self.barrier is None else str(self.barrier),
                int(self.duration), f"{self.stake:.2f}")

    def payload(self, currency: str = "USD") -> dict:
        req = {"proposal": 1, "amount": float(f"{self.stake:.2f}"), "basis": "stake",
               "contract_type": self.contract_type, "currency": currency, "duration": int(self.duration),
               "duration_unit": "t", "underlying_symbol": self.symbol}
        if self.barrier is not None:
            req["barrier"] = str(self.barrier)
        return req


@dataclass
class Quote:
    key: QuoteKey
    ok: bool
    fetched_at: float
    payout: float | None = None
    ask_price: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    reported_payout: float | None = None   # "Current payout is X" in a refusal
    max_payout: float | None = None        # limit stated in the refusal
    min_stake: float | None = None

    @property
    def refused(self) -> bool:
        """Deriv answered and refused this exact contract (a definitive answer, not a glitch)."""
        return not self.ok and self.error_code in REFUSAL_CODES

    @property
    def refused_reason(self) -> str | None:
        if not self.refused:
            return None
        if self.max_payout is not None and self.reported_payout is not None and self.reported_payout > self.max_payout:
            return f"payout {self.reported_payout:,.2f} above the maximum payout {self.max_payout:,.2f}"
        if self.min_stake is not None and self.key.stake < self.min_stake:
            return f"stake {self.key.stake:.2f} below the minimum stake {self.min_stake:.2f}"
        return f"{self.error_code}: {self.error_message}"

    @property
    def transient(self) -> bool:
        return not self.ok and not self.refused

    def age(self, now: float) -> float:
        return now - self.fetched_at


def parse_ok(key: QuoteKey, msg: dict, now: float) -> Quote:
    prop = msg.get("proposal") or {}
    payout = prop.get("payout")
    ask = prop.get("ask_price")
    if payout is None or ask is None:
        return Quote(key, False, now, error_code="MalformedResponse", error_message="proposal without payout")
    return Quote(key, True, now, payout=float(payout), ask_price=float(ask))


def parse_error(key: QuoteKey, code: str | None, message: str | None, now: float) -> Quote:
    q = Quote(key, False, now, error_code=str(code or "Unknown"), error_message=str(message or "")[:300])
    m = LIMITS_RE.search(q.error_message or "")
    if m:
        q.min_stake = float(m.group(1))
        q.max_payout = float(m.group(2))
        if m.group(3):
            q.reported_payout = float(m.group(3))
    else:
        m2 = MIN_STAKE_RE.search(q.error_message or "")
        if m2 and "stake" in (q.error_message or "").lower():
            q.min_stake = float(m2.group(1))
    return q


@dataclass
class PlatformLimits:
    symbol: str
    contract_type: str
    max_payout: float | None
    min_stake: float | None
    observed_at: float
    source: str = "Deriv refusal message"
    context: str = CONTEXT


class QuoteBook:
    """Latest quote per exact key, in memory and in SQLite (``quotes``, ``platform_limits``)."""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.quotes: dict[tuple, Quote] = {}
        self.limits: dict[tuple[str, str], PlatformLimits] = {}

    def load(self) -> None:
        try:
            rows = self.conn.execute("SELECT * FROM quotes").fetchall()
            lrows = self.conn.execute("SELECT * FROM platform_limits").fetchall()
        except Exception:                      # tables not created yet
            return
        for r in rows:
            key = QuoteKey(r["symbol"], r["contract_type"], int(r["barrier"]) if r["barrier"] != "" else None,
                           int(r["duration"]), float(r["stake"]))
            f = lambda v: None if v is None else float(v)  # noqa: E731
            self.quotes[key.db] = Quote(key, bool(r["ok"]), float(r["fetched_at"]), f(r["payout"]), f(r["ask_price"]),
                                        r["error_code"], r["error_message"], f(r["reported_payout"]),
                                        f(r["max_payout"]), f(r["min_stake"]))
        for r in lrows:
            self.limits[(r["symbol"], r["contract_type"])] = PlatformLimits(
                r["symbol"], r["contract_type"], None if r["max_payout"] is None else float(r["max_payout"]),
                None if r["min_stake"] is None else float(r["min_stake"]), float(r["observed_at"] or 0),
                r["source"] or "", r["context"] or "")

    def get(self, key: QuoteKey) -> Quote | None:
        return self.quotes.get(key.db)

    def put(self, q: Quote) -> None:
        self.quotes[q.key.db] = q
        s = lambda v: None if v is None else f"{v:.2f}"  # noqa: E731
        lim = None
        if q.max_payout is not None or q.min_stake is not None:
            old = self.limits.get((q.key.symbol, q.key.contract_type))
            lim = PlatformLimits(q.key.symbol, q.key.contract_type,
                                 q.max_payout if q.max_payout is not None else (old.max_payout if old else None),
                                 q.min_stake if q.min_stake is not None else (old.min_stake if old else None),
                                 q.fetched_at)
            self.limits[(q.key.symbol, q.key.contract_type)] = lim
        with transaction(self.conn, "save_quote"):
            self.conn.execute(
                "INSERT INTO quotes(symbol, contract_type, barrier, duration, stake, ok, payout, ask_price, error_code,"
                " error_message, reported_payout, max_payout, min_stake, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(symbol, contract_type, barrier, duration, stake) DO UPDATE SET ok=excluded.ok,"
                " payout=excluded.payout, ask_price=excluded.ask_price, error_code=excluded.error_code,"
                " error_message=excluded.error_message, reported_payout=excluded.reported_payout,"
                " max_payout=excluded.max_payout, min_stake=excluded.min_stake, fetched_at=excluded.fetched_at",
                (*q.key.db, int(q.ok), s(q.payout), s(q.ask_price), q.error_code, q.error_message,
                 s(q.reported_payout), s(q.max_payout), s(q.min_stake), int(q.fetched_at)))
            if lim is not None:
                self.conn.execute(
                    "INSERT INTO platform_limits(symbol, contract_type, max_payout, min_stake, source, context, observed_at)"
                    " VALUES (?,?,?,?,?,?,?) ON CONFLICT(symbol, contract_type) DO UPDATE SET max_payout=excluded.max_payout,"
                    " min_stake=excluded.min_stake, source=excluded.source, context=excluded.context,"
                    " observed_at=excluded.observed_at",
                    (lim.symbol, lim.contract_type, s(lim.max_payout), s(lim.min_stake), lim.source, lim.context,
                     int(lim.observed_at)))


@dataclass
class QuoteNeed:
    priority: int                 # 0 = EV stake of a leg, 1+ = ladder step k
    key: QuoteKey
    why: str


@dataclass
class QuoteService:
    """Keeps the quotes the evaluation needs fresh, at Deriv's proposal rate limit.

    ``needs_fn()`` returns the keys currently needed (it is re-read every iteration, so ladders
    advance one step at a time and stop at the first refused stake)."""
    client: object
    book: QuoteBook
    needs_fn: object
    refresh_s: float = 6 * 3600
    retry_s: float = 60
    currency: str = "USD"
    idle_sleep_s: float = 30
    stats: dict = field(default_factory=lambda: {"requests": 0, "ok": 0, "refused": 0, "errors": 0})

    def due(self, now: float) -> QuoteNeed | None:
        best = None
        for need in sorted(self.needs_fn(), key=lambda n: n.priority):
            q = self.book.get(need.key)
            if q is None:
                return need
            limit = self.retry_s if q.transient else self.refresh_s
            if q.age(now) >= limit and best is None:
                best = need
        return best

    async def fetch(self, key: QuoteKey, now: float | None = None) -> Quote:
        from app.deriv.client import DerivAPIError
        now = now or time.time()
        self.stats["requests"] += 1
        try:
            msg = await self.client.request(key.payload(self.currency), bucket="proposal")
            q = parse_ok(key, msg, now)
        except DerivAPIError as exc:
            q = parse_error(key, exc.code, exc.message, now)
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            q = Quote(key, False, now, error_code=type(exc).__name__, error_message=str(exc)[:300])
        if q.ok:
            self.stats["ok"] += 1
        elif q.refused:
            self.stats["refused"] += 1
        else:
            self.stats["errors"] += 1
        self.book.put(q)
        return q

    async def run_once(self, now: float | None = None) -> Quote | None:
        now = now or time.time()
        need = self.due(now)
        if need is None:
            return None
        q = await self.fetch(need.key, now)
        if not q.ok:
            event(log, "quote_refused" if q.refused else "quote_failed", logging.INFO if q.refused else logging.WARNING,
                  symbol=need.key.symbol, contract=need.key.contract_type, barrier=need.key.barrier,
                  duration=need.key.duration, stake=f"{need.key.stake:.2f}", code=q.error_code,
                  detail=(q.refused_reason or q.error_message or "")[:200])
        if q.error_code == "RateLimit":
            await asyncio.sleep(60)
        return q

    async def loop(self, ready: asyncio.Event | None = None) -> None:
        if ready is not None:
            await ready.wait()
        while True:
            try:
                await self.client.wait_connected(None)
                q = await self.run_once()
                if q is None:
                    await asyncio.sleep(self.idle_sleep_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event=quote_loop_error")
                await asyncio.sleep(self.retry_s)

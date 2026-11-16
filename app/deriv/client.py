"""Deriv public WebSocket client.

* one connection, request/response matched by ``req_id``
* tick subscriptions dispatched to a callback
* app-level ``ping`` heartbeat + idle watchdog
* automatic reconnect with exponential back-off and jitter
* token-bucket rate limiting (Deriv documents shared per-minute limits)

All JSON is parsed with ``parse_float=Decimal`` so quotes keep their exact text.
The client is strictly read-only: it exposes no buy / sell / authorize calls.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import time
from decimal import Decimal
from typing import Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.monitoring.logger import event

log = logging.getLogger("deriv.client")

# Calls that could move money are refused even if some future code tries them.
FORBIDDEN_CALLS = {"buy", "sell", "authorize", "transfer_between_accounts", "cashier",
                   "paymentagent_transfer", "topup_virtual", "contract_update", "cancel",
                   "sell_expired", "buy_contract_for_multiple_accounts"}


class DerivAPIError(Exception):
    def __init__(self, code: str, message: str, details=None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


class NotConnectedError(ConnectionError):
    pass


class RateLimiter:
    """Token bucket: ``per_minute`` tokens per minute, burst of ``burst``."""

    def __init__(self, per_minute: float, burst: int = 10) -> None:
        self.rate = max(per_minute, 1) / 60.0
        self.capacity = max(1, burst)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def loads(raw: str | bytes) -> dict:
    return json.loads(raw, parse_float=Decimal)


class DerivClient:
    def __init__(self, url: str, *, request_timeout: float = 30, open_timeout: float = 20,
                 ping_interval: float = 30, idle_reconnect: float = 60,
                 backoff_min: float = 1, backoff_max: float = 60,
                 general_per_min: float = 150, proposal_per_min: float = 120,
                 connect_factory=None) -> None:
        self.url = url
        self.request_timeout = request_timeout
        self.open_timeout = open_timeout
        self.ping_interval = ping_interval
        self.idle_reconnect = idle_reconnect
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self._connect = connect_factory or connect
        self._limiters = {"general": RateLimiter(general_per_min), "proposal": RateLimiter(proposal_per_min)}
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._ws = None
        self._connected = asyncio.Event()
        self._stopping = False
        self.on_tick: Callable[[dict], None] | None = None
        self._on_connect: list[Callable[[], Awaitable[None]]] = []
        self._on_disconnect: list[Callable[[], None]] = []
        self.connect_count = 0
        self.disconnected_since: float | None = time.time()
        self.last_message_at: float | None = None
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ hooks
    def add_connect_hook(self, cb: Callable[[], Awaitable[None]]) -> None:
        self._on_connect.append(cb)

    def add_disconnect_hook(self, cb: Callable[[], None]) -> None:
        self._on_disconnect.append(cb)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float | None = None) -> None:
        if self._connected.is_set():
            return
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError as exc:
            raise NotConnectedError("not connected to Deriv") from exc

    # ------------------------------------------------------------ lifecycle
    async def run(self) -> None:
        backoff = self.backoff_min
        while not self._stopping:
            started = time.monotonic()
            try:
                async with self._connect(self.url, open_timeout=self.open_timeout, ping_interval=20,
                                         ping_timeout=20, close_timeout=5, max_size=2 ** 25) as ws:
                    self._ws = ws
                    self.connect_count += 1
                    self.disconnected_since = None
                    self.last_message_at = time.time()
                    self._connected.set()
                    event(log, "api_connected", url=self.url, attempt=self.connect_count)
                    for cb in self._on_connect:
                        self._spawn(cb())
                    ping_task = asyncio.create_task(self._ping_loop())
                    try:
                        await self._recv_loop(ws)
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                self._teardown("cancelled")
                raise
            except Exception as exc:  # network errors, handshake failures, ...
                event(log, "api_connection_error", logging.WARNING, error=f"{type(exc).__name__}: {exc}")
            self._teardown("closed")
            if self._stopping:
                break
            if time.monotonic() - started > 60:
                backoff = self.backoff_min
            delay = backoff + random.uniform(0, backoff / 2)
            event(log, "api_reconnect_wait", logging.WARNING, delay_s=round(delay, 1))
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, self.backoff_max)

    async def stop(self) -> None:
        self._stopping = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # pragma: no cover
                pass

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _teardown(self, reason: str) -> None:
        was_connected = self._connected.is_set()
        self._ws = None
        self._connected.clear()
        if self.disconnected_since is None:
            self.disconnected_since = time.time()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(NotConnectedError(f"connection {reason}"))
        self._pending.clear()
        if was_connected:
            event(log, "api_disconnected", logging.WARNING, reason=reason)
            for cb in self._on_disconnect:
                try:
                    cb()
                except Exception:  # pragma: no cover
                    log.exception("disconnect hook failed")

    async def _recv_loop(self, ws) -> None:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), self.idle_reconnect)
            except asyncio.TimeoutError:
                event(log, "api_idle_timeout", logging.WARNING, idle_s=self.idle_reconnect)
                await ws.close()
                return
            except ConnectionClosed:
                return
            self.last_message_at = time.time()
            try:
                msg = loads(raw)
            except ValueError:
                event(log, "api_malformed_message", logging.WARNING, sample=str(raw)[:200])
                continue
            if not isinstance(msg, dict):
                event(log, "api_malformed_message", logging.WARNING, sample=str(raw)[:200])
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        if msg.get("msg_type") == "tick" and isinstance(msg.get("tick"), dict) and self.on_tick:
            try:
                self.on_tick(msg["tick"])
            except Exception:
                log.exception("tick handler failed")
        rid = msg.get("req_id")
        if rid is None:
            return
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return
        fut = self._pending.get(rid)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ping_interval)
            try:
                await self.request({"ping": 1}, timeout=15, rate_limited=False)
            except Exception as exc:
                event(log, "api_ping_failed", logging.WARNING, error=str(exc))
                ws = self._ws
                if ws is not None:
                    await ws.close()
                return

    # -------------------------------------------------------------- requests
    async def request(self, payload: dict, *, timeout: float | None = None, bucket: str = "general",
                      rate_limited: bool = True) -> dict:
        call = next(iter(payload), "")
        if call in FORBIDDEN_CALLS:
            raise PermissionError(f"call '{call}' is forbidden: analyzer is read-only")
        if rate_limited:
            await self._limiters[bucket].acquire()
        await self.wait_connected(timeout or self.request_timeout)
        ws = self._ws
        if ws is None:
            raise NotConnectedError("not connected")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await ws.send(json.dumps({**payload, "req_id": rid}))
            msg = await asyncio.wait_for(fut, timeout or self.request_timeout)
        except ConnectionClosed as exc:
            raise NotConnectedError(str(exc)) from exc
        finally:
            self._pending.pop(rid, None)
            # If the socket dropped while this request was still sending (or the caller was
            # cancelled), _teardown may have failed the future after nobody awaits it any more.
            # Collect it so asyncio does not log "Future exception was never retrieved".
            if fut.done():
                if not fut.cancelled():
                    fut.exception()
            else:
                fut.cancel()
        err = msg.get("error")
        if err:
            raise DerivAPIError(str(err.get("code")), str(err.get("message")), err.get("details"))
        return msg

    async def subscribe_ticks(self, symbol: str) -> str | None:
        """Subscribe to live ticks. Returns the subscription id."""
        try:
            msg = await self.request({"ticks": symbol, "subscribe": 1})
        except DerivAPIError as exc:
            if exc.code == "AlreadySubscribed":
                return None
            raise
        sub = msg.get("subscription") or {}
        return sub.get("id")

    async def forget(self, subscription_id: str) -> None:
        try:
            await self.request({"forget": subscription_id})
        except DerivAPIError as exc:
            event(log, "api_forget_failed", logging.WARNING, error=str(exc))

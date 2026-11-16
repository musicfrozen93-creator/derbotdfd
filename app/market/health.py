"""Feed health: freshness, missing ticks, duplicates, connection state."""
from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field

OK = "OK"
STALE = "STALE"      # behind, but not yet critical
FAULT = "FAULT"      # critical: stale beyond critical threshold or disconnected too long


@dataclass
class SymbolHealth:
    symbol: str
    nominal_interval: float = 1.0
    last_epoch: int | None = None
    last_wall: float | None = None
    subscribed_at: float | None = None
    ticks: int = 0
    duplicates: int = 0
    duplicate_conflicts: int = 0
    out_of_order: int = 0
    missing_events: int = 0
    malformed: int = 0
    recent_gaps: list[tuple[int, int]] = field(default_factory=list)
    nominal_known: bool = False    # False until learned from stored or live ticks
    deltas: deque = field(default_factory=lambda: deque(maxlen=61))

    MIN_DELTAS = 5

    def learn(self, delta: int) -> None:
        """Track the tick interval from live ticks (median: robust to the occasional gap).
        1s indices tick every second, the classic Volatility indices every 2 s."""
        if delta <= 0:
            return
        self.deltas.append(delta)
        if len(self.deltas) >= self.MIN_DELTAS:
            self.nominal_interval = float(statistics.median(self.deltas))
            self.nominal_known = True


class HealthMonitor:
    def __init__(self, *, stale_factor: float = 10, critical_stale_s: float = 120,
                 critical_disconnect_s: float = 120) -> None:
        self.stale_factor = stale_factor
        self.critical_stale_s = critical_stale_s
        self.critical_disconnect_s = critical_disconnect_s
        self.symbols: dict[str, SymbolHealth] = {}
        self.connected = False
        self.disconnected_since: float | None = time.time()
        self.started_at = time.time()

    def get(self, symbol: str) -> SymbolHealth:
        h = self.symbols.get(symbol)
        if h is None:
            h = self.symbols[symbol] = SymbolHealth(symbol)
        return h

    def remove(self, symbol: str) -> None:
        self.symbols.pop(symbol, None)

    def set_connected(self, connected: bool, now: float | None = None) -> None:
        now = now or time.time()
        self.connected = connected
        if connected:
            self.disconnected_since = None
        elif self.disconnected_since is None:
            self.disconnected_since = now

    def on_subscribed(self, symbol: str, now: float | None = None) -> None:
        self.get(symbol).subscribed_at = now or time.time()

    def on_tick(self, symbol: str, epoch: int, wall: float | None = None) -> None:
        h = self.get(symbol)
        h.ticks += 1
        h.last_wall = wall or time.time()
        if h.last_epoch is None or epoch > h.last_epoch:
            if h.last_epoch is not None:
                h.learn(epoch - h.last_epoch)
            h.last_epoch = epoch

    def set_nominal(self, symbol: str, interval: float) -> None:
        if interval and interval > 0:
            h = self.get(symbol)
            h.nominal_interval = float(interval)
            h.nominal_known = True

    def connection_fault(self, now: float | None = None) -> bool:
        now = now or time.time()
        return (not self.connected and self.disconnected_since is not None
                and now - self.disconnected_since >= self.critical_disconnect_s)

    def status(self, symbol: str, now: float | None = None) -> tuple[str, str]:
        """Return (status, detail) for a symbol's live feed."""
        now = now or time.time()
        h = self.get(symbol)
        if self.connection_fault(now):
            return FAULT, f"Deriv connection down for {int(now - self.disconnected_since)} s"
        ref = h.last_wall or h.subscribed_at
        if ref is None:
            return STALE, "no ticks received yet"
        age = now - ref
        if age >= self.critical_stale_s:
            return FAULT, f"no ticks for {int(age)} s"
        if h.last_wall is None:
            return STALE, "waiting for the first tick"
        if age >= self.stale_factor * max(h.nominal_interval, 1.0):
            return STALE, f"no ticks for {int(age)} s"
        return OK, "live"

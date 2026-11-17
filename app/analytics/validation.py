"""Signal rule: "Manual Conditions", mode Individual.

For each (bot, market) combination:

1. take the bot's contracts exactly as configured in its strategy YAML (contract type + barrier):
   every contract (``signal_rule.contracts: all``, the default) or only the first / entry
   contract (``signal_rule.contracts: first``);
2. evaluate EVERY contract on its own: its winning digits are the last digits that win that
   contract (e.g. DIGITUNDER 5 -> 0-4), and the contract PASSES only if each of its winning
   digits is STRICTLY above ``signal_rule.threshold_pct`` (10 %) of the last
   ``signal_rule.window_ticks`` (500) ticks of that market. One digit at 10.00 % or lower fails
   that contract;
3. the bot passes only if ALL of its contracts pass. Winning digits of different contracts are
   never merged into one set for the decision; every contract keeps its own PASS / FAIL result.

The comparison is exact integer arithmetic (``count * 100 > threshold * window``), so a digit at
exactly 10.00 % fails and floating-point rounding can never turn a fail into a pass.

Statuses (the signal engine turns them into START / STOP):

* RUN_CANDIDATE - every contract passes (3 checks in a row -> START)
* WAIT          - at least one contract fails (3 in a row on an active signal -> STOP); never messaged
* INSUFFICIENT_DATA - fewer than ``window_ticks`` ticks stored for the market yet
* AVOID         - a contract is no longer offered, or the market's live feed is down (critical STOP)

Nothing else gates a signal: there is no win-rate, break-even, confidence-bound or other
statistical test. The live-feed state is only used so that a START is never sent from a frozen
feed (percentages of a feed that stopped updating are not "current").
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from fractions import Fraction

import numpy as np

from app.strategies.loader import BotSpec

RUN = "RUN_CANDIDATE"
WAIT = "WAIT"
AVOID = "AVOID"
INSUFFICIENT = "INSUFFICIENT_DATA"

CLEAN = "CLEAN"          # live feed OK
DEGRADED = "DEGRADED"    # live feed late / reconnecting, below the critical threshold
FAULT = "FAULT"          # live feed down beyond the critical threshold

MODES = ("individual",)
CONTRACT_SCOPES = ("all", "first")


@dataclass(frozen=True)
class RuleConfig:
    threshold_pct: Fraction
    window_ticks: int
    contracts: str = "all"
    mode: str = "individual"

    @classmethod
    def from_settings(cls, cfg) -> "RuleConfig":
        mode = str(cfg.mode).lower()
        scope = str(cfg.contracts).lower()
        if mode not in MODES:
            raise ValueError(f"signal_rule.mode must be one of {MODES}, got {cfg.mode!r}")
        if scope not in CONTRACT_SCOPES:
            raise ValueError(f"signal_rule.contracts must be one of {CONTRACT_SCOPES}, got {cfg.contracts!r}")
        window = int(cfg.window_ticks)
        if window < 10:
            raise ValueError("signal_rule.window_ticks must be at least 10")
        thr = Fraction(str(cfg.threshold_pct))
        if not 0 <= thr < 100:
            raise ValueError("signal_rule.threshold_pct must be between 0 and 100")
        return cls(thr, window, scope, mode)

    @property
    def threshold_text(self) -> str:
        return f"{float(self.threshold_pct):g}%"


# ---------------------------------------------------------------- winning digits
def rule_legs(bot: BotSpec, scope: str = "all") -> list:
    """The bot's contracts the rule looks at: every leg, or only the entry (first) leg."""
    return list(bot.legs) if scope == "all" else [bot.steps[0]]


# ------------------------------------------------------------- digit percentages
def digit_counts(digits: np.ndarray) -> list[int]:
    return [int(c) for c in np.bincount(np.asarray(digits, dtype=np.int64), minlength=10)[:10]]


def pct(count: int, n: int) -> float:
    return 100.0 * count / n if n else 0.0


def digit_passes(count: int, n: int, threshold_pct: Fraction) -> bool:
    """True if count / n is STRICTLY above threshold_pct percent (exact arithmetic)."""
    return n > 0 and count * 100 > threshold_pct * n


def check_rule(counts: list[int], n: int, winning: list[int], threshold_pct: Fraction) -> tuple[bool, list[int]]:
    """Individual mode, ONE contract: every winning digit of that contract must be strictly above
    the threshold.
    Returns (passed, failing_digits)."""
    failing = [d for d in winning if not digit_passes(counts[d], n, threshold_pct)]
    return (not failing and bool(winning)), failing


# ---------------------------------------------------------------------- verdicts
@dataclass
class SymbolQuality:
    symbol: str
    status: str              # CLEAN | DEGRADED | FAULT (live feed)
    detail: str
    live_status: str
    n_ticks: int = 0         # ticks in the window actually used
    nominal_interval: float = 1.0
    counts: list[int] = field(default_factory=lambda: [0] * 10)
    window_from: int = 0     # epoch of the oldest tick in the window
    window_to: int = 0       # epoch of the newest tick in the window

    def pct(self, digit: int) -> float:
        return pct(self.counts[digit], self.n_ticks)

    def to_dict(self) -> dict:
        return asdict(self)


PASS = "PASS"
FAIL = "FAIL"
NOT_CHECKED = "NOT_CHECKED"      # window not full yet
UNAVAILABLE = "UNAVAILABLE"      # contract not offered on this market (contracts_for)


@dataclass
class ContractResult:
    """One contract of a bot, evaluated on its own winning digits."""
    leg_id: str
    label: str               # e.g. "DIGITOVER 3"
    contract_type: str
    barrier: int | None
    duration: int
    winning_digits: list[int]
    result: str = NOT_CHECKED                                    # PASS | FAIL | NOT_CHECKED | UNAVAILABLE
    digit_pct: dict[int, float] = field(default_factory=dict)    # this contract's winning digits only
    failing_digits: list[int] = field(default_factory=list)
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.result == PASS

    @property
    def name(self) -> str:
        return f"{self.label} ({self.leg_id})"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["digit_pct"] = {str(k): round(v, 4) for k, v in self.digit_pct.items()}
        return d


def evaluate_contract(leg, counts: list[int], n: int, rule: "RuleConfig", *,
                      available: bool = True, unavailable_reason: str = "") -> ContractResult:
    """Apply the rule to ONE contract using only that contract's winning digits."""
    win = list(leg.win_digits)
    c = ContractResult(leg.id, leg.label, leg.contract_type, leg.barrier, leg.duration_ticks, win)
    if n:
        c.digit_pct = {d: pct(counts[d], n) for d in win}
    if not available:
        c.result, c.detail = UNAVAILABLE, unavailable_reason or "contract not offered"
        return c
    if n < rule.window_ticks:
        c.result, c.detail = NOT_CHECKED, f"only {n:,} of {rule.window_ticks:,} ticks"
        return c
    passed, failing = check_rule(counts, n, win, rule.threshold_pct)
    c.failing_digits = failing
    c.result = PASS if passed else FAIL
    c.detail = ((f"all {len(win)} winning digits above {rule.threshold_text}" if len(win) > 1 else
                 f"winning digit above {rule.threshold_text}") if passed else
                f"digit{'s' if len(failing) > 1 else ''} at or below {rule.threshold_text}: "
                + ", ".join(f"{d} = {pct(counts[d], n):.2f}%" for d in failing))
    return c


@dataclass
class BotVerdict:
    bot: str
    bot_name: str
    symbol: str
    symbol_name: str
    status: str
    critical: bool
    reasons: list[str]
    quality: SymbolQuality | None
    compatible: bool
    compat_text: str
    contracts: list[ContractResult] = field(default_factory=list)
    window_ticks: int = 0
    threshold_pct: float = 10.0
    evaluated_at: float = 0.0
    critical_kind: str | None = None     # "data" | "contract"
    hold: bool = False                   # live feed late: no START, no STOP count

    # -- views over the per-contract results
    @property
    def failing_contracts(self) -> list[ContractResult]:
        return [c for c in self.contracts if c.result == FAIL]

    @property
    def passing_contracts(self) -> list[ContractResult]:
        return [c for c in self.contracts if c.result == PASS]

    @property
    def all_contracts_pass(self) -> bool:
        return bool(self.contracts) and all(c.passed for c in self.contracts)

    def contracts_text(self) -> str:
        """Compact per-contract result, e.g. 'base:DIGITUNDER9=FAIL[3,4] recovery:DIGITUNDER5=PASS'."""
        return " ".join(f"{c.leg_id}:{c.label.replace(' ', '')}={c.result}"
                        + (f"[{','.join(map(str, c.failing_digits))}]" if c.failing_digits else "")
                        for c in self.contracts)

    def summary(self) -> dict:
        return {"status": self.status, "critical": self.critical, "critical_kind": self.critical_kind,
                "hold": self.hold, "reasons": self.reasons, "compatible": self.compatible,
                "compat": self.compat_text, "rule": {"mode": "individual", "threshold_pct": self.threshold_pct,
                                                     "window_ticks": self.window_ticks,
                                                     "decision": "every contract must pass"},
                "contracts": [c.to_dict() for c in self.contracts],
                "contracts_passing": len(self.passing_contracts), "contracts_total": len(self.contracts),
                "quality": None if self.quality is None else self.quality.to_dict()}


def evaluate_combo(bot: BotSpec, symbol: str, symbol_name: str, quality: SymbolQuality, *,
                   compatible: bool, compat_text: str, rule: RuleConfig, now: float = 0.0,
                   leg_compat: dict[str, str] | None = None) -> BotVerdict:
    """Evaluate every contract of ``bot`` independently on ``symbol``; the bot passes only if all
    of them pass. ``leg_compat`` (leg id -> "ok" / reason, from the compatibility matrix) marks
    individual contracts that the market does not offer."""
    leg_compat = leg_compat or {}
    n = quality.n_ticks
    contracts = []
    for leg in rule_legs(bot, rule.contracts):
        lc = leg_compat.get(leg.id, "ok")
        contracts.append(evaluate_contract(leg, quality.counts, n, rule, available=(lc == "ok"),
                                           unavailable_reason=lc))
    v = BotVerdict(bot.key, bot.name, symbol, symbol_name, WAIT, False, [], quality, compatible, compat_text,
                   contracts=contracts, window_ticks=rule.window_ticks,
                   threshold_pct=float(rule.threshold_pct), evaluated_at=now)
    if not compatible:
        v.status, v.critical, v.critical_kind = AVOID, True, "contract"
        v.reasons = [f"contract unavailable: {compat_text}"]
        return v
    if quality.status == FAULT:
        v.status, v.critical, v.critical_kind = AVOID, True, "data"
        v.reasons = [f"live feed down: {quality.detail}"]
        return v
    if n < rule.window_ticks:
        v.status = INSUFFICIENT
        v.reasons = [f"only {n:,} of {rule.window_ticks:,} ticks stored for {symbol} yet"]
        return v
    failed = [c for c in contracts if not c.passed]
    if failed:
        v.status = WAIT
        v.reasons = [f"contract {c.name} FAIL - {c.detail}" for c in failed]
        return v
    if quality.status != CLEAN:                  # passes, but the feed is late: do not START on it
        v.status, v.hold = WAIT, True
        v.reasons = [f"all contracts pass but live feed not current: {quality.detail}"]
        return v
    v.status = RUN
    v.reasons = [f"all {len(contracts)} contract{'s' if len(contracts) > 1 else ''} PASS"]
    return v

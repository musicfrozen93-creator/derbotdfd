"""Load and validate per-bot strategy YAML files (one file per original XML)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

DIGIT_CONTRACTS = {"DIGITOVER", "DIGITUNDER", "DIGITEVEN", "DIGITODD", "DIGITMATCH", "DIGITDIFF"}
NEEDS_BARRIER = {"DIGITOVER", "DIGITUNDER", "DIGITMATCH", "DIGITDIFF"}
TRIGGER_TYPES = {"every_tick", "last_digit_in", "window_digit_count", "price_vs_sma", "all_of", "any_of"}


class StrategyError(ValueError):
    pass


def win_digits(contract_type: str, barrier: int | None) -> list[int]:
    """Digits that win, per Deriv settlement rules (digit equal to the barrier
    loses both OVER and UNDER)."""
    if contract_type == "DIGITOVER":
        return [d for d in range(10) if d > barrier]
    if contract_type == "DIGITUNDER":
        return [d for d in range(10) if d < barrier]
    if contract_type == "DIGITEVEN":
        return [0, 2, 4, 6, 8]
    if contract_type == "DIGITODD":
        return [1, 3, 5, 7, 9]
    if contract_type == "DIGITMATCH":
        return [barrier]
    if contract_type == "DIGITDIFF":
        return [d for d in range(10) if d != barrier]
    raise StrategyError(f"unsupported contract type {contract_type}")


@dataclass(frozen=True)
class LegSpec:
    id: str
    contract_type: str
    barrier: int | None
    duration_ticks: int
    trigger: dict
    description: str = ""
    trigger_text: str = ""
    compare_to: str = "none"            # none | complement | unconditional

    @property
    def win_digits(self) -> list[int]:
        return win_digits(self.contract_type, self.barrier)

    @property
    def win_table(self) -> np.ndarray:
        t = np.zeros(10, dtype=bool)
        t[self.win_digits] = True
        return t

    @property
    def p0(self) -> float:
        return len(self.win_digits) / 10.0

    @property
    def label(self) -> str:
        b = "" if self.barrier is None else f" {self.barrier}"
        return f"{self.contract_type}{b}"

    @property
    def stat_key(self) -> str:
        """Identity of the statistical test (shared by identical legs of different bots)."""
        return json.dumps([self.contract_type, self.barrier, self.duration_ticks, self.trigger],
                          sort_keys=True)


@dataclass
class BotSpec:
    key: str
    name: str
    source_file: str
    original: dict
    legs: list[LegSpec]
    sequence: dict
    stake: dict
    take_profit: float | None
    stop_loss: float | None
    stop_loss_text: str
    notes: list[str] = field(default_factory=list)
    known_issues: list[str] = field(default_factory=list)
    non_purchase_logic: list[str] = field(default_factory=list)
    optional: bool = False

    def leg(self, leg_id: str) -> LegSpec:
        for leg in self.legs:
            if leg.id == leg_id:
                return leg
        raise KeyError(leg_id)

    @property
    def steps(self) -> list[LegSpec]:
        return [self.leg(s) for s in self.sequence.get("steps", [self.legs[0].id])]

    @property
    def contract_summary(self) -> str:
        parts = []
        seen = set()
        for leg in self.steps:
            if leg.id in seen:
                continue
            seen.add(leg.id)
            parts.append(f"{leg.label} ({leg.id})" if len(self.legs) > 1 else leg.label)
        return " → ".join(parts)

    @property
    def durations(self) -> list[int]:
        return sorted({leg.duration_ticks for leg in self.legs})


def _validate_trigger(trig: dict, where: str) -> None:
    if not isinstance(trig, dict) or trig.get("type") not in TRIGGER_TYPES:
        raise StrategyError(f"{where}: invalid trigger {trig!r}")
    t = trig["type"]
    if t in ("all_of", "any_of"):
        for i, sub in enumerate(trig.get("conditions") or []):
            _validate_trigger(sub, f"{where}.conditions[{i}]")
        if not trig.get("conditions"):
            raise StrategyError(f"{where}: {t} needs conditions")
    elif t == "last_digit_in":
        if not trig.get("digits"):
            raise StrategyError(f"{where}: last_digit_in needs digits")
    elif t == "window_digit_count":
        for k in ("window", "digits", "min_count"):
            if k not in trig:
                raise StrategyError(f"{where}: window_digit_count needs {k}")
    elif t == "price_vs_sma":
        if int(trig.get("period", 0)) < 1 or trig.get("op") not in ("gte", "gt", "lte", "lt"):
            raise StrategyError(f"{where}: price_vs_sma needs period and op")


def parse_bot(data: dict, path: str = "?") -> BotSpec:
    try:
        legs = []
        for i, raw in enumerate(data["legs"]):
            ctype = str(raw["contract_type"]).upper()
            if ctype not in DIGIT_CONTRACTS:
                raise StrategyError(f"{path}: leg {i} unsupported contract {ctype}")
            barrier = raw.get("barrier")
            if ctype in NEEDS_BARRIER:
                if barrier is None:
                    raise StrategyError(f"{path}: leg {i} needs a barrier")
                barrier = int(barrier)
            else:
                barrier = None
            dur = int(raw["duration_ticks"])
            if not 1 <= dur <= 10:
                raise StrategyError(f"{path}: leg {i} duration must be 1-10 ticks")
            _validate_trigger(raw["trigger"], f"{path}: leg {raw.get('id')}")
            legs.append(LegSpec(
                id=str(raw["id"]), contract_type=ctype, barrier=barrier, duration_ticks=dur,
                trigger=raw["trigger"], description=str(raw.get("description", "")),
                trigger_text=str(raw.get("trigger_text", "")),
                compare_to=str(raw.get("compare_to", "none")),
            ))
        ids = [leg.id for leg in legs]
        if len(set(ids)) != len(ids):
            raise StrategyError(f"{path}: duplicate leg ids")
        seq = data.get("sequence") or {"steps": [ids[0]], "on_win": "reset", "on_loss": "advance",
                                       "overflow": "hold_last"}
        for s in seq.get("steps", []):
            if s not in ids:
                raise StrategyError(f"{path}: sequence step {s} is not a leg")
        return BotSpec(
            key=str(data["key"]), name=str(data["name"]), source_file=str(data["source_file"]),
            original=data.get("original") or {}, legs=legs, sequence=seq, stake=data.get("stake") or {},
            take_profit=data.get("take_profit"), stop_loss=data.get("stop_loss"),
            stop_loss_text=str(data.get("stop_loss_text", "")),
            notes=list(data.get("notes") or []), known_issues=list(data.get("known_issues") or []),
            non_purchase_logic=list(data.get("non_purchase_logic") or []),
            optional=bool(data.get("optional", False)),
        )
    except KeyError as exc:
        raise StrategyError(f"{path}: missing field {exc}") from exc


def load_strategies(directory: Path, enabled: list[str] | None = None) -> list[BotSpec]:
    bots = []
    for p in sorted(Path(directory).glob("*.yaml")):
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        bots.append(parse_bot(data, p.name))
    keys = [b.key for b in bots]
    if len(set(keys)) != len(keys):
        raise StrategyError("duplicate bot keys")
    if enabled and "all" not in enabled:
        wanted = set(enabled)
        bots = [b for b in bots if b.key in wanted]
    return bots

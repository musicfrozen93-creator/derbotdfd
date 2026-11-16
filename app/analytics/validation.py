"""Gatekeeping: turn replay statistics into leg / bot statuses.

Status meanings (from the Phase 1 audit, section 12):

* RUN_CANDIDATE – every gate passed (this is the only status that emits START)
* WAIT          – data sufficient and clean, but no edge proven (the expected default)
* AVOID         – data fault, contract unavailable, win rate reliably below
                  break-even, or losing runs longer than independence allows
* INSUFFICIENT_DATA – too few ticks / trigger events, or payout unknown

WAIT / AVOID / INSUFFICIENT_DATA are internal states; they are never messaged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.analytics.payout import Quote
from app.strategies.evaluator import LegStats, SimResult

RUN = "RUN_CANDIDATE"
WAIT = "WAIT"
AVOID = "AVOID"
INSUFFICIENT = "INSUFFICIENT_DATA"

CLEAN = "CLEAN"
DEGRADED = "DEGRADED"
FAULT = "FAULT"


@dataclass
class SymbolQuality:
    symbol: str
    status: str
    detail: str
    live_status: str
    n_ticks: int = 0
    nominal_interval: float = 1.0
    gap_threshold: float = 10.0
    recent_gaps: int = 0
    window_coverage: float = 0.0
    chi2_p: float | None = None
    chi2_n: int = 0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class LegVerdict:
    leg_id: str
    label: str
    duration: int
    trigger_text: str
    status: str
    reasons: list[str]
    checks: dict[str, bool | None]
    stats: LegStats
    payout: Quote | None

    def to_dict(self) -> dict:
        return {"leg": self.leg_id, "label": self.label, "duration": self.duration, "status": self.status,
                "reasons": self.reasons, "checks": self.checks, "stats": self.stats.to_dict(),
                "payout": None if self.payout is None else {"stake": str(self.payout.stake),
                                                            "payout": str(self.payout.payout),
                                                            "break_even": self.payout.break_even}}


@dataclass
class BotVerdict:
    bot: str
    bot_name: str
    symbol: str
    symbol_name: str
    status: str
    critical: bool
    reasons: list[str]
    legs: list[LegVerdict]
    quality: SymbolQuality | None
    compatible: bool
    compat_text: str
    sim: SimResult | None = None
    evaluated_at: float = 0.0
    critical_kind: str | None = None     # "data" | "contract"
    hold: bool = False                   # only failing gate is a non-critical data-quality dip
    extra: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"status": self.status, "critical": self.critical, "critical_kind": self.critical_kind,
                "hold": self.hold, "reasons": self.reasons,
                "compatible": self.compatible, "compat": self.compat_text,
                "quality": None if self.quality is None else self.quality.to_dict(),
                "legs": [lv.to_dict() for lv in self.legs],
                "sim": None if self.sim is None else self.sim.to_dict()}


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.2f}%"


def evaluate_leg(stats: LegStats, *, quality: SymbolQuality, payout: Quote | None, contract_ok: bool,
                 contract_reason: str, cfg, dq_cfg) -> tuple[str, list[str], dict]:
    """Return (status, reasons, checks). ``stats.q_value`` must already be set
    (Benjamini–Hochberg is applied across all legs before this call)."""
    checks: dict[str, bool | None] = {}
    reasons: list[str] = []
    p_be = payout.break_even if payout else None

    checks["data_quality"] = quality.status == CLEAN
    checks["contract_available"] = contract_ok
    checks["payout_known"] = p_be is not None
    checks["digit_uniformity"] = quality.chi2_p is None or quality.chi2_p >= float(dq_cfg.chi2_fault_p)
    checks["min_ticks"] = stats.n_ticks >= int(cfg.min_ticks)
    checks["min_trigger_events"] = stats.n_events >= int(cfg.min_trigger_events)

    # ---- AVOID: faults
    if quality.status == FAULT:
        return AVOID, [f"data fault: {quality.detail}"], checks
    if not contract_ok:
        return AVOID, [f"contract unavailable: {contract_reason}"], checks
    if not checks["digit_uniformity"]:
        return AVOID, [f"digit distribution fault suspected (chi² p={quality.chi2_p:.2g} on last "
                       f"{quality.chi2_n} ticks)"], checks
    # ---- INSUFFICIENT
    if not checks["payout_known"]:
        reasons.append("payout unknown (no fresh proposal quote)")
    if not checks["min_ticks"]:
        reasons.append(f"{stats.n_ticks:,} ticks stored < {int(cfg.min_ticks):,}")
    if not checks["min_trigger_events"]:
        reasons.append(f"{stats.n_events:,} trigger events < {int(cfg.min_trigger_events):,}")
    if reasons:
        return INSUFFICIENT, reasons, checks
    # ---- AVOID: statistical
    checks["upper_bound_above_break_even"] = stats.ci_hi is not None and stats.ci_hi >= p_be
    if not checks["upper_bound_above_break_even"]:
        return AVOID, [f"win rate reliably below break-even (upper 95% {_pct(stats.ci_hi)} < {_pct(p_be)})"], checks
    checks["runs_independent"] = stats.runs_p is None or stats.runs_p >= float(cfg.runs_alpha)
    if not checks["runs_independent"]:
        return AVOID, [f"losing runs longer than independence allows (runs test p={stats.runs_p:.2g})"], checks

    # ---- RUN_CANDIDATE gates
    fails: list[str] = []
    checks["min_trials_run"] = stats.disc_n >= int(cfg.min_trials_run)
    if not checks["min_trials_run"]:
        fails.append(f"discovery trials {stats.disc_n:,} < {int(cfg.min_trials_run):,}")
    checks["lower_bound_above_break_even"] = stats.disc_lo is not None and stats.disc_lo > p_be
    if not checks["lower_bound_above_break_even"]:
        fails.append(f"lower 95% bound {_pct(stats.disc_lo)} ≤ break-even {_pct(p_be)}")
    checks["fdr_survives"] = stats.q_value is not None and stats.q_value <= float(cfg.fdr_q)
    if not checks["fdr_survives"]:
        fails.append(f"does not survive FDR correction (q={stats.q_value if stats.q_value is not None else 'n/a'})")
    checks["out_of_sample"] = (stats.oos_n >= int(cfg.min_oos_trials) and stats.oos_wr is not None
                               and stats.oos_wr > p_be and stats.oos_p is not None
                               and stats.oos_p < float(cfg.oos_alpha))
    if not checks["out_of_sample"]:
        fails.append(f"out-of-sample not confirmed (n={stats.oos_n:,}, WR={_pct(stats.oos_wr)})")
    checks["stable"] = stats.stability_p is not None and stats.stability_p >= float(cfg.stability_alpha)
    if not checks["stable"]:
        fails.append("recent vs prior window unstable or too few events")
    if bool(cfg.latency_robustness):
        checks["latency_robust"] = stats.lat_lo is not None and stats.lat_lo > p_be
        if not checks["latency_robust"]:
            fails.append("edge not robust to one-tick settlement delay")
    if stats.comp_n:
        checks["beats_comparator"] = (stats.disc_wr is not None and stats.comp_wr is not None
                                      and stats.disc_wr > stats.comp_wr)
        if not checks["beats_comparator"]:
            fails.append(f"trigger WR {_pct(stats.disc_wr)} not above comparator {_pct(stats.comp_wr)}")
    if not checks["data_quality"]:
        fails.append(f"data not clean for the full window: {quality.detail}")
    if fails:
        return WAIT, fails, checks
    return RUN, ["all validation gates passed"], checks


def is_data_only_wait(lv: LegVerdict) -> bool:
    """True when a leg is WAIT purely because the recent data window is not clean."""
    if lv.status != WAIT:
        return False
    failed = [k for k, v in lv.checks.items() if v is False]
    return failed == ["data_quality"]


def bot_hold(leg_verdicts: list[LegVerdict]) -> bool:
    flags = [lv.status == RUN or is_data_only_wait(lv) for lv in leg_verdicts]
    return bool(leg_verdicts) and all(flags) and any(is_data_only_wait(lv) for lv in leg_verdicts)


def combine_bot(leg_verdicts: list[LegVerdict]) -> str:
    statuses = [lv.status for lv in leg_verdicts]
    if any(s == AVOID for s in statuses):
        return AVOID
    if any(s == INSUFFICIENT for s in statuses):
        return INSUFFICIENT
    if statuses and all(s == RUN for s in statuses):
        return RUN
    return WAIT

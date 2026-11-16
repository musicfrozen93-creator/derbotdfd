"""Validation gates and end-to-end analyzer replay on synthetic data."""
import time
from decimal import Decimal

import numpy as np

from app.analytics.analyzer import Analyzer, SeriesCache
from app.analytics.payout import Quote, payout_key
from app.analytics.validation import (AVOID, CLEAN, DEGRADED, FAULT, INSUFFICIENT, RUN, WAIT, SymbolQuality,
                                      evaluate_leg, is_data_only_wait, LegVerdict)
from app.deriv.contracts import DigitContract, SymbolContracts
from app.deriv.symbols import MarketInfo
from app.storage import repositories as repo
from app.strategies.compatibility import check
from app.strategies.evaluator import LegStats

from tests.conftest import make_rows


def q(be, sym="S", ctype="DIGITUNDER", barrier=9, dur=1):
    return Quote(sym, ctype, barrier, dur, Decimal("10"), Decimal(str(round(10 / be, 2))), be, time.time())


def good_stats(**kw):
    base = dict(n_ticks=100000, n_events=90000, wins=85000, wr=0.944, ci_lo=0.942, ci_hi=0.946,
                disc_n=60000, disc_k=56640, disc_wr=0.944, disc_lo=0.942, oos_n=27000, oos_k=25500, oos_wr=0.944,
                stability_p=0.5, runs_p=0.5, lat_lo=0.941, q_value=0.001, oos_p=1e-9, p0=0.9)
    base.update(kw)
    return LegStats(**base)


QUAL = SymbolQuality("S", CLEAN, "clean", "OK", 100000, chi2_p=0.5, chi2_n=5000)


def test_run_candidate_when_all_gates_pass(cfg):
    st, reasons, checks = evaluate_leg(good_stats(), quality=QUAL, payout=q(0.917), contract_ok=True,
                                       contract_reason="", cfg=cfg.analysis, dq_cfg=cfg.data_quality)
    assert st == RUN, reasons


def test_payout_unknown_is_insufficient(cfg):
    st, reasons, _ = evaluate_leg(good_stats(), quality=QUAL, payout=None, contract_ok=True, contract_reason="",
                                  cfg=cfg.analysis, dq_cfg=cfg.data_quality)
    assert st == INSUFFICIENT and "payout unknown" in reasons[0]


def test_few_events_is_insufficient(cfg):
    st, _, _ = evaluate_leg(good_stats(n_events=1500), quality=QUAL, payout=q(0.917), contract_ok=True,
                            contract_reason="", cfg=cfg.analysis, dq_cfg=cfg.data_quality)
    assert st == INSUFFICIENT


def test_fault_and_contract_are_avoid(cfg):
    fq = SymbolQuality("S", FAULT, "no ticks for 300 s", "FAULT")
    assert evaluate_leg(good_stats(), quality=fq, payout=q(0.917), contract_ok=True, contract_reason="",
                        cfg=cfg.analysis, dq_cfg=cfg.data_quality)[0] == AVOID
    assert evaluate_leg(good_stats(), quality=QUAL, payout=q(0.917), contract_ok=False, contract_reason="gone",
                        cfg=cfg.analysis, dq_cfg=cfg.data_quality)[0] == AVOID


def test_reliably_below_break_even_is_avoid(cfg):
    s = good_stats(wr=0.90, ci_lo=0.898, ci_hi=0.902)
    st, reasons, _ = evaluate_leg(s, quality=QUAL, payout=q(0.917), contract_ok=True, contract_reason="",
                                  cfg=cfg.analysis, dq_cfg=cfg.data_quality)
    assert st == AVOID and "below break-even" in reasons[0]


def test_twenty_thousand_trials_is_not_automatic_proof(cfg):
    # plenty of trials but lower bound not above break-even -> WAIT
    s = good_stats(disc_lo=0.915, ci_hi=0.93, q_value=0.2)
    st, reasons, _ = evaluate_leg(s, quality=QUAL, payout=q(0.917), contract_ok=True, contract_reason="",
                                  cfg=cfg.analysis, dq_cfg=cfg.data_quality)
    assert st == WAIT


def test_oos_failure_blocks_run(cfg):
    st, reasons, checks = evaluate_leg(good_stats(oos_wr=0.91, oos_p=0.9), quality=QUAL, payout=q(0.917),
                                       contract_ok=True, contract_reason="", cfg=cfg.analysis, dq_cfg=cfg.data_quality)
    assert st == WAIT and checks["out_of_sample"] is False


def test_degraded_only_is_hold(cfg):
    dq = SymbolQuality("S", DEGRADED, "2 tick gaps", "OK", 100000, chi2_p=0.5, chi2_n=5000)
    st, reasons, checks = evaluate_leg(good_stats(), quality=dq, payout=q(0.917), contract_ok=True,
                                       contract_reason="", cfg=cfg.analysis, dq_cfg=cfg.data_quality)
    assert st == WAIT
    lv = LegVerdict("base", "DIGITUNDER 9", 1, "", st, reasons, checks, good_stats(), None)
    assert is_data_only_wait(lv)


def _analyzer_setup(db, bots, cfg, digits, symbol="SYN"):
    conn, path = db
    now = int(time.time())
    n = len(digits)
    repo.insert_ticks(conn, make_rows(symbol, digits, start_epoch=now - n))
    cache = SeriesCache(path, 400000)
    an = Analyzer(list(bots.values()), cache, cfg.analysis, cfg.data_quality)
    market = MarketInfo(symbol, "Synthetic", "synthetic_index", "random_index", 2, True, False)
    sc = SymbolContracts(symbol, {"DIGITUNDER": DigitContract("DIGITUNDER", 1, 10, list(range(1, 10))),
                                  "DIGITOVER": DigitContract("DIGITOVER", 1, 10, list(range(9)))})
    compat = {(k, symbol): check(b, market, sc) for k, b in bots.items()}
    return an, market, compat, now


def test_fair_digits_never_run_candidate(db, bots, cfg, rng):
    digits = rng.integers(0, 10, 60000)
    an, market, compat, now = _analyzer_setup(db, bots, cfg, digits)
    payouts = {}
    for b in ("aria", "delta", "predator"):
        for leg in bots[b].legs:
            be = {9: 0.917, 5: 0.512, 3: 0.612}[leg.barrier]
            payouts[payout_key("SYN", leg.contract_type, leg.barrier, leg.duration_ticks)] = q(
                be, "SYN", leg.contract_type, leg.barrier, leg.duration_ticks)
    res = an.evaluate([("aria", "SYN"), ("delta", "SYN"), ("predator", "SYN")], compat, {"SYN": market}, payouts,
                      {"SYN": ("OK", "live")}, now)
    for v in res.verdicts.values():
        assert v.status in (AVOID, WAIT, INSUFFICIENT), (v.bot, v.status, v.reasons)
        assert v.status != RUN


def test_biased_digits_reach_run_candidate(db, bots, cfg, rng):
    # digit 9 never occurs -> UNDER 9 always wins, UNDER 5 wins 5/9: a real (synthetic) edge
    digits = rng.integers(0, 9, 60000)
    an, market, compat, now = _analyzer_setup(db, bots, cfg, digits)
    payouts = {payout_key("SYN", "DIGITUNDER", 9, 1): q(0.917, "SYN", "DIGITUNDER", 9, 1),
               payout_key("SYN", "DIGITUNDER", 5, 1): q(0.512, "SYN", "DIGITUNDER", 5, 1)}
    res = an.evaluate([("aria", "SYN")], compat, {"SYN": market}, payouts, {"SYN": ("OK", "live")}, now)
    v = res.verdicts[("aria", "SYN")]
    # chi-square on 5k digits flags the missing digit 9 as a data fault: the analyzer must AVOID, not RUN
    assert v.status == AVOID and "digit distribution" in v.reasons[0]
    # with the uniformity alarm relaxed, the same edge passes every statistical gate
    cfg2 = cfg.data_quality.copy()
    cfg2["chi2_fault_p"] = 0.0
    an.dq = type(cfg.data_quality)(cfg2)
    res = an.evaluate([("aria", "SYN")], compat, {"SYN": market}, payouts, {"SYN": ("OK", "live")}, now)
    v = res.verdicts[("aria", "SYN")]
    assert v.status == RUN, v.reasons
    base = v.legs[0].stats
    assert base.wr == 1.0 and base.disc_n >= 20000 and base.q_value < 0.05

#!/usr/bin/env python3
"""Offline replay of stored tick history through the bot-specific evaluator (read-only, no network).

    python scripts/replay_strategy_model.py --ticks DIR [--results results.json] [--live-quotes meta_payouts.json]
                                            [--sample-minutes 60] [--out replay.json]

DIR holds <SYMBOL>.csv.gz files with the header ``epoch,quote,last_digit`` (for example
COMPLETE_BOT_ANALYSIS/supporting_evidence/data/ticks, 7 days x 7 markets).

Steps
1. parity       - every bot leg's entry rule + settlement on every market vs the Node backtest
                  (results.json "signalTests", same ticks, same start index 600, entry gap 1)
2. payout model - the payout formula vs the live Deriv quotes saved in meta_payouts.json
3. evaluator    - the real StrategyAnalyzer at sampled times; it only sees ticks <= the sample time
4. per minute   - the four checks emulated for every minute (vectorised; checked against step 3 at
                  every sample time) and fed to the real SignalEngine -> STARTs / STOPs
5. comparison   - the old analyzer emulation and the 7-day backtest stored in results.json

Quotes: there are no live quotes for past times, so the replay prices every stake with the payout
model checked in step 2 and the platform limits seen in Deriv's refusal messages (minimum stake 0.35,
maximum payout 1000.00, public quote context). Digit payouts depend only on the stake and the
contract, not on the market state, so one quote set serves the whole replay. The live service
never does this: it uses live quotes only.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analytics.analyzer import Series  # noqa: E402
from app.analytics.validation import AVOID, INSUFFICIENT, RUN, WAIT  # noqa: E402
from app.config import DEFAULTS, Section  # noqa: E402
from app.deriv.symbols import MarketInfo  # noqa: E402
from app.economics import ev as E  # noqa: E402
from app.economics.quotes import QuoteBook, parse_error, parse_ok  # noqa: E402
from app.market.precision import format_quote, scaled_int  # noqa: E402
from app.risk import engine as R  # noqa: E402
from app.signals.engine import SignalEngine  # noqa: E402
from app.signals.state import SignalStateStore  # noqa: E402
from app.signals.telegram import MessageFormatter  # noqa: E402
from app.storage.database import init_db  # noqa: E402
from app.strategies.compatibility import CompatResult  # noqa: E402
from app.strategies.loader import load_strategies  # noqa: E402
from app.strategy_model.bots import load_models, win_digits  # noqa: E402
from app.strategy_model.entry import entry_mask, prices_as_doubles  # noqa: E402
from app.strategy_model.evaluate import (FAIL, NOT_CHECKED, PASS, EvalConfig, StrategyAnalyzer,  # noqa: E402
                                         StrategyVerdict)
from app.strategy_model.outcomes import gap_ok_steps, settle, wilson, z_one_sided, z_two_sided  # noqa: E402

SYMBOLS = ["R_10", "R_25", "R_50", "R_75", "R_100", "1HZ10V", "1HZ50V"]
MIN_STAKE, MAX_PAYOUT = 0.35, 1000.0
NODE_TESTS = {  # results.json signalTests name -> (bot, leg)
    "aria_base_UNDER9": ("aria", "base"), "aria_recovery_UNDER5": ("aria", "recovery"),
    "crysis_OVER3_5t_if_X>3&X1>4": ("crysis", "main"), "delta_OVER3_2t_if_last=1": ("delta", "main"),
    "navix_base_UNDER8_if_last=4": ("navix", "base"), "navix_recovery_OVER2_if_last=6": ("navix", "recovery"),
    "predator_OVER3_if_price>=SMA8": ("predator", "main"), "predatorv2_OVER5": ("predator_v2", "main"),
    "pulse2_EVEN_if_last_in_0369": ("pulse2", "main"), "voltrix_OVER7_3t": ("voltrix", "main"),
    "zony_OVER0": ("zony_ai", "step1"), "zony_OVER4": ("zony_ai", "step2"), "zony_OVER5": ("zony_ai", "step3"),
    "zony_OVER6": ("zony_ai", "step4"), "zony_OVER7": ("zony_ai", "step5"), "zony_OVER8": ("zony_ai", "step6"),
}


# ------------------------------------------------------------------ data
def load_ticks(path: Path):
    ep, qt, ld = [], [], []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        head = fh.readline().strip().split(",")
        if head[:2] != ["epoch", "quote"]:
            raise ValueError(f"{path}: unexpected header {head}")
        for line in fh:
            p = line.rstrip("\n").split(",")
            if len(p) >= 2 and p[0]:
                ep.append(int(p[0]))
                qt.append(p[1])
                ld.append(p[2] if len(p) > 2 else "")
    dec = max((len(q.split(".")[1]) if "." in q else 0) for q in qt)
    txt = [format_quote(q, dec) for q in qt]
    prices = np.fromiter((scaled_int(t) for t in txt), dtype=np.int64, count=len(txt))
    digits = np.fromiter((int(t[-1]) for t in txt), dtype=np.int8, count=len(txt))
    epochs = np.array(ep, dtype=np.int64)
    if not np.all(np.diff(epochs) > 0):
        raise ValueError(f"{path}: epochs not strictly increasing")
    digit_column_mismatch = sum(1 for t, d in zip(txt, ld) if d and int(d) != int(t[-1]))
    return {"epochs": epochs, "digits": digits, "prices": prices, "decimals": dec,
            "digit_column_mismatch": digit_column_mismatch}


def display_name(sym: str) -> str:
    return f"Volatility {sym[3:-1]} (1s) Index" if sym.startswith("1HZ") else f"Volatility {sym[2:]} Index"


# ------------------------------------------------------------------ 1. parity with the Node backtest
def parity(data, models, results) -> dict:
    rows, same_n = [], 0
    for sym in SYMBOLS:
        d = data[sym]
        prices = prices_as_doubles(d["prices"], d["decimals"])
        no_gaps = np.ones(len(d["digits"]) - 1, dtype=bool)          # the Node run excluded no gaps
        for name, (bot, leg_id) in NODE_TESTS.items():
            leg = models[bot].leg(leg_id)
            m, _ = entry_mask(leg.entry, d["digits"], prices)
            m = m.copy()
            m[:600] = False                                           # the Node run started at index 600
            idx, _, w = settle(m, d["digits"], no_gaps, leg.duration, 1, leg.win_digits)
            node = results["signalTests"][sym]["gap1"][name]["signal"]
            same = (len(idx), int(w.sum())) == (int(node["n"]), int(node["wins"]))
            same_n += same
            rows.append({"symbol": sym, "test": name, "bot": bot, "leg": leg_id, "python_n": len(idx),
                         "python_wins": int(w.sum()), "node_n": node["n"], "node_wins": node["wins"], "identical": same})
    return {"identical": same_n, "total": len(rows), "rows": rows}


# ------------------------------------------------------------------ 2. payout model vs saved live quotes
def check_live_quotes(path: Path) -> dict:
    m = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for q in m.get("quotes", []):
        if q.get("payout") is None:
            continue
        p = len(win_digits(q["contract_type"], q.get("barrier"))) / 10
        model = E.model_payout(float(q["stake"]), p)
        rows.append({"symbol": q["symbol"], "contract": f"{q['contract_type']} {q.get('barrier', '')} "
                     f"{q.get('duration_ticks')}t", "stake": q["stake"], "live": q["payout"], "model": model,
                     "match": abs(model - float(q["payout"])) <= 0.005})
    return {"endpoint": m.get("endpoint"), "fetched_at_utc": m.get("fetched_at_utc"), "checked": len(rows),
            "match": sum(r["match"] for r in rows), "mismatches": [r for r in rows if not r["match"]][:20]}


def model_quote(key, now: float):
    p = len(win_digits(key.contract_type, key.barrier)) / 10
    if key.stake < MIN_STAKE - 1e-9:
        return parse_error(key, "ContractBuyValidationError",
                           f"Please enter a stake amount that's at least {MIN_STAKE}.", now)
    payout = E.model_payout(key.stake, p)
    if payout > MAX_PAYOUT:
        return parse_error(key, "ContractBuyValidationError",
                           f"Minimum stake of {MIN_STAKE} and maximum payout of {MAX_PAYOUT:.2f}. Current payout is "
                           f"{payout:.2f}.", now)
    return parse_ok(key, {"proposal": {"payout": payout, "ask_price": key.stake}}, now)


# ------------------------------------------------------------------ helpers shared by 3 and 4
class SliceCache:
    """SeriesCache stand-in: the newest ``long`` ticks with epoch <= T (no look-ahead)."""

    def __init__(self, data, long: int):
        self.data, self.long, self.T = data, long, None

    def bounds(self, sym: str, T: float) -> tuple[int, int]:
        N = int(np.searchsorted(self.data[sym]["epochs"], T, side="right"))
        return max(0, N - self.long), N

    def get(self, sym: str) -> Series:
        d = self.data[sym]
        s, N = self.bounds(sym, self.T)
        return Series(sym, d["epochs"][s:N], d["digits"][s:N], d["prices"][s:N])


def live_status(epochs, N: int, T: float, nominal: float, dq) -> tuple[str, str]:
    """Same thresholds as app.market.health, measured on tick epochs instead of arrival time."""
    if N == 0:
        return "FAULT", "no ticks yet"
    age = T - float(epochs[N - 1])
    if age > float(dq.critical_stale_s):
        return "FAULT", f"no tick for {age:.0f} s"
    if age > float(dq.stale_factor) * nominal:
        return "STALE", f"last tick {age:.0f} s ago"
    return "OK", "live"


def nominal_of(epochs) -> float:
    d = np.diff(epochs[-5000:])
    d = d[d > 0]
    return float(np.median(d)) if d.size else 1.0


# ------------------------------------------------------------------ 4. per-minute emulation
def emulate(data, bots, models, an: StrategyAnalyzer, cfg: EvalConfig, grid: np.ndarray, dq) -> dict:
    """Per-minute gate statuses for every combination: {(bot, sym): {gate: np.array(str)}}."""
    z1, z2 = z_one_sided(cfg.confidence), z_two_sided(cfg.confidence)
    cache = an.cache
    out = {}
    for sym in SYMBOLS:
        d = data[sym]
        ep, dg = d["epochs"], d["digits"]
        prices = prices_as_doubles(d["prices"], d["decimals"])
        nominal = nominal_of(ep)
        gap_ok = gap_ok_steps(ep, cfg.gap_factor * max(nominal, 1.0))
        N = np.searchsorted(ep, grid, side="right")
        S = np.maximum(0, N - cache.long)
        enough = N >= cfg.window_ticks
        live = [live_status(ep, int(n), float(t), nominal, dq)[0] for n, t in zip(N, grid)]
        live = np.array(live)
        lo_ep = np.searchsorted(ep, grid - cfg.interval_s, side="right")
        for bot in bots:
            m = models[bot]
            first_leg = m.legs[0]
            mask0, first0 = entry_mask(first_leg.entry, dg, prices)
            cm = np.concatenate([[0], np.cumsum(mask0, dtype=np.int64)])
            lo = np.maximum(lo_ep, S + first0)
            ticks_last = np.maximum(0, N - lo)
            fired = np.where(ticks_last > 0, cm[N] - cm[np.minimum(lo, N)], 0)
            entry = np.where(~enough, NOT_CHECKED, np.where(N <= S + first0, NOT_CHECKED,
                             np.where(ticks_last == 0, NOT_CHECKED, np.where(fired > 0, PASS, FAIL))))
            stakes = an.leg_stakes(bot)
            stat_legs, econ_legs, pmargin = [], [], {}
            for leg in m.legs:
                mk, first = entry_mask(leg.entry, dg, prices)
                idx, _, w = settle(mk, dg, gap_ok, leg.duration, cfg.entry_gap_ticks, leg.win_digits)
                off = cfg.entry_gap_ticks + leg.duration - 1
                cw = np.concatenate([[0], np.cumsum(w, dtype=np.int64)])
                a = np.searchsorted(idx, S + first, side="left")
                b = np.searchsorted(idx, N - off, side="left")
                b = np.maximum(a, b)
                n = np.where(enough, b - a, 0)
                wins = np.where(enough, cw[b] - cw[a], 0)
                q = an.book.get(_key(sym, leg, stakes[leg.id]))
                st = np.empty(len(grid), dtype=object)
                ec = np.empty(len(grid), dtype=object)
                margin = np.full(len(grid), np.nan)
                for i in range(len(grid)):
                    ni, wi = int(n[i]), int(wins[i])
                    if not enough[i] or ni < cfg.min_outcomes:
                        st[i] = NOT_CHECKED
                    else:
                        _, hi = wilson(wi, ni, z2)
                        st[i] = FAIL if hi < leg.p_fair else PASS
                    if q is None or not q.ok:
                        ec[i] = E.UNKNOWN
                    elif ni < cfg.min_outcomes:
                        ec[i] = E.NOT_CHECKED
                    else:
                        plo, phi = wilson(wi, ni, z1)
                        evl, evh = E.ev(plo, q.payout, stakes[leg.id]), E.ev(phi, q.payout, stakes[leg.id])
                        ec[i] = E.POSITIVE if evl > 0 else E.NEGATIVE if evh < 0 else E.INCONCLUSIVE
                        margin[i] = plo - stakes[leg.id] / q.payout
                stat_legs.append(st)
                econ_legs.append(ec)
                pmargin[leg.id] = None if np.all(np.isnan(margin)) else float(np.nanmax(margin))
            stats = np.empty(len(grid), dtype=object)
            econ = np.empty(len(grid), dtype=object)
            for i in range(len(grid)):
                ss = [x[i] for x in stat_legs]
                stats[i] = FAIL if FAIL in ss else PASS if all(s == PASS for s in ss) else NOT_CHECKED
                es = [x[i] for x in econ_legs]
                econ[i] = (E.NEGATIVE if E.NEGATIVE in es else E.UNKNOWN if E.UNKNOWN in es else
                           E.POSITIVE if all(e == E.POSITIVE for e in es) else
                           E.NOT_CHECKED if E.NOT_CHECKED in es else E.INCONCLUSIVE)
            out[(bot, sym)] = {"entry": entry, "statistics": stats, "economics": econ, "live": live,
                               "enough": enough, "best_margin": pmargin}
    return out


def _key(sym, leg, stake):
    from app.economics.quotes import QuoteKey
    return QuoteKey(sym, leg.contract_type, leg.barrier, leg.duration, stake)


def decide(g: dict, i: int, risk: str, requires: tuple) -> tuple[str, bool, str | None, bool]:
    """(status, critical, critical_kind, hold) exactly as StrategyAnalyzer._evaluate_combo orders it."""
    if g["live"][i] == "FAULT":
        return AVOID, True, "data", False
    if risk in (R.UNSAFE, R.BLOCKED):
        return AVOID, True, "risk", False
    if not g["enough"][i]:
        return INSUFFICIENT, False, None, False
    passes = {"entry": g["entry"][i] == PASS, "statistics": g["statistics"][i] == PASS,
              "economics": g["economics"][i] == E.POSITIVE, "risk": risk == R.SAFE}
    if any(not passes[k] for k in requires):
        return WAIT, False, None, False
    if g["live"][i] != "OK":
        return WAIT, False, None, True
    return RUN, False, None, False


# ------------------------------------------------------------------ one scenario
def run_scenario(name, data, bots, models, grid, sample_every, bankroll, requires, tmp: Path) -> dict:
    t0 = time.time()
    dq = Section(DEFAULTS["data_quality"])
    cfg = EvalConfig(start_requires=requires, max_quote_age_s=1e12)
    conn = init_db(tmp / f"{name}.db")
    book = QuoteBook(conn)
    cache = SliceCache(data, cfg.long_window_ticks)
    rs = Section({**DEFAULTS["risk"], "bankroll_usd": bankroll})
    an = StrategyAnalyzer(bots, models, cache, cfg, book, rs)
    combos = [(b, s) for b in bots for s in SYMBOLS]
    quote_time = float(grid[0]) - 1
    for _ in range(500):                                  # answer every quote the evaluation asks for
        todo = {n.key.db: n.key for n in an.quote_needs(combos) if book.get(n.key) is None}
        if not todo:
            break
        for k in todo.values():
            book.put(model_quote(k, quote_time))
    markets = {s: MarketInfo(s, display_name(s), "synthetic_index", "random_index", data[s]["decimals"], True, False)
               for s in SYMBOLS}
    compat = {(b, s): CompatResult(b, s, True, [], {leg.id: "ok" for leg in models[b].legs}) for b, s in combos}
    nominal = {s: nominal_of(data[s]["epochs"]) for s in SYMBOLS}

    # 3. the real evaluator at sample times
    samples = grid[::sample_every]
    real = {}
    risk = {}
    cycle_s = []
    for T in samples:
        cache.T = float(T)
        live = {}
        for s in SYMBOLS:
            _, N = cache.bounds(s, float(T))
            live[s] = live_status(data[s]["epochs"], N, float(T), nominal[s], dq)
        t_eval = time.perf_counter()
        res = an.evaluate(combos, compat, markets, live, float(T))
        cycle_s.append(time.perf_counter() - t_eval)
        for k, v in res.verdicts.items():
            real[(k, float(T))] = v.status_tuple()
            risk[k] = v.risk
    # 4. per-minute emulation, checked against 3
    emu = emulate(data, bots, models, an, cfg, grid, dq)
    agree, disagree = 0, []
    pos = {float(t): i for i, t in enumerate(grid)}
    for (k, T), tup in real.items():
        g = emu[k]
        i = pos[T]
        st, _, _, _ = decide(g, i, risk[k].status, requires)
        mine = (st, g["entry"][i], g["statistics"][i], g["economics"][i], risk[k].status)
        if mine == tup:
            agree += 1
        else:
            disagree.append({"bot": k[0], "symbol": k[1], "time": int(T), "evaluator": tup, "emulation": mine})
    # the real SignalEngine over every minute
    fmt = MessageFormatter(bots, "UTC")
    eng = SignalEngine(conn, SignalStateStore(conn), fmt, Section(DEFAULTS["signals"]), started_at=float(grid[0]))
    starts, stops, crit_stops = Counter(), Counter(), Counter()
    status_counts = defaultdict(Counter)
    for i, T in enumerate(grid):
        verdicts = {}
        for k in combos:
            st, crit, kind, hold = decide(emu[k], i, risk[k].status, requires)
            status_counts[k[0]][st] += 1
            verdicts[k] = StrategyVerdict(k[0], bots[k[0]].name, k[1], k[1], status=st, critical=crit,
                                          critical_kind=kind, hold=hold, reasons=[] if st == RUN else [st])
        for t in eng.process(verdicts, float(T)):
            if t.kind == "START":
                starts[t.bot] += 1
            else:
                stops[t.bot] += 1
                crit_stops[t.bot] += int(t.critical)
    limit_hits = conn.execute("SELECT bot, COUNT(*) FROM signal_events WHERE kind='SIGNAL_LIMIT_REACHED' GROUP BY bot"
                              ).fetchall()
    # gate pass shares per bot (minutes x markets with enough data)
    shares = {}
    for b in bots:
        tot, c = 0, Counter()
        for s in SYMBOLS:
            g = emu[(b, s)]
            e = g["enough"]
            tot += int(e.sum())
            c["entry PASS"] += int(((g["entry"] == PASS) & e).sum())
            c["statistics PASS"] += int(((g["statistics"] == PASS) & e).sum())
            c["statistics FAIL"] += int(((g["statistics"] == FAIL) & e).sum())
            c["economics POSITIVE"] += int(((g["economics"] == E.POSITIVE) & e).sum())
            c["economics NEGATIVE"] += int(((g["economics"] == E.NEGATIVE) & e).sum())
            c["economics INCONCLUSIVE"] += int(((g["economics"] == E.INCONCLUSIVE) & e).sum())
            c["economics NOT_CHECKED"] += int(((g["economics"] == E.NOT_CHECKED) & e).sum())
        shares[b] = {"minute_market_checks": tot, **{k: round(v / tot, 4) if tot else None for k, v in c.items()},
                     "counts": dict(c)}
    best_margin = {f"{b} {s}": emu[(b, s)]["best_margin"] for b in bots for s in SYMBOLS}
    positive_minutes = {}
    for (b, s), g in emu.items():
        pos_e = int(((g["economics"] == E.POSITIVE) & g["enough"]).sum())
        all_pass = int(((g["economics"] == E.POSITIVE) & (g["entry"] == PASS) & (g["statistics"] == PASS)
                        & g["enough"]).sum())
        if pos_e:
            positive_minutes[f"{b} {s}"] = {"economics_positive_minutes": pos_e,
                                            "entry_statistics_economics_all_pass_minutes": all_pass,
                                            "risk": risk[(b, s)].status}
    risk_out = {}
    for b in bots:
        r = risk[(b, SYMBOLS[0])]
        risk_out[b] = {"status": r.status, "reason": r.reason, "next_stake": r.next_stake,
                       "max_losses_before_stop": r.max_losses_before_stop, "worst_loss": r.worst_loss,
                       "required_bankroll": r.required_bankroll,
                       "same_on_all_markets": len({risk[(b, s)].status for s in SYMBOLS}) == 1}
    conn.close()
    return {"scenario": name, "bankroll": bankroll, "start_requires": list(requires), "minutes": len(grid),
            "samples": len(samples), "evaluator_vs_emulation": {"agree": agree, "total": len(real),
                                                                "disagree": disagree[:20]},
            "starts": dict(starts), "starts_total": sum(starts.values()), "stops": dict(stops),
            "critical_stops": dict(crit_stops), "limit_reached_events": {r[0]: r[1] for r in limit_hits},
            "decision_minutes": {b: dict(c) for b, c in status_counts.items()}, "gate_shares": shares,
            "risk": risk_out, "best_economics_margin": best_margin, "economics_positive": positive_minutes,
            "evaluator_cycle_seconds": {"cycles": len(cycle_s), "mean": round(float(np.mean(cycle_s)), 3),
                                        "max": round(float(np.max(cycle_s)), 3), "combinations": len(combos),
                                        "long_window_ticks": cfg.long_window_ticks},
            "seconds": round(time.time() - t0, 1)}


# ------------------------------------------------------------------ historical outcomes (7 days)
def outcomes(data, bots, models, an_stakes) -> dict:
    out = {}
    for sym in SYMBOLS:
        d = data[sym]
        prices = prices_as_doubles(d["prices"], d["decimals"])
        gap_ok = gap_ok_steps(d["epochs"], 10 * max(nominal_of(d["epochs"]), 1.0))
        for b in bots:
            for leg in models[b].legs:
                mk, _ = entry_mask(leg.entry, d["digits"], prices)
                idx, _, w = settle(mk, d["digits"], gap_ok, leg.duration, 1, leg.win_digits)
                n, wins = len(idx), int(w.sum())
                stake = an_stakes[b][leg.id]
                payout = E.model_payout(stake, leg.p_fair)
                out[f"{b} {leg.id} {sym}"] = {
                    "contract": leg.label, "entries": n, "wins": wins, "win_rate": wins / n if n else None,
                    "p_fair": leg.p_fair, "stake": stake, "model_payout": payout, "break_even": stake / payout,
                    "flat_stake_pnl": round(wins * (payout - stake) - (n - wins) * stake, 2),
                    "ev_per_trade_at_fair_p": round(E.ev(leg.p_fair, payout, stake), 4)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ticks", required=True, type=Path)
    ap.add_argument("--results", type=Path, help="results.json of the 7-day Node backtest (parity, comparison)")
    ap.add_argument("--live-quotes", type=Path, help="meta_payouts.json (live quotes for the payout-model check)")
    ap.add_argument("--sample-minutes", type=int, default=60, help="real-evaluator sample spacing")
    ap.add_argument("--bankroll", type=float, default=1000.0, help="bankroll of the 'configured' scenario")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    t0 = time.time()
    bots = {b.key: b for b in load_strategies(ROOT / "strategies")}
    models = load_models(list(bots.values()))
    data = {s: load_ticks(args.ticks / f"{s}.csv.gz") for s in SYMBOLS}
    report = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ticks_dir": str(args.ticks),
              "data": {s: {"ticks": int(len(d["epochs"])), "first": int(d["epochs"][0]), "last": int(d["epochs"][-1]),
                           "decimals": d["decimals"], "digit_column_mismatch": d["digit_column_mismatch"]}
                       for s, d in data.items()}}
    results = json.loads(args.results.read_text(encoding="utf-8")) if args.results else None
    if results:
        report["parity"] = parity(data, models, results)
        print(f"parity with the Node backtest: {report['parity']['identical']} / {report['parity']['total']} identical")
    if args.live_quotes:
        report["payout_model_vs_live_quotes"] = check_live_quotes(args.live_quotes)
        pm = report["payout_model_vs_live_quotes"]
        print(f"payout model vs saved live quotes: {pm['match']} / {pm['checked']} identical")
    start = max(int(d["epochs"][0]) for d in data.values()) + 1000
    end = min(int(d["epochs"][-1]) for d in data.values())
    grid = np.arange(start - start % 60 + 60, end + 1, 60, dtype=np.float64)
    every = max(1, int(args.sample_minutes))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        scen = [("default_config", None, ("entry", "statistics", "economics", "risk")),
                ("bankroll_configured", args.bankroll, ("entry", "statistics", "economics", "risk")),
                ("sensitivity_without_economics_gate", args.bankroll, ("entry", "statistics", "risk"))]
        report["scenarios"] = []
        for name, bank, req in scen:
            r = run_scenario(name, data, bots, models, grid, every, bank, req, tmp)
            report["scenarios"].append(r)
            ev = r["evaluator_vs_emulation"]
            print(f"{name}: STARTs {r['starts_total']} {r['starts']} · evaluator vs emulation {ev['agree']}/{ev['total']}"
                  f" · {r['seconds']} s")
        sconn = init_db(tmp / "s.db")
        an = StrategyAnalyzer(bots, models, None, EvalConfig(), QuoteBook(sconn), Section(DEFAULTS["risk"]))
        report["outcomes_7d"] = outcomes(data, bots, models, {b: an.leg_stakes(b) for b in bots})
        sconn.close()
    if results:
        report["old_analyzer_emulation"] = {b: {"starts": v.get("starts"), "limitedCycles": v.get("limitedCycles")}
                                            for b, v in results.get("analyzer", {}).items()}
        report["backtest_7d"] = {b: {s: {k: v["full"].get(k) for k in ("trades", "winRate", "netProfit", "bust",
                                                                          "maxConsecLosses", "requiredBankroll")}
                                     for s, v in runs.items()} for b, runs in results.get("runs", {}).items()}
    report["seconds"] = round(time.time() - t0, 1)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

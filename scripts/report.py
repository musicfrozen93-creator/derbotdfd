#!/usr/bin/env python3
"""Read-only console report: heartbeat, compatibility matrix, the bot-specific verdict of every
bot x market (each check shown separately), active signals, quotes and recent notifications.

    python scripts/report.py            # full report
    python scripts/report.py --brief    # short status
    python scripts/report.py --matrix   # compatibility matrix only
    python scripts/report.py --bot aria --symbol 1HZ10V   # one combination in detail (legs, payout, risk ladder)
    python scripts/report.py --contracts                  # legacy digit rule per contract (informational)
    python scripts/report.py --quotes                     # stored Deriv quotes, platform limits, payout check
    python scripts/report.py --history                    # recent changes of the verdicts

Every check has its own status, so one green item never hides another:
  feed (market data) · entry (the bot's own entry rule) · statistics (its contract outcomes) ·
  economics (quoted payout -> expected value) · risk (its recovery sequence vs payout cap / bankroll)
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.monitoring.heartbeat import read_heartbeat  # noqa: E402
from app.storage import repositories as repo  # noqa: E402
from app.storage.database import connect  # noqa: E402

RECOVERY_NOTE = ("current recovery level: not observable (the analyzer is not connected to the running DBot); "
                 "the losing streak below starts from a fresh run")


def contracts_text(sm: dict) -> str:
    """Legacy summary (rows written before the strategy alignment)."""
    out = []
    for c in sm.get("contracts") or []:
        fail = c.get("failing_digits") or []
        out.append(f"{c['label']}={c['result']}" + (f"[{','.join(map(str, fail))}]" if fail else ""))
    total = sm.get("contracts_total", len(out))
    return f"{sm.get('contracts_passing', 0)}/{total} pass · " + "  ".join(out)


def gate(sm: dict, name: str) -> str:
    g = (sm.get("gates") or {}).get(name) or {}
    st = g.get("status", "n/a")
    mark = "" if g.get("pass") else ("!" if g.get("required") else "")
    return f"{st}{mark}"


def checks_text(sm: dict) -> str:
    """One line per combination, every check separately."""
    if sm.get("model") != "strategy_v1":
        return "[legacy rule] " + contracts_text(sm)
    feed = (sm.get("quality") or {}).get("status", "n/a")
    return (f"feed={feed} entry={gate(sm, 'entry')} statistics={gate(sm, 'statistics')} "
            f"economics={gate(sm, 'economics')} risk={gate(sm, 'risk')}")


def money(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.2f}%"


def leg_detail(lg: dict, conf: float) -> list[str]:
    bar = "-" if lg.get("barrier") is None else lg["barrier"]
    out = [f"      leg {lg['leg_id']:<9} {lg['contract_type']:<10} barrier {bar}  {lg['duration']}t  "
           f"stake {money(lg.get('stake'))}  used: {lg.get('used_when')}",
           f"          entry rule: {lg.get('entry_rule')}"]
    lo, re_ = lg.get("long") or {}, lg.get("recent") or {}
    if lo:
        out.append(f"          statistics {lg.get('statistics_status')}: long window {lo.get('window_ticks', 0):,} ticks: "
                   f"{lo.get('wins', 0):,}/{lo.get('settled', 0):,} = {pct(lo.get('win_rate'))} "
                   f"({conf:.0%} CI {pct(lo.get('ci_low'))} - {pct(lo.get('ci_high'))}) vs fair {pct(lo.get('p_fair'))}")
    else:
        out.append(f"          statistics {lg.get('statistics_status')}: {lg.get('statistics_detail')}")
    if re_:
        out.append(f"          recent window {re_.get('window_ticks', 0):,} ticks: {re_.get('wins', 0)}/"
                   f"{re_.get('settled', 0)} = {pct(re_.get('win_rate'))} (shown only; overlapping windows are not "
                   f"independent)")
    e = lg.get("economics") or {}
    if e.get("payout") is None:
        out.append(f"          payout: not available -> expected value {e.get('status', 'UNKNOWN')}: {e.get('detail')}")
    else:
        out.append(f"          payout {money(e['payout'])} for {money(e.get('stake'))}: net win +{money(e.get('net_win'))}, "
                   f"net loss -{money(e.get('net_loss'))}, break-even {pct(e.get('break_even'))}, "
                   f"quote age {int(e.get('quote_age_s') or 0)} s")
        if e.get("ev_hat") is not None:
            out.append(f"          expected value {e.get('status')}: {e['ev_hat']:+.4f} per trade "
                       f"(range {e['ev_low']:+.4f} to {e['ev_high']:+.4f}; at fair odds {e.get('ev_fair', 0):+.4f})")
        else:
            out.append(f"          expected value {e.get('status')}: {e.get('detail')}")
    lr = lg.get("legacy_rule") or {}
    if lr.get("result"):
        fail = lr.get("failing_digits") or []
        out.append(f"          legacy digit rule (informational, not a gate): {lr['result']}"
                   + (f" [failing {','.join(map(str, fail))}]" if fail else ""))
    return out


def risk_detail(r: dict) -> list[str]:
    if not r:
        return ["      risk: not evaluated"]
    out = [f"      risk {r.get('status')}: {r.get('reason')}",
           f"      {RECOVERY_NOTE}",
           f"      next trade: {r.get('next_contract')} at {money(r.get('next_stake'))} (quoted payout "
           f"{money(r.get('next_payout'))})",
           f"      losses until the bot stops: {r.get('max_losses_before_stop')}, worst cumulative loss "
           f"-{money(r.get('worst_loss'))}, bankroll needed {money(r.get('required_bankroll'))} "
           f"(configured {money(r.get('bankroll'))})",
           f"      limits: {r.get('limits') or 'none observed yet'} — {r.get('context')}",
           f"      after a refused contract: {r.get('can_continue_after_rejection')}"]
    for lad in r.get("ladders") or []:
        out.append(f"      streak [{lad.get('variant')}] {lad.get('status')} ({lad.get('stop_kind')}): {lad.get('reason')}")
        for st in lad.get("steps") or []:
            ok = {True: "accepted", False: "REFUSED", None: "unknown"}[st.get("executable")]
            out.append(f"         loss #{st['k'] + 1:<3} {st['contract']:<16} stake {money(st['stake']):>10}  "
                       f"lost before -{money(st['cum_loss_before']):>10}  payout {money(st.get('payout')):>10}  {ok}"
                       + ("" if st.get("executable") else f" — {st.get('reason')}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--matrix", action="store_true")
    ap.add_argument("--bot")
    ap.add_argument("--symbol")
    ap.add_argument("--contracts", action="store_true", help="legacy digit rule per contract (informational)")
    ap.add_argument("--quotes", action="store_true", help="stored Deriv quotes, platform limits, payout formula check")
    ap.add_argument("--history", action="store_true", help="recent verdict changes")
    args = ap.parse_args()
    s = load_settings()
    db = s.path(s.storage.db_path)
    if not db.exists():
        print(f"no database at {db}")
        return 1
    conn = connect(db)
    now = time.time()

    hb = read_heartbeat(s.path(s.heartbeat.file))
    if hb:
        age = int(now - hb.get("written_at", 0))
        lt = hb.get("last_tick_wall") or 0
        print(f"heartbeat: {age}s ago · connected={hb.get('connected')} · last tick {int(now - lt) if lt else 'n/a'}s ago"
              f" · symbols={len(hb.get('monitored_symbols', []))} · backfill queue={hb.get('backfill_queue')}")
        print(f"decisions: {hb.get('status_counts')}")
        for g, d in (hb.get("gate_counts") or {}).items():
            print(f"  {g:<10} {d}")
        if hb.get("quotes"):
            print(f"quotes: {hb.get('quotes')}")
        print(f"active signals: {', '.join(hb.get('active_signals') or []) or 'none'}")
    else:
        print("heartbeat: none (service not running?)")

    if args.matrix or not args.brief:
        print("\nCompatibility matrix (bot × market):")
        rows = conn.execute("SELECT bot, symbol, compatible, reasons FROM compatibility ORDER BY bot, symbol").fetchall()
        by_bot = {}
        for r in rows:
            by_bot.setdefault(r["bot"], []).append(r)
        for bot, rs in by_bot.items():
            ok = [r["symbol"] for r in rs if r["compatible"]]
            print(f"  {bot:<12} compatible on {len(ok):>2}: {', '.join(ok)}")
        if args.matrix:
            return 0

    q = "SELECT bot, symbol, status, evaluated_at, summary FROM evaluations"
    params = []
    if args.bot or args.symbol:
        q += " WHERE 1=1"
        if args.bot:
            q += " AND bot=?"
            params.append(args.bot)
        if args.symbol:
            q += " AND symbol=?"
            params.append(args.symbol)
    q += " ORDER BY status, bot, symbol"
    evals = conn.execute(q, params).fetchall()
    counts = {}
    for r in evals:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"\nEvaluations ({len(evals)} combinations): {counts}")
    print("  (a '!' marks a check that is required for START and is not passing)")
    if not args.brief:
        for r in evals:
            sm = json.loads(r["summary"])
            print(f"  {r['status']:<17} {r['bot']:<12} {r['symbol']:<8} {checks_text(sm)}")
            if not (args.bot or args.symbol):
                continue
            for reason_line in sm.get("reasons") or []:
                print(f"      reason: {reason_line}")
            qd = sm.get("quality") or {}
            n = qd.get("n_ticks") or 0
            print(f"      market condition (live feed): {qd.get('status')} — {qd.get('detail')} · recent window {n:,} ticks")
            if sm.get("model") == "strategy_v1":
                en = sm.get("entry") or {}
                print(f"      entry {en.get('status')}: {en.get('detail')}")
                print(f"      statistics {sm.get('statistics_status')} · economics {sm.get('economics_status')}: "
                      f"{sm.get('economics_detail')}")
                for lg in sm.get("legs") or []:
                    for line in leg_detail(lg, float(sm.get("confidence") or 0)):
                        print(line)
                for line in risk_detail(sm.get("risk") or {}):
                    print(line)
            counts_ = qd.get("counts") or []
            if n:
                print("      all digits (recent window): " + "  ".join(f"{d}:{100 * c / n:.2f}%"
                                                                 for d, c in enumerate(counts_)))

    if args.contracts:
        try:
            rows = repo.load_contract_results(conn, args.bot, args.symbol)
        except Exception:                      # database created before contract_results existed
            rows = []
        tally = {}
        for r in rows:
            tally[r["result"]] = tally.get(r["result"], 0) + 1
        print(f"\nLegacy digit rule per contract — informational only, gates nothing ({len(rows)} contracts): {tally}")
        print(f"  {'bot':<12} {'market':<8} {'contract':<10} {'type':<10} {'bar':>3}  {'winning':<18} "
              f"{'result':<11} decision")
        for r in rows:
            bar = "-" if r["barrier"] is None else r["barrier"]
            print(f"  {r['bot']:<12} {r['symbol']:<8} {r['leg_id']:<10} {r['contract_type']:<10} {bar:>3}  "
                  f"{r['winning_digits']:<18} {r['result']:<11} {r['bot_status']}")

    if args.quotes:
        try:
            qs = conn.execute("SELECT * FROM quotes ORDER BY symbol, contract_type, barrier, duration,"
                              " CAST(stake AS REAL)").fetchall()
            lim = conn.execute("SELECT * FROM platform_limits ORDER BY symbol, contract_type").fetchall()
        except Exception:
            qs, lim = [], []
        print(f"\nStored Deriv quotes: {len(qs)}")
        for r in qs:
            what = (f"payout {r['payout']}" if r["ok"] else f"{r['error_code']}: {(r['error_message'] or '')[:90]}")
            print(f"  {r['symbol']:<8} {r['contract_type']:<10} {r['barrier'] or '-':>2} {r['duration']}t stake "
                  f"{r['stake']:>9}  {what}  ({int(now - r['fetched_at'])} s old)")
        print(f"Platform limits observed ({len(lim)}):")
        for r in lim:
            print(f"  {r['symbol']:<8} {r['contract_type']:<10} max payout {r['max_payout']} min stake {r['min_stake']}"
                  f" — {r['context']}")
        try:
            from app.economics.ev import validate_model
            from app.economics.quotes import QuoteBook
            from app.strategy_model.bots import win_digits
            book = QuoteBook(conn)
            book.load()
            res = validate_model(list(book.quotes.values()),
                                 lambda k: len(win_digits(k.contract_type, k.barrier)) / 10)
            print(f"Payout formula cross-check (reporting only, never used instead of a quote): {res['status']}, "
                  f"{res['checked']} quotes checked, {res['mismatch_count']} mismatches")
        except Exception as exc:  # pragma: no cover - report must not fail
            print(f"Payout formula cross-check: not available ({type(exc).__name__})")

    if args.history:
        try:
            hist = conn.execute("SELECT * FROM evaluation_history ORDER BY id DESC LIMIT 40").fetchall()
        except Exception:
            hist = []
        print(f"\nRecent verdict changes ({len(hist)}):")
        for h in hist:
            print(f"  {time.strftime('%m-%d %H:%M:%S', time.gmtime(h['evaluated_at']))} {h['bot']:<12} {h['symbol']:<8} "
                  f"{h['decision']:<17} entry={h['entry_status']} statistics={h['statistics_status']} "
                  f"economics={h['economics_status']} risk={h['risk_status']}")

    act = conn.execute("SELECT bot, symbol, last_start_at FROM signal_state WHERE active=1").fetchall()
    print(f"\nActive START signals: {len(act)}")
    for r in act:
        print(f"  {r['bot']} on {r['symbol']} since {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(r['last_start_at']))}")

    if not args.brief:
        notes = conn.execute("SELECT id, kind, status, created_at FROM notifications ORDER BY id DESC LIMIT 10").fetchall()
        print("\nRecent notifications:")
        for n in notes:
            print(f"  #{n['id']} {n['kind']:<9} {n['status']:<8} {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(n['created_at']))}")
        ticks = conn.execute("SELECT symbol, COUNT(*) c, MIN(epoch) a, MAX(epoch) b FROM ticks GROUP BY symbol").fetchall()
        print("\nStored ticks:")
        for t in ticks:
            print(f"  {t['symbol']:<9} {t['c']:>9,}  {time.strftime('%m-%d %H:%M', time.gmtime(t['a']))} → "
                  f"{time.strftime('%m-%d %H:%M', time.gmtime(t['b']))} UTC")
    return 0


if __name__ == "__main__":
    sys.exit(main())

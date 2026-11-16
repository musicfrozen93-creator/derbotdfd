#!/usr/bin/env python3
"""Read-only console report: heartbeat, compatibility matrix, current verdicts,
active signals, payouts and recent notifications. No dashboard needed.

    python scripts/report.py            # full report
    python scripts/report.py --brief    # short status
    python scripts/report.py --matrix   # compatibility matrix only
    python scripts/report.py --bot aria --symbol 1HZ10V   # one combination in detail
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.monitoring.heartbeat import read_heartbeat  # noqa: E402
from app.storage.database import connect  # noqa: E402


def pct(x):
    return "   n/a" if x is None else f"{100 * x:6.2f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--matrix", action="store_true")
    ap.add_argument("--bot")
    ap.add_argument("--symbol")
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
        print(f"status counts: {hb.get('status_counts')}")
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
    if not args.brief:
        for r in evals:
            sm = json.loads(r["summary"])
            reason = (sm.get("reasons") or [""])[0]
            print(f"  {r['status']:<17} {r['bot']:<12} {r['symbol']:<8} {reason[:90]}")
            if args.bot or args.symbol:
                for lv in sm.get("legs", []):
                    st = lv["stats"]
                    be = (lv.get("payout") or {}).get("break_even")
                    print(f"      leg {lv['leg']:<9} {lv['label']:<13} {lv['duration']}t  events={st['n_events']:>8,}"
                          f"  WR={pct(st['wr'])}  CI=[{pct(st['ci_lo'])},{pct(st['ci_hi'])}]  p0={pct(st['p0'])}"
                          f"  BE={pct(be)}  OOS={pct(st['oos_wr'])}  q={st['q_value']}")
                    for k, v in lv["checks"].items():
                        print(f"          {k:<30} {v}")
                qd = sm.get("quality") or {}
                print(f"      data quality: {qd.get('status')} — {qd.get('detail')} · ticks={qd.get('n_ticks'):,}"
                      f" · chi² p={qd.get('chi2_p')}")
                if sm.get("sim"):
                    print(f"      replay simulation: {sm['sim']}")

    act = conn.execute("SELECT bot, symbol, last_start_at FROM signal_state WHERE active=1").fetchall()
    print(f"\nActive START signals: {len(act)}")
    for r in act:
        print(f"  {r['bot']} on {r['symbol']} since {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(r['last_start_at']))}")

    if not args.brief:
        pays = conn.execute("SELECT * FROM payouts ORDER BY symbol, contract_type, barrier").fetchall()
        print(f"\nPayout quotes: {len(pays)}")
        for p in pays[:200]:
            age = int(now - (p["fetched_at"] or 0))
            be = p["break_even"]
            print(f"  {p['symbol']:<8} {p['contract_type']:<10} {p['barrier']:>2} {p['duration']}t  stake {p['stake']}"
                  f" payout {p['payout']}  break-even {pct(be)}  age {age}s {('ERR ' + p['error']) if p['error'] else ''}")
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

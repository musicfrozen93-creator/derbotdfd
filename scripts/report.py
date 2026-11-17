#!/usr/bin/env python3
"""Read-only console report: heartbeat, compatibility matrix, current verdicts with every
contract's own PASS / FAIL, active signals, digit percentages and recent notifications.

    python scripts/report.py            # full report
    python scripts/report.py --brief    # short status
    python scripts/report.py --matrix   # compatibility matrix only
    python scripts/report.py --bot aria --symbol 1HZ10V   # one combination in detail
    python scripts/report.py --contracts                  # one line per contract (contract_results table)
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


def contracts_text(sm: dict) -> str:
    """One line per combination: every contract with its own PASS / FAIL and failing digits."""
    out = []
    for c in sm.get("contracts") or []:
        fail = c.get("failing_digits") or []
        out.append(f"{c['label']}={c['result']}" + (f"[{','.join(map(str, fail))}]" if fail else ""))
    total = sm.get("contracts_total", len(out))
    return f"{sm.get('contracts_passing', 0)}/{total} pass · " + "  ".join(out)


def contract_detail(c: dict, threshold) -> list[str]:
    pc = c.get("digit_pct") or {}
    fail = set(c.get("failing_digits") or [])
    digits = "  ".join(f"{d}:{pc[str(d)]:.2f}%{'✗' if d in fail else ''}" for d in c["winning_digits"]
                       if str(d) in pc)
    barrier = "-" if c.get("barrier") is None else c["barrier"]
    return [f"      contract {c['leg_id']:<9} {c['contract_type']:<10} barrier {barrier}  {c['duration']}t"
            f"  -> {c['result']}",
            f"          winning digits {','.join(map(str, c['winning_digits']))}: {digits or 'n/a'}",
            f"          {c.get('detail') or ''}"]



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--matrix", action="store_true")
    ap.add_argument("--bot")
    ap.add_argument("--symbol")
    ap.add_argument("--contracts", action="store_true", help="list every contract's latest result")
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
            print(f"  {r['status']:<17} {r['bot']:<12} {r['symbol']:<8} {contracts_text(sm)}")
            if args.bot or args.symbol:
                rule = sm.get("rule") or {}
                print(f"      rule: each contract on its own, every winning digit > {rule.get('threshold_pct')}% "
                      f"over the last {rule.get('window_ticks')} ticks; every contract must pass")
                for reason_line in sm.get("reasons") or []:
                    print(f"      {reason_line}")
                for c in sm.get("contracts", []):
                    for line in contract_detail(c, rule.get("threshold_pct")):
                        print(line)
                qd = sm.get("quality") or {}
                counts = qd.get("counts") or []
                n = qd.get("n_ticks") or 0
                if n:
                    print("      all digits: " + "  ".join(f"{d}:{100 * c / n:.2f}%" for d, c in enumerate(counts)))
                print(f"      live feed: {qd.get('status')} — {qd.get('detail')} · window ticks={n:,}")

    if args.contracts:
        try:
            rows = repo.load_contract_results(conn, args.bot, args.symbol)
        except Exception:                      # database created before contract_results existed
            rows = []
        tally = {}
        for r in rows:
            tally[r["result"]] = tally.get(r["result"], 0) + 1
        print(f"\nContract results ({len(rows)} contracts): {tally}")
        print(f"  {'bot':<12} {'market':<8} {'contract':<10} {'type':<10} {'bar':>3}  {'winning':<18} "
              f"{'result':<11} detail")
        for r in rows:
            bar = "-" if r["barrier"] is None else r["barrier"]
            print(f"  {r['bot']:<12} {r['symbol']:<8} {r['leg_id']:<10} {r['contract_type']:<10} {bar:>3}  "
                  f"{r['winning_digits']:<18} {r['result']:<11} {r['detail']}")

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

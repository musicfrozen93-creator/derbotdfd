#!/usr/bin/env python3
"""Live feasibility check against the Deriv public WebSocket (Phase B).

Exercises every call the analyzer depends on and prints PASS/FAIL per item.
Read-only: proposals are price quotes; nothing is bought.

    python scripts/live_api_check.py [--symbols R_100 R_10 1HZ10V 1HZ50V]
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics.payout import fetch_quote  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.deriv.client import DerivClient  # noqa: E402
from app.deriv.contracts import fetch_contracts  # noqa: E402
from app.deriv.history import fetch_page, page_rows  # noqa: E402
from app.deriv.symbols import fetch_active_symbols  # noqa: E402
from app.deriv.ticks import parse_tick  # noqa: E402
from app.market.precision import format_quote  # noqa: E402

results = []


def report(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


async def run(symbols):
    s = load_settings()
    client = DerivClient(s.deriv.ws_url)
    ticks = {}

    def on_tick(t):
        rt = parse_tick(t)
        ticks.setdefault(rt.symbol, []).append(rt)

    client.on_tick = on_tick
    runner = asyncio.create_task(client.run())
    try:
        await client.wait_connected(30)
        report("connect", True, s.deriv.ws_url)
        r = await client.request({"ping": 1})
        report("ping", r.get("ping") == "pong")
        markets = await fetch_active_symbols(client)
        syn = [m for m in markets if m.market == "synthetic_index"]
        report("active_symbols", bool(syn), f"{len(markets)} symbols, {len(syn)} synthetic")
        mm = {m.symbol: m for m in markets}
        for sym in symbols:
            m = mm.get(sym)
            if not m:
                report(f"{sym} listed", False)
                continue
            sc = await fetch_contracts(client, sym)
            digit = {k: (v.min_ticks, v.max_ticks, v.digit_range) for k, v in sc.contracts.items() if k.startswith("DIGIT")}
            report(f"contracts_for {sym}", "DIGITOVER" in digit and "DIGITUNDER" in digit,
                   ", ".join(f"{k} {v[0]}-{v[1]}t" for k, v in sorted(digit.items())))
            page = await fetch_page(client, sym, count=1000)
            rows = page_rows(page, m.pip_decimals)
            lens = {len(r[2].split('.')[-1]) if '.' in r[2] else 0 for r in rows}
            report(f"ticks_history {sym}", len(rows) > 0 and lens == {m.pip_decimals},
                   f"{len(rows)} ticks, pip decimals {m.pip_decimals}, formatted decimals {sorted(lens)}, "
                   f"page pip_size={page.pip_decimals}")
            q = await fetch_quote(client, sym, "DIGITOVER", 3, 1, 10, "USD")
            report(f"proposal (public, quote only) {sym}", q.break_even is not None,
                   f"DIGITOVER 3 1t stake {q.stake} payout {q.payout} break-even {q.break_even:.4f}")
            await client.subscribe_ticks(sym)
        await asyncio.sleep(12)
        for sym in symbols:
            got = ticks.get(sym, [])
            if got:
                t = got[-1]
                dec = mm[sym].pip_decimals
                report(f"live ticks {sym}", True, f"{len(got)} ticks in ~12s, last {format_quote(t.quote, dec)}")
            else:
                report(f"live ticks {sym}", False)
        # reconnect: force-close the socket and verify the client reconnects by itself
        before = client.connect_count
        await client._ws.close()
        t0 = time.time()
        while client.connect_count == before and time.time() - t0 < 30:
            await asyncio.sleep(0.5)
        report("automatic reconnect", client.connect_count > before, f"{time.time() - t0:.1f}s")
    finally:
        await client.stop()
        runner.cancel()
        try:
            await runner
        except BaseException:
            pass
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=["R_100", "R_10", "1HZ10V", "1HZ50V"])
    a = ap.parse_args()
    return asyncio.run(run(a.symbols))


if __name__ == "__main__":
    sys.exit(main())

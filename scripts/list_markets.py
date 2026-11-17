#!/usr/bin/env python3
"""Show which Deriv markets the market filter allows and which it excludes (read-only).

Calls only the public ``active_symbols`` endpoint (no token, no contracts, nothing bought) and
applies the same rules the analyzer uses (config.yaml -> discovery).

    python scripts/list_markets.py              # allowed + excluded synthetic indices
    python scripts/list_markets.py --all        # also list every excluded non-synthetic market

In Docker:  docker exec deriv-analyzer python scripts/list_markets.py
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.deriv.client import DerivClient  # noqa: E402
from app.deriv.symbols import classify_market, fetch_active_symbols, submarket_label  # noqa: E402


async def fetch(url: str):
    client = DerivClient(url)
    runner = asyncio.create_task(client.run())
    try:
        await client.wait_connected(30)
        return await fetch_active_symbols(client)
    finally:
        await client.stop()
        runner.cancel()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="List markets allowed / excluded by the market filter")
    ap.add_argument("--all", action="store_true", help="also list excluded non-synthetic markets one by one")
    args = ap.parse_args(argv)
    s = load_settings()
    d = s.discovery
    rules = dict(include_markets=list(d.markets), include_submarkets=list(d.include_submarkets),
                 exclude_symbols=list(d.exclude_symbols), one_second_symbols=list(d.one_second_symbols))
    listed = asyncio.run(fetch(s.deriv.ws_url))
    allowed, excluded = [], {}
    for m in listed:
        ok, why = classify_market(m, **rules)
        if ok:
            allowed.append(m)
        else:
            excluded.setdefault((m.market, m.submarket), []).append((m, why))
    print(f"Listed by Deriv: {len(listed)} markets\n")
    print(f"ALLOWED ({len(allowed)}):")
    for m in sorted(allowed, key=lambda m: m.symbol):
        print(f"  {m.symbol:<10} {m.display_name:<28} {submarket_label(m.submarket)} ({m.submarket})")
    print(f"\nEXCLUDED ({sum(len(v) for v in excluded.values())}):")
    for (market, sub), items in sorted(excluded.items()):
        syms = ", ".join(sorted(m.symbol for m, _ in items))
        if market != "synthetic_index" and not args.all:
            print(f"  {market}/{sub}: {len(items)} markets (use --all to list)")
            continue
        print(f"  {submarket_label(sub)} ({market}/{sub}): {syms}")
        print(f"      reason: {items[0][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

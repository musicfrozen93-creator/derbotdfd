# Phase B — Deriv API feasibility (tested live, 2026-11-16)

These checks were run against the real Deriv public WebSocket from a Windows workstation
with no API token, using `scripts/live_api_check.py` (20/20 passed) and two probe scripts.
The full application was then run in dry-run mode against the live feed (see the end of this file).
Re-run the check on the VPS after installing:

```bash
/opt/deriv-market-analyzer/.venv/bin/python /opt/deriv-market-analyzer/scripts/live_api_check.py
```

## Endpoint

| Item | Result |
|---|---|
| Working endpoint | `wss://api.derivws.com/trading/v1/options/ws/public` |
| Legacy `wss://ws.derivws.com/websockets/v3?app_id=…` and `ws.binaryws.com` | HTTP 520 — no longer usable |
| Token needed for market data | **No**. `active_symbols`, `contracts_for`, `ticks`, `ticks_history` and one-shot `proposal` all work unauthenticated |
| Schema naming | `underlying_symbol` / `underlying_symbol_name` (older docs say `symbol` / `display_name`) |

## Calls the analyzer depends on

| Call | Status | Notes |
|---|---|---|
| `{"ping": 1}` | PASS | Used as an application-level keep-alive every 30 s |
| `{"active_symbols": "brief"}` | PASS | 95 symbols listed; 52 `synthetic_index` (random_index 13, jump_index 5, crash_index 20, step_index 5, range_index 2, random_daily 2, baskets 5). `pip_size` here is a **step** (e.g. `0.001`) |
| `{"contracts_for": "<symbol>"}` | PASS | Rejects a `currency` field. random_index (R_*, 1HZ*V) and jump_index (JD*) offer DIGITOVER/UNDER/EVEN/ODD/MATCH/DIFF at **1–10 ticks**. Crash/Boom, Step and others offer **no digit contracts** — the compatibility matrix excludes them automatically |
| Digit barriers | — | DIGITOVER accepts 0–8, DIGITUNDER 1–9 (from `contracts_for` barrier ranges). All bot barriers fall inside these |
| `{"ticks": "<symbol>", "subscribe": 1}` | PASS | One stream per symbol. 1s indices tick every 1 s; the classic R_10…R_100 every **2 s**. `pip_size` on a tick is a **count of decimals** (e.g. `3`) |
| `{"ticks_history": …, "style": "ticks"}` | PASS | At most **1,000 ticks per request**, anchored at `end`; the collector pages backwards. Prices arrive as JSON numbers, so trailing zeros are lost on the wire — the analyzer re-formats every price with the symbol's pip count (`Decimal.quantize`) before taking the last digit |
| `{"proposal": 1, … "underlying_symbol": …}` | PASS | Answered **without a token**; nothing is bought. Gives `ask_price` and `payout`, so break-even = ask_price / payout. Example: DIGITOVER 3, 1 tick, stake 10 → payout ≈ 16.3 → break-even ≈ 61.2 % |
| Automatic reconnect | PASS | Socket force-closed; client reconnected and re-subscribed in ~1–2 s |

## Rate limits

* Deriv returned `RateLimit: You have reached the rate limit for proposal` after roughly 80 quotes
  in under 30 seconds during the live run. The default quote rate is therefore **30 per minute**.
  A rate-limited quote is retried after 60 s, a previous good quote is kept, and missing quotes
  are retried after 5 minutes rather than 60.
* History bootstrap at the configured general rate (150/min) loaded 30,000 ticks for each of 6
  symbols in about 85 s. The default target of 400,000 ticks for each of 20 compatible symbols is
  about 8,000 requests, which takes roughly **1 hour** on first start. Nothing is evaluated as
  RUN-capable until a symbol has enough data.

## Settlement alignment (assumption — confirm on a demo account)

A contract bought on the tick that met the condition is assumed to settle on the last digit of
the tick `duration` ticks later (entry spot = next tick). This matches what `proposal`
reports (`date_start`/`date_expiry`) and Deriv's contract rules. DBot's real purchase latency can
push entry one tick later, so the analyzer also scores every leg at `+1` tick
(`latency_robustness`). A START requires the edge to hold under both alignments.

## Live dry-run of the full application (2026-11-16)

The application ran three times in dry-run mode against the live feed, about 16 minutes in
total. It used a scratch database, 6 symbols and a 30,000-tick bootstrap. No Telegram was used
and nothing was sent.

* **Run 1 (8 min, empty DB).** Discovery found 20 digit-capable synthetic symbols (200 bot ×
  market pairs) and monitored the 6 allowed by the test config. Bootstrap loaded about 30,000
  ticks per symbol in about 85 s. Payout quotes were fetched and every combination was
  evaluated. Every combination came out AVOID, WAIT or INSUFFICIENT_DATA. That is the expected
  result on fair digits: for example, Aria's DIGITUNDER 9 wins about 90 % against a 91.24 %
  break-even. No START was produced.
* **Run 2 (6 min, same DB, simulated restart).** Stored history was reused (bootstrap
  inserted 0). The restart gap was backfilled. A **real network drop** occurred: the ping
  failed, the client reconnected after about 26 s, re-subscribed and backfilled the 58–60 s gap
  on every symbol. Payouts came back 89/90 (one failed on the dropped connection; it is retried
  after 5 min). Afterwards there were 0 notifications, 0 active signals and 0 incidents.
* **Run 3 (1.5 min, same DB).** The restart gap was backfilled within about 1 s of startup.
  Discovery now reported 19 compatible symbols, which shows the market list is re-read
  dynamically.

Three defects found during these runs were fixed and covered by new tests:

1. The classic 2-second indices were flagged as "missing ticks" on every tick because the
   default interval was 1 s. The interval is now learned from stored and live ticks.
2. The proposal burst limit (above).
3. After a restart, the downtime gap waited for the 10-minute gap audit (first run at +2 min).
   It is now queued as soon as a market with stored history is added.

One defect in the app's own shutdown logic was also fixed: a loop that legitimately ends, such as
the daily summary when it is disabled, used to stop the whole service.

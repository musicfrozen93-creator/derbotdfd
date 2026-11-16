# Final pre-deployment audit (2026-11-16)

Scope: inspect the existing code, fix only verified issues, and report what was and was not
checked. Nothing in this audit places trades, uses a Deriv account or token, or changes a bot's
entry rules.

## Classification

**READY FOR VPS DRY-RUN.**

Not yet "ready for Telegram testing" or "production monitoring", because two required checks
have not been performed:

1. `install.sh`, the systemd unit, reboot behaviour and file permissions have **not been run on
   Ubuntu 24.04**. All testing was on Windows 10 (Python 3.13). The shell scripts pass
   `bash -n` and have LF line endings; nothing more is proven.
2. Telegram delivery has **not been tested with a real bot**, because no credentials were
   provided here (as intended). The sender and the new TEST command are covered by mocked
   tests only.

Upgrade path: VPS dry-run (`ANALYZER_DRY_RUN=true`, `docs/DEPLOYMENT_UBUNTU_24.04.md` step 5)
→ run `scripts/test_telegram.py` on the VPS → READY FOR TELEGRAM TESTING → one day of clean live
running with `status.sh` and no `evaluation_slow` / `task_crashed` lines → READY FOR PRODUCTION
MONITORING.

## 1. Market coverage

| Question | Answer | Evidence |
|---|---|---|
| Was the 6-market limit only a test setting? | Yes | It came from the scratch file `smoke.yaml` (`max_symbols: 6`). The shipped `config.yaml` has `max_symbols: 40`, no submarket filter, no excluded symbols |
| Does production discover and monitor every compatible market? | Yes | Live dry-run with the production config (only paths and first-start history size changed): `discovery_done listed=95 candidates=52 monitored=19 compatible_pairs=190`; all 19 subscribed and bootstrapped |
| Are all bot × market combinations generated dynamically? | Yes | `build_matrix` runs on every discovery (every 6 h) from live `active_symbols` + `contracts_for`; `tests/test_discovery.py` checks 20 symbols → 200 combinations |
| Independent state per combination? | Yes | One `signal_state` row per (bot, symbol); tested in `test_one_markets_stop_does_not_touch_other_markets_or_bots` |
| New markets added automatically? | Yes | `test_new_market_is_added_automatically`; live: the count was 20 earlier today and 19 now |
| Removed / unavailable markets? | Yes | Delisted → active signals get a critical STOP; contract withdrawn → critical STOP; **fixed:** a failed `contracts_for` call (e.g. RateLimit) no longer drops a market |

Today's count is 19 because Deriv's `contracts_for` for **JD100** currently lists only
CALL/CALLE/PUT/PUTE (no digit contracts). The earlier run today listed 20. This changes on its
own when Deriv changes the listing. Full matrix: `docs/COMPATIBILITY_MATRIX.md`.

## 2. START / STOP verification

All verified by mocked tests in `tests/test_signal_transitions.py` (32 tests) plus the existing
`tests/test_signals.py`:

| Requirement | Verified by |
|---|---|
| START only when every validation gate passes (3 consecutive RUN_CANDIDATE evaluations, cooldown over) | `test_inactive_no_start_without_three_consecutive_run` (5 cases), `test_inactive_broken_streak_restarts_count`, `test_inactive_data_dip_hold_blocks_start`, `test_inactive_cooldown_after_stop` |
| No WAIT / AVOID / INSUFFICIENT messages | `test_inactive_non_run_statuses_send_nothing` (3 statuses × 50 cycles), `test_inactive_critical_fault_sends_nothing` |
| STOP when an active combination becomes invalid | `test_active_three_consecutive_failures_stop_once` (4 cases), contract withdrawn, feed fault, unmonitored |
| No repeated START | `test_active_run_never_repeats_start` (100 cycles), restart tests |
| No repeated STOP | every STOP test continues failing afterwards and asserts exactly one STOP |
| One market's STOP does not affect another | `test_one_markets_stop_does_not_touch_other_markets_or_bots`, `test_delisted_market_stops_its_active_signals_only` |
| Restart does not duplicate notifications | `test_restart_keeps_active_and_sends_no_duplicate_start`, `test_outbox_delivers_each_message_once_across_restart_and_failures` |
| A genuine data-feed failure invalidates an active signal safely | `test_genuine_feed_failure_invalidates_active_signal_end_to_end` (real analyzer + engine: RUN → START, feed FAULT → immediate critical STOP), grace-period tests |
| State change and message are atomic | `test_transition_and_notification_are_atomic` |

## 3. Strategy verification

All 10 XML files were decompiled block by block and compared with the YAML files. **All match;
no strategy was changed.** Confirmed behaviour and the assumptions that need a demo account
(tick alignment, Navix `timeout(0 s)`, Zony AI after 6 losses, Crysis tick ordering) are listed
separately in `docs/STRATEGY_INVENTORY.md`.

## 4. Statistical validation (code review)

| Item | Finding |
|---|---|
| Break-even | `ask_price / payout` from a live public `proposal` (stake 10). Correct for digit contracts: profit at p is p·payout − ask. A missing or stale (> 3 h) quote gives INSUFFICIENT_DATA; payouts are never assumed |
| Payout | Decimal from the proposal JSON; RateLimit retried, previous good quote kept |
| Confidence intervals | Wilson score interval (95 %) |
| Out-of-sample | Discovery = older 70 % of ticks, confirmation = newest 30 %; event windows never straddle the split; OOS needs ≥ 5,000 trials, WR > break-even and one-sided binomial p < 0.05 |
| Multiple testing | Benjamini–Hochberg across every (symbol, leg) test that has a payout in the cycle (≈ 285–300 tests), q ≤ 0.05 |
| Latency robustness | Same trigger settled one tick later must also have its lower 95 % bound above break-even. **On by default** (`analysis.latency_robustness: true`), asserted by a test on the shipped config |
| Sample size | INSUFFICIENT below 20,000 ticks or 2,000 trigger events; RUN needs ≥ 20,000 discovery trials |
| Digit frequency alone | Never creates a START. The only digit-frequency test (chi², p < 0.001) can only block (AVOID). Every RUN gate compares a win rate with the real break-even |

Limitation (documented, not fixed): the same hypotheses are re-tested every minute on mostly
overlapping data. BH controls false discoveries within a cycle, and the independent
out-of-sample block, latency and stability gates make a false START much rarer, but over months
of continuous running a false START remains possible. Treat any START as "confirm on demo".

## 5–6. Production configuration and deployment

| Item | Status |
|---|---|
| Discovers all compatible markets; all combinations | Yes (above) |
| Rate limits | 150/min general, 30/min proposals (from the live RateLimit at ~80 quotes in < 30 s). First start: ~1 h history download, ~10 min of quotes |
| Reconnect and backfill | Tested live earlier today (real network drop, 26 s reconnect, gap backfilled) and by mocked tests |
| SQLite persistence | WAL; state + outbox in one transaction |
| Telegram START/STOP only | Plus CRITICAL / RECOVERED alerts for feed or system faults (`telegram.critical_alerts`, as in the original brief) |
| No trading, no Deriv token | Confirmed: the client sends no `buy`, `sell` or `authorize` request and refuses them (`FORBIDDEN_CALLS` in `app/deriv/client.py`); no token setting exists |
| Credentials in environment variables | `.env` (mode 640, root:deriv-analyzer) or the systemd environment; token redacted from logs |
| install.sh / systemd / permissions | Reviewed and hardened (below). **Not run on Ubuntu** |

## Issues found and fixed in this audit

1. **Memory peak ~1.1 GB per evaluation cycle (measured).** With 20 markets × 400,000 ticks the
   analyzer kept the per-event arrays of all 300 tests until the end of each cycle. Now it
   processes one market at a time and releases them. Measured peak: **1,122 MB → 325 MB**,
   steady 250 MB (load test: 20 × 400k ticks, 200 combinations, i5-6500).
2. **Active signal could stay "active" across an analyzer outage.** If the VPS or service was
   down, no STOP was ever sent for what happened meanwhile. Now the analyzer stores a "last
   alive" time; after ≥ 120 s offline (`signals.offline_stop_s`) every active signal gets a
   critical STOP on restart. Short restarts keep the signal.
3. **Confirmation counters survived restarts.** Two RUN evaluations before a restart plus one
   after could trigger a START. Counters now reset at start-up.
4. **A failed `contracts_for` call removed a market.** A RateLimit reply during re-discovery made
   the market "incompatible" and would STOP its signals. The last known listing (memory or
   database) is now kept.
5. **Series loading** read 400k SQLite rows at once; now in 20k-row chunks.
6. **install.sh:** re-installing overwrote an edited `config.yaml` (now kept, new one saved as
   `config.yaml.new`); the whole app was owned by the service user (code now root-owned,
   `data/` and `logs/` service-owned, `.env` 640); bytecode precompiled for the read-only code
   directory; `useradd --user-group` explicit.
7. **Slow evaluations were silent.** An `evaluation_slow` warning is logged when a cycle takes
   longer than its 60 s interval.
8. **"Future exception was never retrieved" ERROR at shutdown (seen live).** A request that was
   still sending when the socket closed left a failed future behind. The request now always
   collects its own future. Harmless before, but it would have shown up as an ERROR line on
   every disconnect that hit an in-flight request.

New: Telegram TEST mode (`scripts/test_telegram.py`, TEST START + TEST STOP, never opens the
database) and `scripts/telegram_chat_id.py` (finds the chat id using the token in `.env`).

## Test report

### Automated tests (mocked Deriv and Telegram APIs)

`python -m pytest -q` → **152 passed** (Windows 10, Python 3.13.7, numpy 2.4.4, scipy 1.18.1,
websockets 16.1.1). Before this audit: 105.

| File | Tests | Covers |
|---|---|---|
| test_signal_transitions.py | 32 | every START/STOP transition, restarts, downtime STOP, outbox, atomicity, feed failure end to end |
| test_strategies.py | 31 | each bot's trigger, contract, barrier, duration, sequence |
| test_precision.py | 15 | last digit from Decimal at pip precision, trailing zeros |
| test_collector.py | 11 | WAL, dedupe, gaps, backfill, restart gap, chunked series loading |
| test_signals.py | 10 | confirmation, cooldown, independence, restart |
| test_validation.py | 10 | every gate; fair digits never RUN; biased digits reach RUN |
| test_statistics.py | 9 | Wilson, binomial, BH, runs test |
| test_discovery.py | 8 | all compatible markets × all bots, new / delisted markets, failed contracts_for, restart check |
| test_compatibility.py | 8 | contracts_for parsing, matrix |
| test_telegram.py | 6 | message format, timezone, outbox retries, token redaction |
| test_telegram_testmode.py | 6 | TEST labels, database untouched (byte-identical), no token in output |
| test_payout.py | 4 | break-even, RateLimit retry |
| test_client.py | 2 | reconnect against a local WebSocket server, exact quote text |

### Load test (production scale, synthetic data)

20 markets × 400,000 ticks, 10 bots, 200 combinations, 300 statistical tests per cycle, on an
Intel i5-6500 (3.2 GHz):

| | Before fix | After fix |
|---|---|---|
| Peak memory (RSS) | 1,122 MB | **325 MB** |
| Steady memory | 249 MB | 253 MB |
| Normal cycle | 11.5 s | 11.4 s |
| Cycle with streak simulation (every 30 min) | 49.5 s | 49.6 s |

A VPS vCPU is usually slower than this desktop core, so expect longer cycles there; the
analyzer adapts its sleep, and `evaluation_slow` is logged if a cycle exceeds 60 s.

### Live checks against the public Deriv API (no token, dry-run, nothing sent)

Production `config.yaml`, with only the file paths and the first-start history size
(25,000 instead of 400,000 ticks) changed, so it finishes in minutes:

| Run | Duration | Result |
|---|---|---|
| Full production run, empty database | 12 min | 95 listed / 52 synthetic / **19 compatible markets, 190 combinations monitored**; 19 bootstraps done; **285/285 payout quotes, no RateLimit**; AVOID 142, WAIT 48; 0 notifications, 0 active signals, 0 incidents, no ERROR lines |
| Restart on the fixed code, same database | 2.5 min | `analyzer_restart offline_s=19 stopped=0`; stored history reused; downtime gap backfilled on all 19 markets; same 190 combinations; 0 notifications. Revealed issue 8 below |
| Restart after fixing issue 8 | 1.7 min | `offline_s=46`, clean shutdown, no ERROR lines |

Earlier today (before the audit): reconnect after a real network drop (26 s) with gap
backfill, `scripts/live_api_check.py` 20/20.

### Not tested

* Ubuntu 24.04 installation, systemd start/stop/restart, reboot, permissions (no Linux machine
  here; WSL has no distribution installed).
* Real Telegram delivery (no credentials provided, as intended).
* Behaviour after a real START on live data: none occurred (expected on fair digits). The START
  path is covered by mocked and synthetic-data tests.
* Demo-account questions in `docs/STRATEGY_INVENTORY.md`.

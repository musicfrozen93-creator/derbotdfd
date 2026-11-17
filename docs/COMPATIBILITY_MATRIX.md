# Bot × market compatibility matrix

> **Signal rule changed on 2026-11-17.** The win-rate / break-even / confidence-bound / FDR /
> out-of-sample gates and payout quotes described below were removed. A bot × market now
> passes only when every winning digit of the bot's contracts is above 10 % of the last 500
> ticks (README → "How it works"). This file is kept as a record of the earlier checks.

> **Since 2026-11-17 the analyzer monitors Continuous Indices only.** The market filter keeps the
> API submarket `random_index` (R_10, R_25, R_50, R_75, R_100) plus the 1-second indices the bot
> XML files use (1HZ10V, 1HZ50V). Live run on 2026-11-17: 95 markets listed, **7 kept, 70 bot ×
> market combinations, all 10 bots compatible with all 7.** Excluded: Jump (JD10–JD100),
> Crash/Boom (20), Range Break (RB100, RB200), Daily Reset (RDBEAR, RDBULL), Step (stpRNG–
> stpRNG5), Forex Basket (WLDAUD, WLDEUR, WLDGBP, WLDUSD), Commodities Basket (WLDXAU), the other
> six 1-second indices (1HZ15V, 1HZ25V, 1HZ30V, 1HZ75V, 1HZ90V, 1HZ100V) and every non-synthetic
> market. The rest of this page is the earlier all-market run, kept for reference.

Source: live dry-run of the production configuration against Deriv's public API on
2026-11-16 (12:16–12:28 UTC, then two restarts). `active_symbols` listed 95 symbols, 52 of them
synthetic; `contracts_for` was called for all 52. **19 markets offer the digit contracts the bots
need, and every one of the 10 bots is compatible with all 19: 190 combinations, all monitored.**

The matrix is rebuilt from live data every 6 hours, so it follows Deriv's listings. Earlier the
same day JD100 offered digit contracts (20 markets, 200 combinations); at 12:17 UTC its
`contracts_for` listed only CALL/CALLE/PUT/PUTE, so it was excluded automatically. Run
`scripts/report.py --matrix` on the VPS for the current matrix.

## What each bot needs

| Bot | Contracts (prediction, duration) |
|---|---|
| Aria | DIGITUNDER 9 and DIGITUNDER 5, 1 tick |
| Aria V2 | DIGITUNDER 9 and DIGITUNDER 5, 1 tick |
| Crysis | DIGITOVER 3, 5 ticks |
| Delta | DIGITOVER 3, 2 ticks |
| Navix | DIGITUNDER 8 and DIGITOVER 2, 1 tick |
| Predator | DIGITOVER 3, 1 tick |
| Predator v2 | DIGITOVER 5, 1 tick |
| Pulse2 | DIGITEVEN, 1 tick |
| Voltrix | DIGITOVER 7, 3 ticks |
| Zony AI | DIGITOVER 0, 4, 5, 6, 7, 8, 1 tick |

All 19 markets list DIGITOVER (predictions 0–8), DIGITUNDER (1–9), DIGITEVEN and DIGITODD at
1–10 ticks, so every leg fits.

## Matrix (✓ = compatible and monitored; status from the last evaluation of the run)

| Market | Name | Ticks stored | Aria | Aria V2 | Crysis | Delta | Navix | Predator | Predator v2 | Pulse2 | Voltrix | Zony AI |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| R_10 | Volatility 10 Index | 25,349 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |
| R_25 | Volatility 25 Index | 25,344 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |
| R_50 | Volatility 50 Index | 25,344 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID |
| R_75 | Volatility 75 Index | 25,344 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID |
| R_100 | Volatility 100 Index | 25,348 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID |
| 1HZ10V | Volatility 10 (1s) Index | 25,696 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID |
| 1HZ15V | Volatility 15 (1s) Index | 25,696 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |
| 1HZ25V | Volatility 25 (1s) Index | 25,695 | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |
| 1HZ30V | Volatility 30 (1s) Index | 25,695 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID |
| 1HZ50V | Volatility 50 (1s) Index | 25,696 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |
| 1HZ75V | Volatility 75 (1s) Index | 25,695 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |
| 1HZ90V | Volatility 90 (1s) Index | 25,694 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID |
| 1HZ100V | Volatility 100 (1s) Index | 25,696 | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |
| JD10 | Jump 10 Index | 25,693 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID |
| JD25 | Jump 25 Index | 25,692 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID |
| JD50 | Jump 50 Index | 25,691 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ AVOID | ✓ AVOID |
| JD75 | Jump 75 Index | 25,691 | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |
| RDBEAR | Bear Market Index | 25,345 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |
| RDBULL | Bull Market Index | 25,345 | ✓ AVOID | ✓ AVOID | ✓ WAIT | ✓ WAIT | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID | ✓ AVOID |

Status totals: AVOID 142, WAIT 48, RUN_CANDIDATE 0, no notifications sent. The history here
was a shortened 25,000-tick bootstrap (about 7 h on 1 s markets, 14 h on 2 s markets); on the
VPS the default is 400,000 ticks. AVOID mostly means "win rate reliably below the real
break-even" (e.g. Aria's UNDER 9 wins about 90 % but needs 91.24 %), which is the expected
result for fair digits.

## Synthetic markets without digit contracts (excluded automatically)

| Family | Symbols |
|---|---|
| Boom | BOOM50, BOOM99, BOOM100, BOOM150N, BOOM200, BOOM300N, BOOM500, BOOM600, BOOM900, BOOM1000 |
| Crash | CRASH50, CRASH99, CRASH100, CRASH150N, CRASH200, CRASH300N, CRASH500, CRASH600, CRASH900, CRASH1000 |
| Step | stpRNG, stpRNG2, stpRNG3, stpRNG4, stpRNG5 |
| Range Break | RB100, RB200 |
| Baskets | WLDAUD, WLDEUR, WLDGBP, WLDUSD, WLDXAU |
| Jump (today) | JD100 (only CALL/PUT listed at the time of the run) |

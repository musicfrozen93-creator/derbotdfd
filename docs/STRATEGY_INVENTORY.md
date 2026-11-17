# Phase A — Strategy inventory (10 DBot XML files)

> **Signal rule changed on 2026-11-17.** The win-rate / break-even / confidence-bound / FDR /
> out-of-sample gates and payout quotes described below were removed. A bot × market now
> passes only when every winning digit of the bot's contracts is above 10 % of the last 500
> ticks (README → "How it works"). This file is kept as a record of the earlier checks.

Each bot was re-read from its XML (Blockly) file and reconciled against the Phase 1 audit
report. The machine-readable version is `strategies/<bot>.yaml`; the analyzer uses only those
files. The original XML files are never opened for writing.

**Nothing below is a new trading rule.** Each trigger, contract, prediction and duration is the
bot's own logic. Logic in the XML that only produces notifications, display text or statistics
is listed under "non-purchase logic" and is not used as an entry condition.

## Purchase legs (18 legs, 10 bots)

| Bot | Original symbol | Leg | Contract | Ticks | Entry condition (from the XML) | p₀ | Compared with |
|---|---|---|---|---|---|---|---|
| Aria | 1HZ10V | base | DIGITUNDER 9 | 1 | none — every tick | 90 % | — |
| Aria | 1HZ10V | recovery (after any loss) | DIGITUNDER 5 | 1 | none — every tick | 50 % | — |
| Aria V2 | 1HZ10V | base | DIGITUNDER 9 | 1 | none — every tick | 90 % | — |
| Aria V2 | 1HZ10V | recovery | DIGITUNDER 5 | 1 | none — every tick (the "→ 6" branch is unreachable) | 50 % | — |
| Predator | 1HZ10V | main | DIGITOVER 3 | 1 | last price ≥ SMA(8) of the last 8 prices | 60 % | SMA-false ticks |
| Predator v2 | R_100 | main | DIGITOVER 5 | 1 | none — every tick | 40 % | — |
| Voltrix | R_100 | main | DIGITOVER 7 | 3 | none — every tick | 20 % | — |
| Delta | R_10 | main | DIGITOVER 3 | 2 | last digit = 1 | 60 % | unconditional |
| Crysis | R_10 | main | DIGITOVER 3 | 5 | ≥ 4 of the last 7 digits in 0–3 **and** last digit ≥ 5 | 60 % | unconditional |
| Navix | R_100 | base | DIGITUNDER 8 | 1 | last digit = 4 (base mode) | 80 % | unconditional |
| Navix | R_100 | recovery | DIGITOVER 2 | 1 | last digit = 6 (after a loss) | 70 % | unconditional |
| Zony AI | 1HZ50V | step 1–6 | DIGITOVER 0 / 4 / 5 / 6 / 7 / 8 | 1 | none — every tick; the barrier steps up after each loss | 90 / 50 / 40 / 30 / 20 / 10 % | — |
| Pulse2 *(optional)* | R_100 | main | DIGITEVEN | 1 | last digit ∈ {0, 3, 6, 9} | 50 % | unconditional |

Aria and Aria V2 share their two legs, so the statistics are computed once and used for both.

## Stake, take-profit and stop-loss (for the risk lines in alerts only)

| Bot | Base | After a loss | TP | SL | Note |
|---|---|---|---|---|---|
| Aria | 1 | × 2.1 | 200 | 200 | SL checked only after a loss settles (8 losses = −342.94) |
| Aria V2 | 1 | × 2.1 | 20 | 50 | SL breached on the 6th loss (−77.06) |
| Predator | 1 | × 3 | 10 | none | 6 losses = −364 |
| Predator v2 | 2 | × 2 (reset if a single loss ≥ 1000) | 607 | none | resets only at the 10th loss |
| Voltrix | 0.37 | flat × 4, then × 1.3 | 5 | 500 | 20 % win contract |
| Delta | 0.5 | × 2.5 | 5 | 50 | SL breached on the 6th loss (−81.05) |
| Crysis | 0.35 | Y += loss, stake = 2Y (≈ × 3) | 3 | none | compounding after clean wins depends on balance (unknown) |
| Navix | 0.6 | × 3.5 | none | none | 7th stake = 1,102.96 |
| Zony AI | 1 | flat; barrier steps up | none | none | 7th step undefined → DBot error |
| Pulse2 | 0.75 | × 2.05 | 5 | 3000 | SL reached around the 12th loss |

The analyzer never uses these stakes to trade. They only produce the "x losses in a row cost y,
about 1 in n" lines in START messages, with theoretical probabilities from each
contract's winning digits.

## Differences from the Phase 1 audit report

* **Leg count.** The audit lists "10 Over/Under legs across 9 bots". Here Zony AI's six
  prediction steps are six legs, Aria and Aria V2 each have two legs, and Pulse2 adds one, for
  18 legs. These are the same contracts; they are only split out so that each barrier is tested
  on its own.
* **Monitored markets.** The audit recommended monitoring R_100, R_10, 1HZ10V and 1HZ50V. The
  master brief asks for dynamic discovery. The analyzer therefore checks every synthetic index
  that `contracts_for` shows with digit contracts: 20 symbols on 2026-11-16 (Volatility and Jump
  indices). The four original symbols are served first, and `discovery.max_symbols` (default 40)
  caps the total.
* **Pulse2** (Even/Odd) is marked optional in the audit. It is enabled because the master brief
  lists all 10 bots. Remove it from `strategies.enabled` if it is not wanted.
* **Latency check (added safeguard, not an entry rule).** A RUN CANDIDATE also needs the edge to
  hold when settlement slips by one tick. This only makes START harder to reach, and it is
  documented here as asked. Turn it off with `analysis.latency_robustness: false`.

## Verification against the XML (pre-deployment audit, 2026-11-16)

Each XML file was decompiled block by block into pseudocode (trade options, `before_purchase`,
`after_purchase`, `tick_analysis`, procedures) and compared with its YAML file. **All 10 YAML
files match. No strategy was changed during the audit.**

### Confirmed from the XML

| Bot | Purchase condition (`before_purchase`) | Contract / prediction / duration (trade options) | Stake and stop logic (`after_purchase`) |
|---|---|---|---|
| Aria | `Random = randint(0,9)`; all four branches purchase DIGITUNDER → every tick | prediction `9 if Loss==0 else 5`; 1 tick | win: Stake=1, Loss=0 · loss: Loss+1, Stake=\|profit\|×2.1 · stop if total ≥ 200 (checked after a win) or loss ≥ 200 (checked after a loss) |
| Aria V2 | same as Aria | `9 if Loss==0 else 5` (the `6` branch needs `Loss≥2` after `Loss≥1` already matched, so it is unreachable); 1 tick | same ladder; TP 20, SL 50 |
| Predator | `MA = SMA(ticks, 8)`; purchase only if NOT `tick < MA` (i.e. tick ≥ SMA) | DIGITOVER 3; 1 tick | loss: stake += \|profit\|×2 (→ ×3) · win: stake=1 · stop when total ≥ 10 |
| Predator v2 | none → every tick | DIGITOVER 5; 1 tick | loss: Stake += \|profit\|×1 (→ ×2), reset to 2 if that loss ≥ 1000 · win: 2 · stop at total ≥ 607 |
| Voltrix | none → every tick | DIGITOVER 7 (constant; the `Prediction` variable is unused); 3 ticks | loss counter; from the 5th loss on, stake += \|profit\|×0.3 · win: 0.37 · TP 5, SL 500 |
| Delta | `last_digit == 1` | DIGITOVER `PREDIKSI`=3; 2 ticks | loss: stake += \|profit×1.5\| (→ ×2.5) · win: 0.5 · TP 5, SL 50. The candle/direction blocks in `tick_analysis` only notify |
| Crysis | X = count of X1..X7 < 4 (last 7 digits in 0–3); purchase if `X > 3 and X1 > 4` | DIGITOVER 3; 5 ticks | loss: Y += \|profit\|, stake = 2Y · win: stake = X if Y>0, else stake += profit (back to 0.35 if it exceeds balance/1300) · stop when own profit ≥ 3 |
| Navix | `last_digit==4 and stake==0.6` → DIGITUNDER; `last_digit==6 and stake>0.6` → DIGITOVER | prediction `Predict` (8 base, 2 after a loss); `Time` = 1 tick | win: stake 0.6, Predict 8 · loss: inside `timeout(0 s)`: stake ×3.5, Predict 2 · no TP/SL (only `target += profit`) |
| Zony AI | none → every tick, DIGITOVER | trade options run `currentPrediction += 1`, then `predictionList[currentPrediction]` (1-based) of `[0,4,5,6,7,8]`; 1 tick | win: `currentPrediction = 0` · flat stake 1 · no TP/SL |
| Pulse2 | `last_digit in {0,3,6,9}` → DIGITEVEN | 1 tick | loss: +\|profit\|×1.05 (→ ×2.05) · win: 0.75 · TP 5, SL 3000. Pivot/worm blocks never feed the purchase |

### Assumptions that still need a demo-account check

These are behaviours of the DBot runtime, not of the XML, so the XML cannot settle them. The
replay uses the stated assumption; nothing here changes an entry rule.

| Topic | Assumption used by the analyzer | Why it matters | How to confirm on a demo account |
|---|---|---|---|
| Tick settlement alignment | A purchase decided on tick *i* settles on the last digit of tick *i + duration* (entry spot = tick *i+1*) | If DBot's buy lands one tick later, every conditional win rate shifts by one tick | Run Delta for ~20 trades; compare each contract's entry/exit tick times in the transaction details with the tick stream. The analyzer also requires the edge to hold at *i + duration + 1* (latency check, on by default) |
| Navix 0-second timer | The `timeout(0 s)` (stake ×3.5, Predict 2) runs before the next `before_purchase`, so the trade after a loss is a recovery trade | If `trade_again` runs first, the next trade still sees stake 0.6, buys base mode with prediction 8, and only then switches | Force a loss on demo and check whether the next contract is DIGITOVER 2 at stake 2.10 |
| Zony AI after 6 losses | Index 7 of a 6-item list is undefined → DBot error → "restart on error" restarts at step 1 | `currentPrediction` is never set in the start-up block; if the variable survives the restart it keeps counting up and errors on every attempt | Watch a 6-loss streak on demo (or temporarily shorten the list on a copy of the bot) |
| Crysis tick ordering | `tick_analysis` (shifting X1..X7) runs before `before_purchase` on the same tick | Otherwise the condition uses digits one tick old | Log X1 against the live last digit on demo |
| Crysis first loss | Initial Y = 2, so the first loss after start jumps the stake to ~4.70 | Affects only risk lines, not entry | — |
| Restart on error (all bots) | DBot restarts the trade loop immediately | Timing is not visible in the XML | — |

## Open questions that need a demo-account check (detail)

These cannot be settled from the XML alone. The replay uses the assumption stated for each one.

1. **Tick alignment (audit risk 3).** The replay assumes a trade triggered on tick *i* settles
   on tick *i + duration*. If DBot buys one tick late, conditional win rates shift; the +1-tick
   latency check exists to catch that. A few demo trades with recorded entry and exit ticks
   would confirm it.
2. **Navix loss branch.** The stake ×3.5 and the change to prediction 2 sit inside a 0-second
   `timeout` block, while `trade_again` runs right after it. The replay assumes the next trade
   already uses the recovery settings.
3. **Crysis ordering.** `tick_analysis` (which shifts X1..X7) and `before_purchase` both run on
   each tick. The replay assumes they see the same tick.
4. **Crysis initial Y = 2.** The first loss after start jumps the stake from 0.35 to 4.70. The
   risk ladder uses the steady state (Y = 0).
5. **Crysis null start.** X1..X7 start as null, and JavaScript treats `null < 4` as true. This
   affects only the first 7 live ticks after DBot starts, not the replay.
6. **Zony AI overflow.** After 6 straight losses the prediction becomes undefined and DBot
   raises an error. With "restart on error" on, the replay assumes it restarts at step 1
   (`overflow: error_reset`).
7. **Restart on error** generally (Aria, Navix, Delta, …). DBot's restart timing is not visible
   in the XML.

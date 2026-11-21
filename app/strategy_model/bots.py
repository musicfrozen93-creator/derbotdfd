"""The 10 DBot strategies, reconstructed rule by rule from the refactored XML files.

Source of truth: ``final_dbot_bots/<name>.xml`` (identical trading logic to the originals; see the
``COMPLETE_BOT_ANALYSIS`` reports). Every rule below names the XML block it comes from.

DBot runtime facts used here (deriv-app ``packages/bot-skeleton``, archived open-source copy):

* main loop: Init once -> per trade cycle: trade options (``Bot.start``) -> ``before_purchase`` on
  every new tick until ``purchase`` -> ``during_purchase`` -> ``after_purchase``; the bot stops
  unless ``after_purchase`` reaches ``trade again`` (``dbot.js`` generateCode);
* ``trade again`` compiles to ``return true`` - statements after it in the same branch never run;
* ``after N seconds`` compiles to ``sleep(N); <stack>`` (sequential);
* the purchase amount is ``+(Number(stake).toFixed(2))``;
* a ``ContractBuyValidationError`` (e.g. payout above the platform maximum) and
  ``InsufficientBalance`` are *unrecoverable*: the bot stops (``constants/messages.ts``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.strategy_model.jsnum import to_fixed2

# --------------------------------------------------------------------------- data types


def win_digits(contract_type: str, barrier: int | None) -> tuple[int, ...]:
    """Last digits that win the contract (a digit equal to the barrier loses OVER and UNDER)."""
    if contract_type == "DIGITOVER":
        return tuple(d for d in range(10) if d > barrier)
    if contract_type == "DIGITUNDER":
        return tuple(d for d in range(10) if d < barrier)
    if contract_type == "DIGITEVEN":
        return (0, 2, 4, 6, 8)
    if contract_type == "DIGITODD":
        return (1, 3, 5, 7, 9)
    if contract_type == "DIGITMATCH":
        return (barrier,)
    if contract_type == "DIGITDIFF":
        return tuple(d for d in range(10) if d != barrier)
    raise ValueError(f"unsupported contract type {contract_type}")


@dataclass(frozen=True)
class EntryRule:
    """A bot's purchase condition, evaluated on the tick that triggers ``before_purchase``."""
    kind: str                         # every_tick | last_digit_in | crysis_pattern | price_ge_sma
    digits: tuple[int, ...] = ()
    period: int = 0
    lookback: int = 1                 # ticks of history the rule needs, the current tick included
    text: str = ""
    evidence: str = ""

    @property
    def key(self) -> tuple:
        return (self.kind, self.digits, self.period)

    @property
    def fair_rate(self) -> float | None:
        """Share of ticks on which the rule fires for uniform, independent digits (None if it
        depends on prices)."""
        if self.kind == "every_tick":
            return 1.0
        if self.kind == "last_digit_in":
            return len(self.digits) / 10
        if self.kind == "crysis_pattern":
            # last digit in 5..9 (p = .5) and >= 4 of the 6 previous digits in 0..3 (p = .4 each)
            from math import comb
            return 0.5 * sum(comb(6, j) * 0.4 ** j * 0.6 ** (6 - j) for j in range(4, 7))
        return None


@dataclass(frozen=True)
class Leg:
    id: str                           # same id as the leg in strategies/<bot>.yaml
    contract_type: str
    barrier: int | None
    duration: int                     # ticks
    entry: EntryRule
    used_when: str

    @property
    def win_digits(self) -> tuple[int, ...]:
        return win_digits(self.contract_type, self.barrier)

    @property
    def p_fair(self) -> float:
        return len(self.win_digits) / 10

    @property
    def label(self) -> str:
        b = "" if self.barrier is None else f" {self.barrier}"
        return f"{self.contract_type}{b} {self.duration}t"


@dataclass(frozen=True)
class LadderStep:
    k: int                            # 0 = first trade of the losing streak
    leg_id: str
    contract_type: str
    barrier: int | None
    duration: int
    stake_variable: float             # the DBot stake variable before rounding
    amount: float                     # stake actually sent: +(Number(x).toFixed(2))
    cum_loss_before: float            # lost in this streak before this trade
    total_before: float               # DBot TOTAL_PROFIT before this trade


@dataclass(frozen=True)
class Ladder:
    variant: str
    start_total: float
    steps: tuple[LadderStep, ...]
    xml_stop_after: int | None        # losses after which the bot's OWN rule stops it (None: never)
    xml_stop_kind: str                # stop_loss | list_end | none
    xml_stop_text: str


LadderFn = Callable[[float, float, int], tuple[list[LadderStep], int | None, str, str]]


@dataclass(frozen=True)
class BotModel:
    key: str
    name: str
    xml_file: str
    default_symbol: str
    default_base_stake: float
    contract_type_block: str
    legs: tuple[Leg, ...]
    take_profit: float | None
    take_profit_rule: str
    stop_loss: float | None
    stop_loss_rule: str
    recovery_text: str
    stop_conditions: tuple[str, ...]
    reset_conditions: tuple[str, ...]
    non_trading_logic: tuple[str, ...]
    evidence: tuple[str, ...]
    ladder_variants: tuple[tuple[str, float | None, dict], ...]   # (name, start_total or None=TP-0.01, kwargs)
    _ladder: LadderFn = field(repr=False, compare=False)

    def leg(self, leg_id: str) -> Leg:
        for leg in self.legs:
            if leg.id == leg_id:
                return leg
        raise KeyError(leg_id)

    def ladders(self, base_stake: float | None = None, max_steps: int = 60) -> list[Ladder]:
        """Stake / contract of every trade in a losing streak, until the bot's own rule stops it.

        Platform limits (minimum stake, maximum payout) and the bankroll are NOT applied here;
        :mod:`app.risk.engine` applies them to these steps."""
        base = self.default_base_stake if base_stake is None else float(base_stake)
        out = []
        for name, start, kw in self.ladder_variants:
            start_total = (to_fixed2(self.take_profit - 0.01) if start is None else float(start))
            steps, stop_after, kind, text = self._ladder(base, start_total, max_steps, **kw)
            out.append(Ladder(name, start_total, tuple(steps), stop_after, kind, text))
        return out


def _after_loss(total: float, amount: float) -> float:
    """DBot TOTAL_PROFIT after a lost contract (profit = -stake), rounded to cents."""
    return to_fixed2(total - amount)


def _step(k, leg_id, ct, barrier, dur, var, amount, cum, total) -> LadderStep:
    return LadderStep(k, leg_id, ct, barrier, dur, var, amount, to_fixed2(cum), total)


# --------------------------------------------------------------------------- entry rules

EVERY_TICK = EntryRule("every_tick", text="none: it purchases on every tick",
                       evidence="before_purchase contains an unconditional purchase")
DIGIT_1 = EntryRule("last_digit_in", (1,), text="last digit = 1",
                    evidence="before_purchase: IF last_digit == 1 -> purchase")
DIGIT_4 = EntryRule("last_digit_in", (4,), text="last digit = 4 (base mode, stake == Base Stake)",
                    evidence="before_purchase: IF last_digit == 4 { IF stake == Base Stake -> purchase DIGITUNDER }")
DIGIT_6 = EntryRule("last_digit_in", (6,), text="last digit = 6 (recovery mode, stake > Base Stake)",
                    evidence="before_purchase: IF last_digit == 6 { IF stake > Base Stake -> purchase DIGITOVER }")
DIGITS_0369 = EntryRule("last_digit_in", (0, 3, 6, 9), text="last digit in {0, 3, 6, 9}",
                        evidence="before_purchase: IF last_digit in {0,3,6,9} -> purchase DIGITEVEN")
CRYSIS = EntryRule("crysis_pattern", lookback=7,
                   text="at least 4 of the last 7 last digits are below 4 (X > 3) and the current last digit is "
                        "above 4 (X1 > 4)",
                   evidence="tick_analysis shifts X7..X1 and counts X = #(Xi < 4); before_purchase: IF X > 3 AND "
                            "X1 > 4 -> purchase DIGITOVER")
SMA8 = EntryRule("price_ge_sma", period=8, lookback=8,
                 text="last tick price is not below the simple moving average of the last 8 tick prices",
                 evidence="before_purchase: sma_statement sma = SMA(ticks, 8); MA = sma; IF tick < MA -> notify "
                          "only ELSE purchase DIGITOVER")


# --------------------------------------------------------------------------- ladders (loss branch of after_purchase)

def _ladder_aria(base, start_total, max_steps, sl):
    # Stake = Base; Loss = 0. Trade options PREDICTION = Loss==0 ? 9 : 5 (the ternary's later branches are
    # unreachable). after_purchase loss: Loss += 1; Stake = abs(profit) * 2.1. SL checked after a loss.
    steps, stake, loss, total, cum = [], base, 0, start_total, 0.0
    for k in range(max_steps):
        barrier, leg = (9, "base") if loss == 0 else (5, "recovery")
        amount = to_fixed2(stake)
        steps.append(_step(k, leg, "DIGITUNDER", barrier, 1, stake, amount, cum, total))
        cum += amount
        total = _after_loss(total, amount)
        loss += 1
        stake = abs(-amount) * 2.1
        if total < 0 and abs(total) >= sl:
            return steps, k + 1, "stop_loss", f"XML stop-loss {sl:g}: TOTAL_PROFIT {total:.2f} after loss {k + 1}"
    return steps, None, "none", f"XML stop-loss {sl:g} not reached within {max_steps} losses"


def _ladder_crysis(base, start_total, max_steps, initial_y):
    # Initial Amount = Base; Initial Y = 2 at start (0 after any win). Loss: Y = Y + abs(profit);
    # Initial Amount = Y * 2. No stop-loss (svoy_profit >= 3 is checked after every trade, a take-profit).
    steps, ia, y, total, cum = [], base, float(initial_y), start_total, 0.0
    for k in range(max_steps):
        amount = to_fixed2(ia)
        steps.append(_step(k, "main", "DIGITOVER", 3, 5, ia, amount, cum, total))
        cum += amount
        total = _after_loss(total, amount)
        y = y + abs(-amount)
        ia = y * 2
    return steps, None, "none", "no stop-loss in the XML (only the svoy_profit >= 3 take-profit)"


def _ladder_delta(base, start_total, max_steps, sl):
    # Initial Stake = STAKE_MODAL (= Base). Loss: Initial Stake += abs(profit * MARTI), MARTI = 1.5.
    # After every contract: IF TOTAL_PROFIT < 0 AND abs >= STOP_LOSS (50) -> stop.
    steps, stake, total, cum = [], base, start_total, 0.0
    for k in range(max_steps):
        amount = to_fixed2(stake)
        steps.append(_step(k, "main", "DIGITOVER", 3, 2, stake, amount, cum, total))
        cum += amount
        total = _after_loss(total, amount)
        stake = stake + abs(-amount * 1.5)
        if total < 0 and abs(total) >= sl:
            return steps, k + 1, "stop_loss", f"XML stop-loss {sl:g}: TOTAL_PROFIT {total:.2f} after loss {k + 1}"
    return steps, None, "none", f"XML stop-loss {sl:g} not reached within {max_steps} losses"


def _ladder_navix(base, start_total, max_steps):
    # stake = Base, Predict = 8. Loss (inside sleep(0), sequential): stake = stake * 3.5; Predict = 2.
    # Base mode buys DIGITUNDER 8 (last digit 4), recovery buys DIGITOVER 2 (last digit 6). No TP / SL.
    steps, stake, total, cum = [], base, start_total, 0.0
    for k in range(max_steps):
        ct, barrier, leg = ("DIGITUNDER", 8, "base") if k == 0 else ("DIGITOVER", 2, "recovery")
        amount = to_fixed2(stake)
        steps.append(_step(k, leg, ct, barrier, 1, stake, amount, cum, total))
        cum += amount
        total = _after_loss(total, amount)
        stake = stake * 3.5
    return steps, None, "none", "no take-profit or stop-loss in the XML (trade again is unconditional)"


def _ladder_predator(base, start_total, max_steps):
    # stake = Base. Loss: stake += abs(profit) * MartingaleFactor (2). No stop-loss (TP: TOTAL_PROFIT >= 10).
    steps, stake, total, cum = [], base, start_total, 0.0
    for k in range(max_steps):
        amount = to_fixed2(stake)
        steps.append(_step(k, "main", "DIGITOVER", 3, 1, stake, amount, cum, total))
        cum += amount
        total = _after_loss(total, amount)
        stake = stake + abs(-amount) * 2
    return steps, None, "none", "no stop-loss in the XML (only TOTAL_PROFIT >= 10)"


def _ladder_predator_v2(base, start_total, max_steps):
    # Stake = stake 2 = Base. Loss: Stake += abs(profit) * 1; IF abs(profit) >= Loss (1000) -> Stake = stake 2.
    steps, stake, total, cum = [], base, start_total, 0.0
    for k in range(max_steps):
        amount = to_fixed2(stake)
        steps.append(_step(k, "main", "DIGITOVER", 5, 1, stake, amount, cum, total))
        cum += amount
        total = _after_loss(total, amount)
        stake = stake + abs(-amount) * 1
        if abs(-amount) >= 1000:
            stake = base
    return steps, None, "none", "no stop-loss in the XML (only TOTAL_PROFIT >= 607; single-loss >= 1000 resets the stake)"


def _ladder_pulse2(base, start_total, max_steps, sl):
    # Initial Amount = Base. Loss: Initial Amount += abs(profit) * 1.05. After every contract:
    # IF TOTAL_PROFIT < 5 { IF negative AND abs >= Max Acceptable Loss (3000) -> stop }.
    steps, ia, total, cum = [], base, start_total, 0.0
    for k in range(max_steps):
        amount = to_fixed2(ia)
        steps.append(_step(k, "main", "DIGITEVEN", None, 1, ia, amount, cum, total))
        cum += amount
        total = _after_loss(total, amount)
        ia = ia + abs(-amount) * 1.05
        if total < 0 and abs(total) >= sl:
            return steps, k + 1, "stop_loss", f"XML max loss {sl:g}: TOTAL_PROFIT {total:.2f} after loss {k + 1}"
    return steps, None, "none", f"XML max loss {sl:g} not reached within {max_steps} losses"


def _ladder_voltrix(base, start_total, max_steps, sl, tp):
    # Initial Amount = Base. Loss: Contador-perdas += 1; IF counter >= 5: Initial Amount += abs(profit) * 0.3.
    # Then (loss branch only - after a win trade_again returns first): IF TOTAL_PROFIT < 5 { IF negative AND
    # abs >= 500 -> stop ELSE sleep(0); trade again } ELSE stop (take-profit).
    steps, ia, cnt, total, cum = [], base, 0, start_total, 0.0
    for k in range(max_steps):
        amount = to_fixed2(ia)
        steps.append(_step(k, "main", "DIGITOVER", 7, 3, ia, amount, cum, total))
        cum += amount
        total = _after_loss(total, amount)
        cnt += 1
        if cnt >= 5:
            ia = ia + abs(-amount) * 0.3
        if total >= tp:
            return steps, k + 1, "take_profit", f"XML take-profit {tp:g} reached after loss {k + 1}"
        if total < 0 and abs(total) >= sl:
            return steps, k + 1, "stop_loss", f"XML stop-loss {sl:g}: TOTAL_PROFIT {total:.2f} after loss {k + 1}"
    return steps, None, "none", f"XML stop-loss {sl:g} not reached within {max_steps} losses"


ZONY_LIST = (0, 4, 5, 6, 7, 8)


def _ladder_zony(base, start_total, max_steps):
    # Stake = Base (flat). Trade options: currentPrediction += 1; PREDICTION = predictionList[currentPrediction]
    # (1-based) of [0,4,5,6,7,8]; a win sets currentPrediction = 0. After 6 losses item 7 does not exist ->
    # prediction undefined -> no barrier -> DBot error (unrecoverable) -> the bot stops.
    steps, total, cum = [], start_total, 0.0
    for k, barrier in enumerate(ZONY_LIST[:max_steps]):
        amount = to_fixed2(base)
        steps.append(_step(k, f"step{k + 1}", "DIGITOVER", barrier, 1, base, amount, cum, total))
        cum += amount
        total = _after_loss(total, amount)
    return steps, len(steps), "list_end", ("after 6 losses predictionList[7] is undefined: DBot rejects the "
                                           "contract and stops (unrecoverable error)")


# --------------------------------------------------------------------------- the 10 bots

FRESH = ("fresh run (TOTAL_PROFIT 0)", 0.0, {})


def _near_tp(name="run just below its take-profit"):
    return (name, None, {})


BOTS: dict[str, BotModel] = {}


def _add(m: BotModel) -> None:
    BOTS[m.key] = m


_add(BotModel(
    key="aria", name="Aria", xml_file="Aria.xml", default_symbol="1HZ10V", default_base_stake=1.0,
    contract_type_block="both",
    legs=(Leg("base", "DIGITUNDER", 9, 1, EVERY_TICK, "Loss == 0 (start, and after every win)"),
          Leg("recovery", "DIGITUNDER", 5, 1, EVERY_TICK, "Loss >= 1 (after any loss, until a win)")),
    take_profit=200.0, take_profit_rule="after a win: stop if TOTAL_PROFIT >= Expected Profit (200)",
    stop_loss=200.0, stop_loss_rule="after a loss: stop if TOTAL_PROFIT < 0 and abs(TOTAL_PROFIT) >= Stop Loss (200)",
    recovery_text="after a loss: Stake = abs(profit) * 2.1 and the prediction switches to 5; after a win: "
                  "Stake = Win Stake (Base Stake), prediction 9",
    stop_conditions=("TOTAL_PROFIT >= 200 after a win", "TOTAL_PROFIT <= -200 after a loss",
                     "Deriv refuses a stake (payout above the platform maximum, insufficient balance)"),
    reset_conditions=("any win: Stake = Base Stake, Loss = 0 (prediction 9)",),
    non_trading_logic=("Random = randint(0..9) only chooses the notification text; all four branches purchase "
                       "DIGITUNDER", "list / Trend List are never read",
                       "contract type 'both' also quotes DIGITOVER 9, which Deriv rejects; DBot ignores the "
                       "rejected side unless it is purchased"),
    evidence=("trade_definition_tradeoptions PREDICTION = Loss==0 ? 9 : (Loss>=1 ? 5 : ...)",
              "after_purchase: loss -> Loss = Loss + 1; Stake = abs(read_details profit) * 2.1",
              "after_purchase: win -> Stake = Win Stake; Loss = 0"),
    ladder_variants=(("fresh run (TOTAL_PROFIT 0)", 0.0, {"sl": 200.0}), ("run just below its take-profit", None, {"sl": 200.0})),
    _ladder=_ladder_aria))

_add(BotModel(
    key="aria_v2", name="Aria V2", xml_file="Aria V2.xml", default_symbol="1HZ10V", default_base_stake=1.0,
    contract_type_block="both",
    legs=(Leg("base", "DIGITUNDER", 9, 1, EVERY_TICK, "Loss == 0 (start, and after every win)"),
          Leg("recovery", "DIGITUNDER", 5, 1, EVERY_TICK,
              "Loss >= 1 (the ternary's 'Loss >= 2 -> 6' branch is unreachable)")),
    take_profit=20.0, take_profit_rule="after a win: stop if TOTAL_PROFIT >= Expected Profit (20)",
    stop_loss=50.0, stop_loss_rule="after a loss: stop if TOTAL_PROFIT < 0 and abs(TOTAL_PROFIT) >= Stop Loss (50)",
    recovery_text="after a loss: Stake = abs(profit) * 2.1, prediction 5; after a win: Stake = Base Stake, prediction 9",
    stop_conditions=("TOTAL_PROFIT >= 20 after a win", "TOTAL_PROFIT <= -50 after a loss",
                     "Deriv refuses a stake (payout above the platform maximum, insufficient balance)"),
    reset_conditions=("any win: Stake = Base Stake, Loss = 0 (prediction 9)",),
    non_trading_logic=("a text prompt (NAME) at start blocks until answered", "Random only chooses notification text",
                       "text4 / text7 built but never shown; two disabled loose blocks"),
    evidence=("trade_definition_tradeoptions PREDICTION = Loss==0 ? 9 : (Loss>=1 ? 5 : (Loss>=2 ? 6 : ...))",
              "after_purchase identical to Aria with Expected Profit 20, Stop Loss 50"),
    ladder_variants=(("fresh run (TOTAL_PROFIT 0)", 0.0, {"sl": 50.0}), ("run just below its take-profit", None, {"sl": 50.0})),
    _ladder=_ladder_aria))

_add(BotModel(
    key="crysis", name="Crysis", xml_file="Crysis.xml", default_symbol="R_10", default_base_stake=0.35,
    contract_type_block="both",
    legs=(Leg("main", "DIGITOVER", 3, 5, CRYSIS, "every trade"),),
    take_profit=None, take_profit_rule="its own counter: stop when svoy_profit >= 3 (checked after every trade)",
    stop_loss=None, stop_loss_rule="none",
    recovery_text="after a loss: Y = Y + abs(profit), stake = 2 * Y (about x3 per loss); Initial Y starts at 2, so the "
                  "first loss of every run jumps the stake to (2 + 0.35) * 2 = 4.70; after a win: stake = Initial X "
                  "(if losses were pending) or stake + profit capped at balance / 1300 (clean win)",
    stop_conditions=("svoy_profit >= 3 (its own take-profit)",
                     "Deriv refuses a stake (payout above the platform maximum, insufficient balance)"),
    reset_conditions=("win after losses: stake = Initial X, Y = 0",
                      "clean win: stake = stake + profit, reset to Base Stake if above balance / 1300"),
    non_trading_logic=("during_purchase: IF sell_available THEN (empty)", "notifications only"),
    evidence=("tick_analysis: X7..X2 shift, X1 = last digit, X = count(Xi < 4)",
              "before_purchase: IF X > 3 AND X1 > 4 -> purchase DIGITOVER; trade options DURATION 5, PREDICTION 3",
              "after_purchase: loss -> Initial Y = Initial Y + abs(profit); Initial Amount = Initial Y * 2"),
    ladder_variants=(("first losing streak of a run (Initial Y = 2)", 0.0, {"initial_y": 2.0}),
                     ("later losing streaks (Y = 0 after a win)", 0.0, {"initial_y": 0.0})),
    _ladder=_ladder_crysis))

_add(BotModel(
    key="delta", name="Delta", xml_file="Delta.xml", default_symbol="R_10", default_base_stake=0.5,
    contract_type_block="DIGITOVER",
    legs=(Leg("main", "DIGITOVER", 3, 2, DIGIT_1, "every trade"),),
    take_profit=5.0, take_profit_rule="after every contract: stop if TOTAL_PROFIT >= TARGET PROFIT (5)",
    stop_loss=50.0, stop_loss_rule="after every contract: stop if TOTAL_PROFIT < 0 and abs(TOTAL_PROFIT) >= STOP_LOSS (50)",
    recovery_text="after a loss: Initial Stake += abs(profit * 1.5) (x2.5); after a win: Initial Stake = STAKE_MODAL",
    stop_conditions=("TOTAL_PROFIT >= 5", "TOTAL_PROFIT <= -50",
                     "Deriv refuses a stake (payout above the platform maximum, insufficient balance)"),
    reset_conditions=("any win: Initial Stake = STAKE_MODAL (Base Stake)",),
    non_trading_logic=("tick_analysis: direction / candle colour notifications only",),
    evidence=("before_purchase: IF last_digit == 1 -> purchase DIGITOVER; trade options DURATION 2, PREDICTION PREDIKSI (3)",
              "after_purchase: loss -> math_change Initial Stake += abs(profit * MARTI)"),
    ladder_variants=(("fresh run (TOTAL_PROFIT 0)", 0.0, {"sl": 50.0}), ("run just below its take-profit", None, {"sl": 50.0})),
    _ladder=_ladder_delta))

_add(BotModel(
    key="navix", name="Navix", xml_file="Navix.xml", default_symbol="R_100", default_base_stake=0.6,
    contract_type_block="both",
    legs=(Leg("base", "DIGITUNDER", 8, 1, DIGIT_4, "stake == Base Stake (start, after a win)"),
          Leg("recovery", "DIGITOVER", 2, 1, DIGIT_6, "stake > Base Stake (after a loss)")),
    take_profit=None, take_profit_rule="none ('target' accumulates profit but is never compared)",
    stop_loss=None, stop_loss_rule="none",
    recovery_text="after a loss (inside 'after 0 s'): stake = stake * 3.5, Predict = 2; after a win: stake = Base Stake, "
                  "Predict = 8",
    stop_conditions=("none in the XML: trade again is unconditional",
                     "Deriv refuses a stake (payout above the platform maximum, insufficient balance)"),
    reset_conditions=("any win: stake = Base Stake, Predict = 8",),
    non_trading_logic=("Report2 procedure: display statistics only", "target += profit is never checked"),
    evidence=("before_purchase: IF last_digit == 4 { IF stake == Base Stake -> DIGITUNDER } ; "
              "IF last_digit == 6 { IF stake > Base Stake -> DIGITOVER }",
              "trade options PREDICTION = Predict (8 / 2), DURATION = Time (1)",
              "after_purchase: loss -> timeout(0 s) { stake = stake * martingale (3.5); Predict = 2 }"),
    ladder_variants=(FRESH,),
    _ladder=_ladder_navix))

_add(BotModel(
    key="predator", name="Predator", xml_file="Predator.xml", default_symbol="1HZ10V", default_base_stake=1.0,
    contract_type_block="DIGITOVER",
    legs=(Leg("main", "DIGITOVER", 3, 1, SMA8, "every trade"),),
    take_profit=10.0, take_profit_rule="after every contract: stop if TOTAL_PROFIT >= Target (10)",
    stop_loss=None, stop_loss_rule="none",
    recovery_text="after a loss: stake += abs(profit) * 2 (x3); after a win: stake = initialStake",
    stop_conditions=("TOTAL_PROFIT >= 10",
                     "Deriv refuses a stake (payout above the platform maximum, insufficient balance)"),
    reset_conditions=("any win: stake = initialStake (Base Stake)",),
    non_trading_logic=("tick_analysis shows amountLoss (never updated)", "Report procedure: display only"),
    evidence=("before_purchase: sma_statement sma = SMA(ticks, 8); IF tick < MA -> notify ELSE purchase DIGITOVER",
              "after_purchase: loss -> stake += abs(profit) * MartingaleFactor (2)"),
    ladder_variants=(FRESH,),
    _ladder=_ladder_predator))

_add(BotModel(
    key="predator_v2", name="Predator v2", xml_file="Predator v2.xml", default_symbol="R_100", default_base_stake=2.0,
    contract_type_block="both",
    legs=(Leg("main", "DIGITOVER", 5, 1, EVERY_TICK, "every trade"),),
    take_profit=607.0, take_profit_rule="after every contract: stop if TOTAL_PROFIT >= Target Profit (607)",
    stop_loss=None, stop_loss_rule="none (Loss = 1000 is compared with ONE contract's loss, not the total)",
    recovery_text="after a loss: Stake += abs(profit) (x2); if that single loss >= 1000: Stake = stake 2 (Base Stake); "
                  "after a win: Stake = stake 2",
    stop_conditions=("TOTAL_PROFIT >= 607",
                     "Deriv refuses a stake (payout above the platform maximum, insufficient balance)"),
    reset_conditions=("any win: Stake = Base Stake", "a single loss >= 1000: Stake = Base Stake"),
    non_trading_logic=("two disabled loose blocks",),
    evidence=("before_purchase: purchase DIGITOVER (unconditional); trade options PREDICTION 5, DURATION 1",
              "after_purchase: loss -> Stake += abs(profit) * 1; IF abs(profit) >= Loss -> Stake = stake 2"),
    ladder_variants=(FRESH,),
    _ladder=_ladder_predator_v2))

_add(BotModel(
    key="pulse2", name="Pulse2", xml_file="Pulse2.xml", default_symbol="R_100", default_base_stake=0.75,
    contract_type_block="both",
    legs=(Leg("main", "DIGITEVEN", None, 1, DIGITS_0369, "every trade"),),
    take_profit=5.0, take_profit_rule="after every contract: stop if TOTAL_PROFIT >= Expected Profit (5)",
    stop_loss=3000.0, stop_loss_rule="after every contract: stop if TOTAL_PROFIT < 0 and abs >= Max Acceptable Loss (3000)",
    recovery_text="after a loss: Initial Amount += abs(profit) * 1.05 (x2.05); after a win: Initial Amount = Win Amount",
    stop_conditions=("TOTAL_PROFIT >= 5", "TOTAL_PROFIT <= -3000",
                     "Deriv refuses a stake (payout above the platform maximum, insufficient balance)"),
    reset_conditions=("any win: Initial Amount = Win Amount (Base Stake)",),
    non_trading_logic=("five tick_analysis blocks (pivot points R1-R3/S1-S3, crossings, 20-tick worm colour) feed "
                       "only notifications - never the purchase",),
    evidence=("before_purchase: IF last_digit in {0,3,6,9} -> purchase DIGITEVEN; trade options DURATION 1",
              "after_purchase: loss -> Initial Amount += abs(profit) * 1.05"),
    ladder_variants=(("fresh run (TOTAL_PROFIT 0)", 0.0, {"sl": 3000.0}),
                     ("run just below its take-profit", None, {"sl": 3000.0})),
    _ladder=_ladder_pulse2))

_add(BotModel(
    key="voltrix", name="Voltrix", xml_file="Voltrix.xml", default_symbol="R_100", default_base_stake=0.37,
    contract_type_block="DIGITOVER",
    legs=(Leg("main", "DIGITOVER", 7, 3, EVERY_TICK, "every trade"),),
    take_profit=5.0, take_profit_rule="checked only after a LOSS: stop if TOTAL_PROFIT >= Expected Profit (5) "
                                      "(after a win 'trade again' returns before the check)",
    stop_loss=500.0, stop_loss_rule="after a loss: stop if TOTAL_PROFIT < 0 and abs(TOTAL_PROFIT) >= Stop Loss (500)",
    recovery_text="flat stake for 5 losses in a row; from the 5th consecutive loss on: Initial Amount += abs(profit) * 0.3 "
                  "(x1.3); after a win: Initial Amount = Win Amount, counter 0",
    stop_conditions=("TOTAL_PROFIT >= 5 when checked after a loss", "TOTAL_PROFIT <= -500 after a loss",
                     "Deriv refuses a stake (payout above the platform maximum, insufficient balance)"),
    reset_conditions=("any win: Initial Amount = Win Amount (Base Stake), Contador-perdas = 0",),
    non_trading_logic=("Prediction (2) and Time (3) variables are never used: the trade options hold literals 7 and 3",
                       "tick_analysis shows the last digit only", "three disabled text blocks"),
    evidence=("trade options PREDICTION 7 (literal), DURATION 3 (literal); before_purchase: purchase DIGITOVER",
              "after_purchase: win -> ...; trade again (returns); loss -> counter += 1; IF counter >= 5: "
              "Initial Amount += abs(profit) * 0.3; TP / SL check; sleep(0); trade again"),
    ladder_variants=(("fresh run (TOTAL_PROFIT 0)", 0.0, {"sl": 500.0, "tp": 5.0}),
                     ("run just below its take-profit", None, {"sl": 500.0, "tp": 5.0})),
    _ladder=_ladder_voltrix))

_add(BotModel(
    key="zony_ai", name="Zony AI", xml_file="Zony AI.xml", default_symbol="1HZ50V", default_base_stake=1.0,
    contract_type_block="both",
    legs=tuple(Leg(f"step{i + 1}", "DIGITOVER", b, 1, EVERY_TICK,
                   "fresh run / after a win" if i == 0 else f"after {i} consecutive loss{'es' if i > 1 else ''}")
               for i, b in enumerate(ZONY_LIST)),
    take_profit=None, take_profit_rule="none", stop_loss=None, stop_loss_rule="none",
    recovery_text="flat stake; after each loss the prediction steps up through 0, 4, 5, 6, 7, 8; a win resets it to 0",
    stop_conditions=("after 6 consecutive losses the prediction is undefined: DBot rejects the contract and stops",
                     "Deriv refuses a stake (insufficient balance)"),
    reset_conditions=("any win: currentPrediction = 0 (next trade OVER 0)",),
    non_trading_logic=("during_purchase: IF sell_available THEN (empty)",
                       "contract type 'both' also quotes DIGITUNDER 0, which Deriv rejects; DBot ignores the "
                       "rejected side unless it is purchased"),
    evidence=("trade options: math_change currentPrediction += 1; PREDICTION = lists_getIndex(predictionList, "
              "FROM_START, currentPrediction), predictionList = [0, 4, 5, 6, 7, 8]",
              "after_purchase: IF win -> currentPrediction = 0; trade again"),
    ladder_variants=(FRESH,),
    _ladder=_ladder_zony))


# --------------------------------------------------------------------------- consistency with the YAML legs

class StrategyModelError(ValueError):
    pass


def check_against_yaml(model: BotModel, spec) -> list[str]:
    """Differences between the model's legs and the analyzer's strategy YAML (BotSpec)."""
    diffs = []
    yaml_legs = {leg.id: leg for leg in spec.legs}
    model_legs = {leg.id: leg for leg in model.legs}
    for lid in sorted(set(yaml_legs) | set(model_legs)):
        a, b = model_legs.get(lid), yaml_legs.get(lid)
        if a is None or b is None:
            diffs.append(f"leg {lid}: present only in the {'YAML' if a is None else 'model'}")
            continue
        if (a.contract_type, a.barrier, a.duration) != (b.contract_type, b.barrier, b.duration_ticks):
            diffs.append(f"leg {lid}: model {a.contract_type} {a.barrier} {a.duration}t vs YAML "
                         f"{b.contract_type} {b.barrier} {b.duration_ticks}t")
    return diffs


def load_models(specs: list) -> dict[str, BotModel]:
    """Models for the enabled bots, cross-checked against their YAML. Raises on any mismatch."""
    out, problems = {}, []
    for spec in specs:
        model = BOTS.get(spec.key)
        if model is None:
            problems.append(f"{spec.key}: no strategy model (bot-specific evaluation unsupported)")
            continue
        problems += [f"{spec.key}: {d}" for d in check_against_yaml(model, spec)]
        out[spec.key] = model
    if problems:
        raise StrategyModelError("strategy model does not match strategies/*.yaml: " + "; ".join(problems))
    return out

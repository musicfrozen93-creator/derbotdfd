"""Walk each bot's losing streak (from :mod:`app.strategy_model.bots`) against the platform.

For every trade of the streak the engine asks: will Deriv accept this exact stake, and can the
account pay it? Evidence comes only from live quotes and Deriv's own refusal messages for that exact
stake (``app.economics.quotes``) and from the configured bankroll. Nothing is assumed:

* SAFE     - every trade the bot can require before its OWN stop rule (XML stop-loss, Zony's list
             end) is accepted by Deriv and fundable from the configured bankroll
* UNSAFE   - a later trade of the streak is refused (payout above the platform maximum, stake below
             the minimum) or unaffordable: the recovery breaks and DBot stops with the loss so far
* BLOCKED  - the very next trade (a fresh run's first trade) cannot be executed
* UNKNOWN  - a needed stake has no fresh quote yet, or no bankroll is configured

After a refused contract the bot cannot continue: DBot lists ``ContractBuyValidationError`` and
``InsufficientBalance`` as unrecoverable errors (bot-skeleton ``constants/messages.ts``), so "restart
on error" does not apply.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.economics.quotes import CONTEXT, QuoteBook, QuoteKey
from app.strategy_model.bots import BotModel, Ladder

SAFE, UNSAFE, BLOCKED, UNKNOWN = "SAFE", "UNSAFE", "BLOCKED", "UNKNOWN"
_RANK = {SAFE: 0, UNKNOWN: 1, UNSAFE: 2, BLOCKED: 3}
CONTINUE_AFTER_REJECTION = ("No. DBot treats ContractBuyValidationError and InsufficientBalance as unrecoverable "
                            "(bot-skeleton constants/messages.ts): the bot stops with the loss already taken.")


@dataclass
class RiskConfig:
    bankroll: float | None = None             # account balance available to the bot; None = unknown
    max_payout_override: float | None = None  # your account's verified maximum payout, if it differs
    min_stake_override: float | None = None
    max_quote_age_s: float = 24 * 3600
    max_steps: int = 60


@dataclass
class StepCheck:
    k: int
    leg_id: str
    contract: str
    stake: float
    cum_loss_before: float
    payout: float | None
    executable: bool | None
    reason: str


@dataclass
class LadderRisk:
    variant: str
    start_total: float
    status: str
    stop_kind: str               # xml_stop_loss | list_end | take_profit | payout_cap | min_stake | bankroll | other_refusal | unknown_quote | no_stop
    losses_before_stop: int | None
    worst_loss: float | None     # loss of the streak when it ends (own stop or forced stop)
    required_bankroll: float | None
    reason: str
    steps: list[StepCheck] = field(default_factory=list)


@dataclass
class RiskResult:
    status: str
    reason: str
    base_stake: float
    bankroll: float | None
    next_contract: str
    next_stake: float
    next_payout: float | None
    max_losses_before_stop: int | None
    worst_loss: float | None
    required_bankroll: float | None
    limits: dict
    context: str
    can_continue_after_rejection: str
    ladders: list[LadderRisk]

    def to_dict(self) -> dict:
        return asdict(self)


def _label(s) -> str:
    b = "" if s.barrier is None else f" {s.barrier}"
    return f"{s.contract_type}{b} {s.duration}t"


def _refusal(q, st, cfg: RiskConfig) -> tuple[str, str] | None:
    """(kind, reason) if Deriv / the configured account limits refuse this exact stake, else None."""
    if cfg.min_stake_override is not None and st.amount < cfg.min_stake_override:
        return "min_stake", f"stake {st.amount:.2f} below your configured minimum stake {cfg.min_stake_override:.2f}"
    if q.refused and q.min_stake is not None and st.amount < q.min_stake:
        return "min_stake", q.refused_reason
    payout = q.payout if q.ok else q.reported_payout
    if cfg.max_payout_override is not None:
        # your verified account limit replaces the maximum payout seen on public quotes
        if payout is not None:
            if payout > cfg.max_payout_override:
                return "payout_cap", (f"payout {payout:,.2f} above your configured maximum "
                                      f"{cfg.max_payout_override:,.2f}")
            return None
        return ("other_refusal", q.refused_reason) if q.refused else None
    if not q.refused:
        return None
    if q.max_payout is not None and q.reported_payout is not None and q.reported_payout > q.max_payout:
        return "payout_cap", q.refused_reason
    return "other_refusal", q.refused_reason


def _check_ladder(lad: Ladder, symbol: str, book: QuoteBook, cfg: RiskConfig, now: float,
                  bankroll: float | None) -> LadderRisk:
    steps: list[StepCheck] = []
    status, kind, reason = SAFE, "no_stop", ""
    losses, worst, need = None, None, 0.0
    for st in lad.steps:
        q = book.get(QuoteKey(symbol, st.contract_type, st.barrier, st.duration, st.amount))
        first = st.k == 0
        if q is None or q.transient or q.age(now) > cfg.max_quote_age_s:
            why = ("no Deriv quote yet" if q is None else
                   f"last quote failed ({q.error_code})" if q.transient else "quote too old")
            steps.append(StepCheck(st.k, st.leg_id, _label(st), st.amount, st.cum_loss_before, None, None, why))
            status, kind = UNKNOWN, "unknown_quote"
            reason = f"trade {st.k + 1} of a losing streak ({_label(st)} at {st.amount:,.2f}): {why}"
            break
        payout = q.payout if q.ok else q.reported_payout
        ref = _refusal(q, st, cfg)
        if ref is not None:
            kind, why = ref
            steps.append(StepCheck(st.k, st.leg_id, _label(st), st.amount, st.cum_loss_before, payout, False, why))
            status, losses, worst = (BLOCKED if first else UNSAFE), st.k, st.cum_loss_before
            reason = (f"trade {st.k + 1} of a losing streak ({_label(st)} at {st.amount:,.2f}) is refused: {why}. "
                      + ("The bot cannot place its first trade." if first else
                         f"DBot stops after {st.k} loss{'es' if st.k != 1 else ''} at -{st.cum_loss_before:,.2f}."))
            break
        if bankroll is not None and st.amount > bankroll + lad.start_total - st.cum_loss_before + 1e-9:
            avail = bankroll + lad.start_total - st.cum_loss_before
            why = f"stake {st.amount:,.2f} exceeds the balance left ({avail:,.2f}) of bankroll {bankroll:,.2f}"
            steps.append(StepCheck(st.k, st.leg_id, _label(st), st.amount, st.cum_loss_before, payout, False, why))
            status, kind, losses, worst = (BLOCKED if first else UNSAFE), "bankroll", st.k, st.cum_loss_before
            reason = f"trade {st.k + 1} of a losing streak: {why} (DBot: InsufficientBalance, the bot stops)"
            break
        steps.append(StepCheck(st.k, st.leg_id, _label(st), st.amount, st.cum_loss_before, payout, True, "accepted"))
        need = max(need, st.cum_loss_before + st.amount - lad.start_total)
        if lad.xml_stop_after is not None and st.k + 1 == lad.xml_stop_after:
            kind = {"stop_loss": "xml_stop_loss"}.get(lad.xml_stop_kind, lad.xml_stop_kind)
            losses, worst = st.k + 1, st.cum_loss_before + st.amount
            reason = f"the bot's own rule ends the streak after {losses} losses ({lad.xml_stop_text})"
            break
    else:
        status, kind = UNSAFE, "no_stop"
        reason = f"no stop of any kind within {len(lad.steps)} consecutive losses"
    return LadderRisk(lad.variant, lad.start_total, status, kind, losses,
                      None if worst is None else round(worst, 2), round(need, 2) if need else None, reason, steps)


def evaluate_risk(model: BotModel, symbol: str, book: QuoteBook, cfg: RiskConfig, base_stake: float | None,
                  now: float, ladders: list[Ladder] | None = None) -> RiskResult:
    """``ladders``: ``model.ladders(base_stake, cfg.max_steps)`` if the caller already has them."""
    base = model.default_base_stake if base_stake is None else float(base_stake)
    if ladders is None:
        ladders = model.ladders(base, cfg.max_steps)
    checks = []
    for lad in ladders:
        c = _check_ladder(lad, symbol, book, cfg, now, cfg.bankroll)
        free = c if cfg.bankroll is None else _check_ladder(lad, symbol, book, cfg, now, None)
        c.required_bankroll = free.required_bankroll if free.status == SAFE else None
        if c.status == SAFE and cfg.bankroll is None:
            c.status = UNKNOWN
            c.reason = (f"{c.reason}; bankroll not configured (risk.bankroll_usd): this streak needs "
                        f"{c.required_bankroll:,.2f} available")
        checks.append(c)
    worst = max(checks, key=lambda c: _RANK[c.status])
    first = ladders[0].steps[0]
    q0 = book.get(QuoteKey(symbol, first.contract_type, first.barrier, first.duration, first.amount))
    limits = {}
    for leg in model.legs:
        lim = book.limits.get((symbol, leg.contract_type))
        if lim is not None:
            limits[leg.contract_type] = {"max_payout": lim.max_payout, "min_stake": lim.min_stake,
                                         "observed_at": int(lim.observed_at), "context": lim.context}
    losses = [c.losses_before_stop for c in checks if c.losses_before_stop is not None]
    worst_losses = [c.worst_loss for c in checks if c.worst_loss is not None]
    reqs = [c.required_bankroll for c in checks if c.required_bankroll is not None]
    reason = worst.reason if worst.status != SAFE else "; ".join(f"{c.variant}: {c.reason}" for c in checks)
    return RiskResult(
        status=worst.status, reason=reason, base_stake=base, bankroll=cfg.bankroll,
        next_contract=_label(first), next_stake=first.amount,
        next_payout=(q0.payout if q0 is not None and q0.ok else None),
        max_losses_before_stop=max(losses) if losses else None,
        worst_loss=max(worst_losses) if worst_losses else None,
        required_bankroll=max(reqs) if reqs else None, limits=limits,
        context=(f"platform limits as observed on {CONTEXT}" if cfg.max_payout_override is None else
                 f"maximum payout from your configuration ({cfg.max_payout_override:,.2f})"),
        can_continue_after_rejection=CONTINUE_AFTER_REJECTION, ladders=checks)


def ladder_quote_needs(model: BotModel, symbol: str, book: QuoteBook, base_stake: float | None,
                       max_steps: int = 60, ladders: list[Ladder] | None = None) -> list[tuple[int, QuoteKey]]:
    """Exact stakes whose quotes the risk check needs, in streak order. A ladder is walked only up to
    its first missing / refused quote (or the bot's own stop), so it advances as quotes arrive."""
    out = []
    for lad in (model.ladders(base_stake, max_steps) if ladders is None else ladders):
        for st in lad.steps:
            key = QuoteKey(symbol, st.contract_type, st.barrier, st.duration, st.amount)
            out.append((1 + st.k, key))
            q = book.get(key)
            if q is None or not q.ok:
                break
            if lad.xml_stop_after is not None and st.k + 1 >= lad.xml_stop_after:
                break
    return out

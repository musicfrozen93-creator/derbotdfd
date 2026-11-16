"""Bot × market compatibility matrix, driven by live contracts_for data."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.deriv.contracts import SymbolContracts
from app.deriv.symbols import MarketInfo
from app.strategies.loader import BotSpec


@dataclass
class CompatResult:
    bot: str
    symbol: str
    compatible: bool
    reasons: list[str] = field(default_factory=list)
    legs: dict[str, str] = field(default_factory=dict)   # leg id -> "ok" / reason

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "all legs supported"


def check(bot: BotSpec, market: MarketInfo | None, contracts: SymbolContracts | None) -> CompatResult:
    sym = market.symbol if market else (contracts.symbol if contracts else "?")
    res = CompatResult(bot.key, sym, True)
    if market is None:
        res.compatible = False
        res.reasons.append("symbol not listed by active_symbols")
        return res
    if not market.exchange_is_open:
        res.compatible = False
        res.reasons.append("market closed")
    if market.is_trading_suspended:
        res.compatible = False
        res.reasons.append("trading suspended")
    if contracts is None:
        res.compatible = False
        res.reasons.append("contracts_for unavailable")
        return res
    for leg in bot.legs:
        c = contracts.get(leg.contract_type)
        if c is None:
            res.legs[leg.id] = f"{leg.contract_type} not offered"
        else:
            ok, why = c.supports(leg.duration_ticks, leg.barrier)
            res.legs[leg.id] = "ok" if ok else f"{leg.label} {leg.duration_ticks}t: {why}"
        if res.legs[leg.id] != "ok":
            res.compatible = False
            res.reasons.append(f"leg {leg.id}: {res.legs[leg.id]}")
    return res


def build_matrix(bots: list[BotSpec], markets: dict[str, MarketInfo],
                 contracts: dict[str, SymbolContracts]) -> dict[tuple[str, str], CompatResult]:
    out = {}
    for sym in sorted(set(markets) | set(contracts)):
        for bot in bots:
            out[(bot.key, sym)] = check(bot, markets.get(sym), contracts.get(sym))
    return out


def format_matrix(bots: list[BotSpec], matrix: dict[tuple[str, str], CompatResult]) -> str:
    lines = ["| Bot | Market | Contract | Duration | Compatible |", "|---|---|---|---|---|"]
    for (bkey, sym), res in sorted(matrix.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        bot = next(b for b in bots if b.key == bkey)
        legs = ", ".join(f"{l.label}" for l in bot.legs)
        durs = "/".join(f"{d}t" for d in bot.durations)
        lines.append(f"| {bot.name} | {sym} | {legs} | {durs} | {'Yes' if res.compatible else 'No: ' + res.reason_text} |")
    return "\n".join(lines)

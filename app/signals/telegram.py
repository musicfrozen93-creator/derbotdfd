"""Telegram Bot API notifier (outbox delivery) and message formatting.

Messages are plain text (no parse_mode), so no Markdown/HTML escaping issues.
The bot token is only used to build the request URL and is never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.analytics.risk import risk_lines
from app.analytics.validation import BotVerdict, LegVerdict
from app.monitoring.logger import event
from app.strategies.loader import BotSpec

log = logging.getLogger("signals.telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4000


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.2f}%"


def _n(x) -> str:
    return f"{int(x):,}"


class MessageFormatter:
    def __init__(self, bots: dict[str, BotSpec], tz_name: str = "Asia/Colombo", risk_streak: int = 6) -> None:
        self.bots = bots
        self.tz = ZoneInfo(tz_name)
        self.tz_name = tz_name
        self.risk_streak = risk_streak

    def ts(self, now: float) -> str:
        utc = datetime.fromtimestamp(now, tz=timezone.utc)
        local = utc.astimezone(self.tz)
        return f"{local:%Y-%m-%d %H:%M:%S} ({self.tz_name}) · {utc:%H:%M:%S} UTC"

    def _durations(self, bot: BotSpec) -> str:
        d = bot.durations
        return " / ".join(f"{x} tick{'s' if x > 1 else ''}" for x in d)

    def _leg_block(self, lv: LegVerdict, multi: bool) -> list[str]:
        s = lv.stats
        head = f"• {lv.leg_id} — {lv.label}, {lv.duration}t, trigger: {lv.trigger_text}" if multi else \
            f"• Trigger: {lv.trigger_text}"
        lines = [head]
        lines.append(f"   Trigger events: {_n(s.n_events)} (discovery {_n(s.disc_n)} · out-of-sample {_n(s.oos_n)})")
        lines.append(f"   Win rate: {_pct(s.wr)} · 95% CI {_pct(s.ci_lo)}–{_pct(s.ci_hi)}")
        if lv.payout is not None:
            lines.append(f"   Break-even: {_pct(lv.payout.break_even)} (payout {lv.payout.payout} per "
                         f"{lv.payout.stake} stake)")
        else:
            lines.append("   Break-even: unknown (no payout quote)")
        lines.append(f"   Theoretical: {_pct(s.p0)} · out-of-sample WR: {_pct(s.oos_wr)}")
        if s.q_value is not None:
            lines.append(f"   FDR q-value: {s.q_value:.3g} · runs test p: "
                         f"{'n/a' if s.runs_p is None else f'{s.runs_p:.2g}'}")
        return lines

    def _risk_block(self, bot: BotSpec, v: BotVerdict | None) -> list[str]:
        lines = [f"Risk (stake ladder from {bot.source_file}):"]
        lines += [f"• {r}" for r in risk_lines(bot, self.risk_streak)]
        if v is not None and v.sim is not None and v.sim.trades:
            lines.append(f"• Longest losing streak in replay: {v.sim.longest_losing_streak} "
                         f"({_n(v.sim.trades)} simulated trades)")
        return lines

    def start(self, v: BotVerdict, now: float) -> str:
        bot = self.bots[v.bot]
        multi = len(v.legs) > 1
        q = v.quality
        lines = ["🟢 START SIGNAL", "",
                 f"Bot: {bot.name}", "",
                 f"Market: {v.symbol_name}", f"Symbol: {v.symbol}", "",
                 f"Contract: {bot.contract_summary}", f"Duration: {self._durations(bot)}", "",
                 "Status: Conditions Confirmed", "", "Validation:",
                 f"• Sample size: {_n(q.n_ticks if q else 0)} ticks"]
        for lv in v.legs:
            lines += self._leg_block(lv, multi)
        lines += [f"• Data quality: {'PASS' if q and q.status == 'CLEAN' else 'CHECK'}",
                  f"• Contract availability: {'PASS' if v.compatible else 'FAIL'}", ""]
        lines += self._risk_block(bot, v)
        lines += ["", f"Time: {self.ts(now)}", "",
                  f"ACTION: You may consider running {bot.name} on {v.symbol}.", "",
                  "This is a suitability notification, not a guaranteed trading signal. "
                  "Historical results do not guarantee future results, and this signal does not reduce "
                  "the bot's martingale risk."]
        return "\n".join(lines)[:MAX_LEN]

    def stop(self, v: BotVerdict, now: float, critical: bool = False) -> str:
        bot = self.bots[v.bot]
        q = v.quality
        wr_bits = []
        for lv in v.legs:
            be = lv.payout.break_even if lv.payout else None
            wr_bits.append(f"{lv.leg_id}: WR {_pct(lv.stats.wr)} vs break-even {_pct(be)} "
                           f"(lower 95% {_pct(lv.stats.disc_lo)})")
        lines = ["🔴 STOP SIGNAL" + (" (critical)" if critical else ""), "",
                 f"Bot: {bot.name}", "",
                 f"Market: {v.symbol_name}", f"Symbol: {v.symbol}", "",
                 "Reason:", "The previously confirmed conditions are no longer satisfied.", "",
                 "Details:",
                 f"• Validation status: FAILED ({v.status})"]
        lines += [f"• {r}" for r in v.reasons[:6]]
        if len(wr_bits) == 1:
            lines.append(f"• Current win-rate assessment: {wr_bits[0]}")
        else:
            lines.append("• Current win-rate assessment:")
            lines += [f"   {b}" for b in wr_bits]
        lines += [f"• Data quality: {q.status if q else 'n/a'}" + (f" — {q.detail}" if q and q.status != 'CLEAN' else ""),
                  f"• Contract availability: {'PASS' if v.compatible else 'FAIL — ' + v.compat_text}", "",
                  f"ACTION: Stop running {bot.name} on {v.symbol}.", "",
                  f"Time: {self.ts(now)}"]
        return "\n".join(lines)[:MAX_LEN]

    def stop_unmonitored(self, bot_key: str, symbol: str, reason: str, now: float) -> str:
        bot = self.bots.get(bot_key)
        name = bot.name if bot else bot_key
        lines = ["🔴 STOP SIGNAL (critical)", "", f"Bot: {name}", "", f"Symbol: {symbol}", "",
                 "Reason:", reason, "",
                 "Details:", "• Validation status: FAILED (not evaluated)", "",
                 f"ACTION: Stop running {name} on {symbol}.", "", f"Time: {self.ts(now)}"]
        return "\n".join(lines)

    def critical(self, title: str, detail: str, now: float, affected: list[str] | None = None) -> str:
        lines = [f"⚠️ CRITICAL: {title}", "", detail]
        if affected:
            lines += ["", "Active signals affected: " + ", ".join(affected)]
        lines += ["", f"Time: {self.ts(now)}"]
        return "\n".join(lines)[:MAX_LEN]

    def recovered(self, title: str, detail: str, now: float) -> str:
        return "\n".join([f"✅ RECOVERED: {title}", "", detail, "", f"Time: {self.ts(now)}"])[:MAX_LEN]

    def daily_summary(self, now: float, monitored: int, compatible: int, symbols: int, active: list[str],
                      statuses: dict[str, int], quality: dict[str, str]) -> str:
        lines = ["📊 Daily summary", "",
                 f"Markets monitored: {symbols}", f"Bot × market combinations compatible: {compatible}",
                 f"Combinations evaluated: {monitored}",
                 "Active START signals: " + (", ".join(active) if active else "none"), "",
                 "Status counts: " + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items()))]
        bad = {s: q for s, q in quality.items() if q != "CLEAN"}
        lines.append("Data quality: all clean" if not bad else
                     "Data quality issues: " + ", ".join(f"{s} {q}" for s, q in sorted(bad.items())))
        lines += ["", f"Time: {self.ts(now)}"]
        return "\n".join(lines)[:MAX_LEN]


def enqueue(conn, kind: str, dedupe_key: str, text: str, now: float | None = None) -> bool:
    now_i = int(now or time.time())
    before = conn.total_changes
    conn.execute("INSERT OR IGNORE INTO notifications(kind, dedupe_key, text, created_at, next_attempt_at)"
                 " VALUES (?,?,?,?,?)", (kind, dedupe_key, text, now_i, now_i))
    return conn.total_changes > before


class TelegramSender:
    """Delivers queued notifications. Without credentials (or in dry-run) it logs
    the message text and marks it 'skipped'."""

    def __init__(self, conn, token: str | None, chat_id: str | None, *, enabled: bool = True,
                 dry_run: bool = False, timeout: float = 15, max_attempts: int = 20, transport=None) -> None:
        self.conn = conn
        self._token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token and chat_id) and not dry_run
        self.dry_run = dry_run
        self.timeout = timeout
        self.max_attempts = max_attempts
        self._transport = transport or self._http_send
        self.sent_count = 0

    def _http_send(self, text: str) -> None:
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(API.format(token=self._token), data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # The URL contains the token: report only status and Telegram's description.
            try:
                desc = json.loads(exc.read().decode("utf-8")).get("description")
            except Exception:
                desc = None
            raise RuntimeError(f"Telegram HTTP {exc.code}: {desc}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram network error: {exc.reason}") from None
        if not body.get("ok"):
            raise RuntimeError(f"Telegram error: {body.get('description')}")

    async def deliver_pending(self, now: float | None = None) -> int:
        now_i = int(now or time.time())
        rows = self.conn.execute("SELECT id, kind, text, attempts FROM notifications WHERE status='pending'"
                                 " AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY id LIMIT 20",
                                 (now_i,)).fetchall()
        done = 0
        for r in rows:
            if not self.enabled:
                event(log, "notification_not_sent", logging.WARNING if not self.dry_run else logging.INFO,
                      kind=r["kind"], reason=("dry_run" if self.dry_run else "telegram_not_configured"))
                log.info("notification text (%s):\n%s", r["kind"], r["text"])
                self.conn.execute("UPDATE notifications SET status='skipped', sent_at=? WHERE id=?", (now_i, r["id"]))
                continue
            try:
                await asyncio.to_thread(self._transport, r["text"])
            except Exception as exc:
                attempts = int(r["attempts"]) + 1
                delay = min(3600, 5 * 2 ** min(attempts, 10))
                status = "failed" if attempts >= self.max_attempts else "pending"
                self.conn.execute("UPDATE notifications SET attempts=?, last_error=?, next_attempt_at=?, status=?"
                                  " WHERE id=?", (attempts, str(exc)[:300], now_i + delay, status, r["id"]))
                event(log, "telegram_send_failed", logging.WARNING, kind=r["kind"], attempts=attempts,
                      error=str(exc)[:200])
                break   # keep ordering; retry later
            self.conn.execute("UPDATE notifications SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?",
                              (now_i, r["id"]))
            self.sent_count += 1
            done += 1
            event(log, "telegram_sent", kind=r["kind"], id=r["id"])
        return done

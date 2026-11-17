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
from fractions import Fraction
from zoneinfo import ZoneInfo

from app.analytics.risk import risk_lines
from app.analytics.validation import (FAIL, PASS, UNAVAILABLE, BotVerdict, ContractResult,
                                      digit_passes)
from app.monitoring.logger import event
from app.storage.database import transaction
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

    @staticmethod
    def _digits(ds: list[int]) -> str:
        return ", ".join(str(d) for d in ds) if ds else "none"

    @staticmethod
    def _contract_head(c: ContractResult) -> str:
        barrier = "no barrier" if c.barrier is None else f"barrier {c.barrier}"
        return f"{c.contract_type}, {barrier}, {c.duration} tick{'s' if c.duration > 1 else ''}"

    def _contract_blocks(self, v: BotVerdict) -> list[str]:
        """One block per contract: type + barrier, its own winning digits, the percentage of each
        of those digits, and that contract's own PASS / FAIL."""
        thr = f"{v.threshold_pct:g}%"
        q = v.quality
        n = q.n_ticks if q else 0
        total = len(v.contracts)
        lines = [f"Rule: each contract is checked on its own; every winning digit of a contract must be "
                 f"> {thr} of the last {v.window_ticks:,} ticks (Manual Conditions, Individual).", ""]
        for i, c in enumerate(v.contracts, 1):
            head = "Contract" if total == 1 else f"Contract {i} of {total}"
            lines.append(f"{head} ({c.leg_id}): {self._contract_head(c)}")
            lines.append(f"Winning digits: {self._digits(c.winning_digits)}")
            if c.digit_pct:
                for d in c.winning_digits:
                    if c.result in (PASS, FAIL):
                        ok = d not in c.failing_digits
                    else:
                        ok = bool(q) and digit_passes(q.counts[d], n, Fraction(str(v.threshold_pct)))
                    lines.append(f"• Digit {d}: {c.digit_pct[d]:.2f}% {'✅' if ok else '❌'}")
            else:
                lines.append("• Digit percentages: not available")
            if c.result == PASS:
                lines.append("Result: PASS ✅")
            elif c.result == FAIL:
                lines.append(f"Result: FAIL ❌ — {self._fail_text(c, thr)}")
            elif c.result == UNAVAILABLE:
                lines.append(f"Result: NOT AVAILABLE ❌ — {c.detail}")
            else:
                lines.append(f"Result: NOT CHECKED — {c.detail}")
            lines.append("")
        return lines

    def _fail_text(self, c: ContractResult, thr: str) -> str:
        ds = ", ".join(f"{d} ({c.digit_pct[d]:.2f}%)" for d in c.failing_digits)
        return f"digit{'s' if len(c.failing_digits) > 1 else ''} {ds} at or below {thr}"

    def _risk_block(self, bot: BotSpec) -> list[str]:
        lines = [f"Risk (stake ladder from {bot.source_file}):"]
        lines += [f"• {r}" for r in risk_lines(bot, self.risk_streak)]
        return lines

    def start(self, v: BotVerdict, now: float) -> str:
        bot = self.bots[v.bot]
        total = len(v.contracts)
        lines = ["🟢 START SIGNAL", "",
                 f"Bot: {bot.name}", f"Market: {v.symbol_name}", f"Symbol: {v.symbol}", ""]
        lines += self._contract_blocks(v)
        lines += [f"Signal status: START — {len(v.passing_contracts)} of {total} contract"
                  f"{'s' if total > 1 else ''} PASS (all required)", ""]
        lines += self._risk_block(bot)
        lines += ["", f"Time: {self.ts(now)}", "",
                  f"ACTION: You may consider running {bot.name} on {v.symbol}.", "",
                  "This is a digit-frequency notification, not a guaranteed trading signal. Past digit "
                  "frequencies do not predict the next tick, and this signal does not reduce the bot's "
                  "martingale risk."]
        return "\n".join(lines)[:MAX_LEN]

    def stop(self, v: BotVerdict, now: float, critical: bool = False) -> str:
        bot = self.bots[v.bot]
        total = len(v.contracts)
        thr = f"{v.threshold_pct:g}%"
        lines = ["🔴 STOP SIGNAL" + (" (critical)" if critical else ""), "",
                 f"Bot: {bot.name}", f"Market: {v.symbol_name}", f"Symbol: {v.symbol}", ""]
        lines += self._contract_blocks(v)
        failed = v.failing_contracts
        unavailable = [c for c in v.contracts if c.result == UNAVAILABLE]
        if failed:
            lines.append(f"Signal status: STOP — {len(failed)} of {total} contract{'s' if total > 1 else ''} "
                         f"FAILED")
            lines.append("Failed contract" + ("s:" if len(failed) > 1 else ":"))
            lines += [f"• {c.label} ({c.leg_id}) — {self._fail_text(c, thr)}" for c in failed]
        elif unavailable:
            lines.append("Signal status: STOP — contract no longer offered on this market")
            lines += [f"• {c.label} ({c.leg_id}) — {c.detail}" for c in unavailable]
        else:
            lines.append(f"Signal status: STOP — {v.reasons[0] if v.reasons else v.status}")
        if not v.compatible and not unavailable:
            lines.append(f"• Contract availability: FAIL — {v.compat_text}")
        q = v.quality
        if q is not None and q.status != "CLEAN":
            lines.append(f"• Live feed: {q.status} — {q.detail}")
        lines += ["", f"ACTION: Stop running {bot.name} on {v.symbol}.", "", f"Time: {self.ts(now)}"]
        return "\n".join(lines)[:MAX_LEN]

    def stop_unmonitored(self, bot_key: str, symbol: str, reason: str, now: float) -> str:
        bot = self.bots.get(bot_key)
        name = bot.name if bot else bot_key
        lines = ["🔴 STOP SIGNAL (critical)", "", f"Bot: {name}", f"Symbol: {symbol}", "",
                 f"Signal status: STOP — {reason}",
                 "• The digit percentages could not be checked for this combination.", "",
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
        lines.append("Live feeds: all OK" if not bad else
                     "Live feed issues: " + ", ".join(f"{s} {q}" for s, q in sorted(bad.items())))
        lines += ["", f"Time: {self.ts(now)}"]
        return "\n".join(lines)[:MAX_LEN]


def enqueue(conn, kind: str, dedupe_key: str, text: str, now: float | None = None) -> bool:
    now_i = int(now or time.time())
    before = conn.total_changes
    with transaction(conn, f"enqueue_notification:{kind}"):
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
        # Delivered to Telegram, but recording 'sent' in the database failed: never send these again,
        # only retry the database update (prevents a duplicate message after a write failure).
        self._sent_unrecorded: dict[int, int] = {}

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
            if r["id"] in self._sent_unrecorded:
                self._mark_sent(r["id"], self._sent_unrecorded[r["id"]])
                continue
            if not self.enabled:
                event(log, "notification_not_sent", logging.WARNING if not self.dry_run else logging.INFO,
                      kind=r["kind"], reason=("dry_run" if self.dry_run else "telegram_not_configured"))
                log.info("notification text (%s):\n%s", r["kind"], r["text"])
                with transaction(self.conn, "notification_skipped"):
                    self.conn.execute("UPDATE notifications SET status='skipped', sent_at=? WHERE id=?",
                                      (now_i, r["id"]))
                continue
            try:
                await asyncio.to_thread(self._transport, r["text"])
            except Exception as exc:
                attempts = int(r["attempts"]) + 1
                delay = min(3600, 5 * 2 ** min(attempts, 10))
                status = "failed" if attempts >= self.max_attempts else "pending"
                with transaction(self.conn, "notification_retry"):
                    self.conn.execute("UPDATE notifications SET attempts=?, last_error=?, next_attempt_at=?,"
                                      " status=? WHERE id=?", (attempts, str(exc)[:300], now_i + delay, status,
                                                              r["id"]))
                event(log, "telegram_send_failed", logging.WARNING, kind=r["kind"], attempts=attempts,
                      error=str(exc)[:200])
                break   # keep ordering; retry later
            self._sent_unrecorded[r["id"]] = now_i
            self.sent_count += 1
            done += 1
            event(log, "telegram_sent", kind=r["kind"], id=r["id"])
            self._mark_sent(r["id"], now_i)
        return done

    def _mark_sent(self, nid: int, sent_at: int) -> None:
        """Record delivery. On a database error the id stays in ``_sent_unrecorded`` (never re-sent) and
        the original exception propagates (logged by the notifier loop)."""
        with transaction(self.conn, "notification_sent"):
            self.conn.execute("UPDATE notifications SET status='sent', sent_at=?, attempts=attempts+1"
                              " WHERE id=?", (sent_at, nid))
        self._sent_unrecorded.pop(nid, None)

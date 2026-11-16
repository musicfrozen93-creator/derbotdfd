"""15. Telegram notification formatting, delivery, retries and secret redaction."""
import asyncio
import logging
import time
from decimal import Decimal

from app.analytics.payout import Quote
from app.analytics.validation import RUN, BotVerdict, LegVerdict, SymbolQuality
from app.monitoring.logger import RedactFilter
from app.signals.telegram import MessageFormatter, TelegramSender, enqueue
from app.strategies.evaluator import LegStats

T = 1_794_823_660.0   # 2026-11-16 10:07:40 UTC


def aria_v2_verdict(bots):
    legs = []
    for leg, wr, be in (("base", 0.904, 0.9174), ("recovery", 0.52, 0.5128)):
        st = LegStats(n_ticks=312400, n_events=280000, wins=int(280000 * wr), wr=wr, ci_lo=wr - 0.001,
                      ci_hi=wr + 0.001, disc_n=196000, disc_lo=wr - 0.0013, oos_n=84000, oos_wr=wr, p0=0.9,
                      q_value=0.012, runs_p=0.44)
        l = bots["aria_v2"].leg(leg)
        legs.append(LegVerdict(leg, l.label, 1, l.trigger_text, RUN, ["ok"], {}, st,
                               Quote("1HZ10V", l.contract_type, l.barrier, 1, Decimal("10.00"),
                                     Decimal(str(round(10 / be, 2))), be, T)))
    q = SymbolQuality("1HZ10V", "CLEAN", "clean", "OK", 312400)
    return BotVerdict("aria_v2", "Aria V2", "1HZ10V", "Volatility 10 (1s) Index", RUN, False, [], legs, q, True,
                      "all legs supported")


def test_start_message_format(bots):
    fmt = MessageFormatter(bots, "Asia/Colombo")
    text = fmt.start(aria_v2_verdict(bots), T)
    for needle in ("🟢 START SIGNAL", "Bot: Aria V2", "Market: Volatility 10 (1s) Index", "Symbol: 1HZ10V",
                   "Contract: DIGITUNDER 9 (base) → DIGITUNDER 5 (recovery)", "Duration: 1 tick",
                   "Status: Conditions Confirmed", "Sample size: 312,400 ticks", "Trigger events: 280,000",
                   "Break-even: 91.74%", "95% CI", "Data quality: PASS", "Contract availability: PASS",
                   "1 in 320", "stop-loss 50 breached at loss #6", "ACTION: You may consider running Aria V2 on 1HZ10V.",
                   "suitability notification, not a guaranteed trading signal", "(Asia/Colombo)", "UTC"):
        assert needle in text, needle
    assert "2026-11-16 15:37:40 (Asia/Colombo) · 10:07:40 UTC" in text     # UTC+05:30
    assert len(text) <= 4096


def test_stop_message_format(bots):
    fmt = MessageFormatter(bots, "Asia/Colombo")
    v = aria_v2_verdict(bots)
    v.status = "WAIT"
    v.reasons = ["base: lower 95% bound 90.20% ≤ break-even 91.74%"]
    text = fmt.stop(v, T)
    for needle in ("🔴 STOP SIGNAL", "Bot: Aria V2", "Symbol: 1HZ10V", "The previously confirmed conditions are no "
                   "longer satisfied.", "Validation status: FAILED (WAIT)", "Current win-rate assessment",
                   "Data quality: CLEAN", "ACTION: Stop running Aria V2 on 1HZ10V."):
        assert needle in text, needle
    assert "WAIT" not in fmt.start(aria_v2_verdict(bots), T).split("\n")[0]


def test_critical_and_recovery_format(bots):
    fmt = MessageFormatter(bots, "Asia/Colombo")
    t = fmt.critical("Deriv feed stale — R_10", "no ticks for 130 s", T, ["Delta on R_10"])
    assert t.startswith("⚠️ CRITICAL") and "Delta on R_10" in t
    assert fmt.recovered("feed live", "ok", T).startswith("✅ RECOVERED")


def test_outbox_dedupe_and_dry_run(db):
    conn, _ = db
    assert enqueue(conn, "START", "START:aria:1HZ10V:1", "hello", T)
    assert not enqueue(conn, "START", "START:aria:1HZ10V:1", "hello", T)      # duplicate key ignored
    sender = TelegramSender(conn, None, None, dry_run=True)
    assert asyncio.run(sender.deliver_pending(T)) == 0
    assert conn.execute("SELECT status FROM notifications").fetchone()[0] == "skipped"


def test_delivery_retry_then_success(db):
    conn, _ = db
    enqueue(conn, "START", "k1", "first", T)
    enqueue(conn, "STOP", "k2", "second", T)
    calls = []

    def flaky(text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("Telegram network error: timed out")

    sender = TelegramSender(conn, "123456789:" + "A" * 35, "42", transport=flaky)
    assert asyncio.run(sender.deliver_pending(T)) == 0          # first fails, ordering kept
    row = conn.execute("SELECT status, attempts, next_attempt_at FROM notifications WHERE dedupe_key='k1'").fetchone()
    assert row[0] == "pending" and row[1] == 1 and row[2] > T
    assert asyncio.run(sender.deliver_pending(row[2])) == 2
    assert calls == ["first", "first", "second"]
    assert [r[0] for r in conn.execute("SELECT status FROM notifications ORDER BY id")] == ["sent", "sent"]
    assert asyncio.run(sender.deliver_pending(row[2] + 10)) == 0  # nothing re-sent


def test_token_redaction(caplog):
    token = "123456789:AAH" + "x" * 32
    logger = logging.getLogger("redact-test")
    f = RedactFilter([token])
    rec = logger.makeRecord("redact-test", logging.INFO, __file__, 1, "url=https://api.telegram.org/bot%s/x",
                            (token,), None)
    f.filter(rec)
    assert token not in rec.getMessage() and "<redacted>" in rec.getMessage()
    rec2 = logger.makeRecord("redact-test", logging.INFO, __file__, 1, "other 987654321:" + "B" * 35, None, None)
    f.filter(rec2)
    assert "<redacted>" in rec2.getMessage()

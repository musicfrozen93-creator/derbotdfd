"""15. Telegram notification formatting, delivery, retries and secret redaction."""
import asyncio
import logging
import time

from app.analytics.validation import RUN, RuleConfig, SymbolQuality, evaluate_combo
from app.monitoring.logger import RedactFilter
from app.signals.telegram import MessageFormatter, TelegramSender, enqueue

T = 1_794_823_660.0   # 2026-11-16 10:07:40 UTC
RULE = RuleConfig.from_settings(type("C", (), dict(mode="individual", threshold_pct=10, window_ticks=500,
                                                   contracts="all"))())


def predator_verdict(bots, counts):
    """Predator (DIGITOVER 3 -> winning digits 4-9) on 1HZ10V with the given digit counts (sum 500)."""
    assert sum(counts) == 500
    q = SymbolQuality("1HZ10V", "CLEAN", "live feed OK", "OK", 500, 1.0, list(counts))
    return evaluate_combo(bots["predator"], "1HZ10V", "Volatility 10 (1s) Index", q, compatible=True,
                          compat_text="all legs supported", rule=RULE, now=T)


# digit:      0   1   2   3   4   5   6   7   8   9
PASSING = [42, 42, 42, 43, 56, 54, 61, 52, 57, 51]    # 4-9: 11.2 10.8 12.2 10.4 11.4 10.2 %
FAILING = [44, 44, 44, 45, 56, 54, 61, 52, 49, 51]    # same, but digit 8 = 9.80 %


def test_start_message_format(bots):
    v = predator_verdict(bots, PASSING)
    assert v.status == RUN
    text = MessageFormatter(bots, "Asia/Colombo").start(v, T)
    for needle in ("🟢 START SIGNAL", "Bot: Predator", "Market: Volatility 10 (1s) Index", "Symbol: 1HZ10V",
                   "Contract (main): DIGITOVER, barrier 3, 1 tick", "Winning digits: 4, 5, 6, 7, 8, 9",
                   "every winning digit of a contract must be > 10% of the last 500 ticks",
                   "• Digit 4: 11.20% ✅", "• Digit 6: 12.20% ✅", "• Digit 9: 10.20% ✅", "Result: PASS ✅",
                   "Signal status: START — 1 of 1 contract PASS (all required)",
                   "ACTION: You may consider running Predator on 1HZ10V.",
                   "not a guaranteed trading signal", "(Asia/Colombo)", "UTC"):
        assert needle in text, needle
    assert "❌" not in text
    assert "2026-11-16 15:37:40 (Asia/Colombo) · 10:07:40 UTC" in text     # UTC+05:30
    for gone in ("Break-even", "win-rate", "95% CI", "confidence", "Trigger events"):
        assert gone not in text, gone
    assert len(text) <= 4096


def test_start_message_multi_contract_bot(bots):
    q = SymbolQuality("R_50", "CLEAN", "live feed OK", "OK", 500, 1.0, [55] * 5 + [45] * 5)
    v = evaluate_combo(bots["aria_v2"], "R_50", "Volatility 50 Index", q, compatible=True, compat_text="ok",
                       rule=RULE, now=T)
    assert v.status == "WAIT"                          # digits 5-8 are 9.00%: base fails, recovery passes
    text = MessageFormatter(bots, "Asia/Colombo").stop(v, T)
    for needle in ("Contract 1 of 2 (base): DIGITUNDER, barrier 9, 1 tick",
                   "Contract 2 of 2 (recovery): DIGITUNDER, barrier 5, 1 tick",
                   "Winning digits: 0, 1, 2, 3, 4, 5, 6, 7, 8", "Winning digits: 0, 1, 2, 3, 4",
                   "• Digit 0: 11.00% ✅", "• Digit 5: 9.00% ❌",
                   "Result: FAIL ❌ — digits 5 (9.00%), 6 (9.00%), 7 (9.00%), 8 (9.00%) at or below 10%",
                   "Result: PASS ✅", "Signal status: STOP — 1 of 2 contracts FAILED", "Failed contract:",
                   "• DIGITUNDER 9 (base) — digits 5 (9.00%), 6 (9.00%), 7 (9.00%), 8 (9.00%) at or below 10%"):
        assert needle in text, needle
    assert "DIGITUNDER 5 (recovery) —" not in text            # the passing contract is not listed as failed


def test_stop_message_format(bots):
    fmt = MessageFormatter(bots, "Asia/Colombo")
    v = predator_verdict(bots, FAILING)
    assert v.status == "WAIT" and v.contracts[0].failing_digits == [8]
    text = fmt.stop(v, T)
    for needle in ("🔴 STOP SIGNAL", "Bot: Predator", "Market: Volatility 10 (1s) Index", "Symbol: 1HZ10V",
                   "Contract (main): DIGITOVER, barrier 3, 1 tick", "Winning digits: 4, 5, 6, 7, 8, 9",
                   "• Digit 8: 9.80% ❌", "• Digit 4: 11.20% ✅",
                   "Result: FAIL ❌ — digit 8 (9.80%) at or below 10%",
                   "Signal status: STOP — 1 of 1 contract FAILED",
                   "• DIGITOVER 3 (main) — digit 8 (9.80%) at or below 10%",
                   "ACTION: Stop running Predator on 1HZ10V."):
        assert needle in text, needle
    assert "(critical)" not in text.split("\n")[0]
    assert fmt.stop(v, T, critical=True).startswith("🔴 STOP SIGNAL (critical)")


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

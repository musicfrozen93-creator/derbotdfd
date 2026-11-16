"""Telegram TEST mode: send clearly labelled TEST START / TEST STOP messages.

Safety
* The messages go straight to the Telegram Bot API. The SQLite database (signal state,
  notification outbox) is never opened, so real signal states cannot change and nothing is
  queued for the running service.
* No validation is run and no statistics are shown, so the message cannot be mistaken for a
  real result.
* The bot token is only used inside the request URL and is never printed.
"""
from __future__ import annotations

from app.signals.telegram import MAX_LEN, MessageFormatter, TelegramSender

BANNER = "🧪 TEST MESSAGE — NOT A REAL SIGNAL"
FOOTER = ("This message only checks that Telegram delivery works. No market data was analysed, "
          "no signal state was changed, and nothing was traded. Do NOT run any bot because of it.")


def build_test_messages(fmt: MessageFormatter, bot_key: str, symbol: str, now: float) -> list[tuple[str, str]]:
    """Return [(kind, text)] for the TEST START and TEST STOP messages."""
    bot = fmt.bots[bot_key]
    common = [f"Bot: {bot.name}", "", f"Symbol: {symbol}", "",
              f"Contract: {bot.contract_summary}", f"Duration: {fmt._durations(bot)}", ""]
    start = [BANNER, "", "🟢 START SIGNAL (TEST)", ""] + common + [
        "Status: TEST — no validation was run", "", FOOTER, "", f"Time: {fmt.ts(now)}"]
    stop = [BANNER, "", "🔴 STOP SIGNAL (TEST)", ""] + common + [
        "Reason: TEST — no validation was run", "", FOOTER, "", f"Time: {fmt.ts(now)}"]
    return [("TEST_START", "\n".join(start)[:MAX_LEN]), ("TEST_STOP", "\n".join(stop)[:MAX_LEN])]


def send_test_messages(sender: TelegramSender, messages: list[tuple[str, str]]) -> list[tuple[str, str | None]]:
    """Send each message directly (no outbox). Returns [(kind, error or None)]."""
    results = []
    for kind, text in messages:
        try:
            sender._transport(text)
            results.append((kind, None))
        except Exception as exc:  # the error text never contains the token (see _http_send)
            results.append((kind, str(exc)))
            break
    return results

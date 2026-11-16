#!/usr/bin/env python3
"""Telegram TEST mode.

Sends two clearly labelled TEST messages (a TEST START and a TEST STOP) with the credentials
from .env / the environment. The database is never opened, so real signal states cannot
change. The bot token is never printed.

    python scripts/test_telegram.py                      # send TEST START + TEST STOP
    python scripts/test_telegram.py --print              # show the messages, send nothing
    python scripts/test_telegram.py --bot delta --symbol R_10
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.signals.telegram import MessageFormatter, TelegramSender  # noqa: E402
from app.signals.telegram_testmode import build_test_messages, send_test_messages  # noqa: E402
from app.strategies.loader import load_strategies  # noqa: E402


def main(argv=None, *, transport=None) -> int:
    ap = argparse.ArgumentParser(description="Send TEST START / TEST STOP messages to Telegram")
    ap.add_argument("--bot", default="aria", help="bot key used in the test text (default aria)")
    ap.add_argument("--symbol", default="1HZ10V", help="symbol used in the test text (default 1HZ10V)")
    ap.add_argument("--print", dest="print_only", action="store_true", help="print the messages, send nothing")
    args = ap.parse_args(argv)

    s = load_settings()
    bots = {b.key: b for b in load_strategies(s.path(s.strategies.dir))}
    if args.bot not in bots:
        print(f"unknown bot '{args.bot}'. Known: {', '.join(sorted(bots))}")
        return 3
    fmt = MessageFormatter(bots, str(s.telegram.timezone))
    messages = build_test_messages(fmt, args.bot, args.symbol, time.time())

    if args.print_only:
        for kind, text in messages:
            print(f"----- {kind} -----\n{text}\n")
        print("(--print: nothing was sent)")
        return 0
    if not s.telegram_configured:
        print("TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID are not set (.env or environment). Nothing sent.")
        return 1
    sender = TelegramSender(None, s.telegram_token, s.telegram_chat_id,
                            timeout=float(s.telegram.send_timeout_s), transport=transport)
    for kind, err in send_test_messages(sender, messages):
        if err:
            print(f"{kind}: send failed: {err}")
            return 2
        print(f"{kind}: sent")
    print("Both TEST messages were sent. Real signal states were not touched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

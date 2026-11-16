#!/usr/bin/env python3
"""Find your Telegram chat id with the bot token already stored in .env.

1. Put TELEGRAM_BOT_TOKEN in .env (leave TELEGRAM_CHAT_ID empty for now).
2. In Telegram, open your bot and send it any message (for a channel: add the bot as admin and post).
3. Run:  python scripts/telegram_chat_id.py

The token is read from .env and never printed, so it does not end up in a URL you type or in
your shell history. Read-only: it calls getUpdates and sends nothing.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402


def main() -> int:
    s = load_settings()
    if not s.telegram_token:
        print("TELEGRAM_BOT_TOKEN is not set in .env")
        return 1
    url = f"https://api.telegram.org/bot{s.telegram_token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"Telegram HTTP {exc.code} (check the token)")
        return 2
    except urllib.error.URLError as exc:
        print(f"network error: {exc.reason}")
        return 2
    chats = {}
    for upd in body.get("result") or []:
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            chats[chat["id"]] = (chat.get("type"), chat.get("title") or chat.get("username") or
                                 chat.get("first_name") or "")
    if not chats:
        print("No messages found. Send your bot a message in Telegram first, then run this again.")
        return 3
    print("Chats that messaged the bot (use the id as TELEGRAM_CHAT_ID):")
    for cid, (ctype, name) in chats.items():
        print(f"  {cid}   {ctype}   {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

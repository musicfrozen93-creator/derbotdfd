"""Telegram TEST mode: labelled TEST messages, no database access, token never printed."""
import hashlib
import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

from app.signals.telegram import MessageFormatter, TelegramSender
from app.signals.telegram_testmode import build_test_messages
from app.storage.database import init_db

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456789:AAEhBOweik9ai2o4ifjfi4jf4jf4jf4jf4j"


def load_script():
    spec = importlib.util.spec_from_file_location("tg_script", ROOT / "scripts" / "test_telegram.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "live.db"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f'storage: {{db_path: "{db.as_posix()}"}}\n', encoding="utf-8")
    monkeypatch.setenv("ANALYZER_CONFIG", str(cfg))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    return db


def test_messages_are_clearly_labelled_and_carry_no_statistics(bots):
    fmt = MessageFormatter(bots, "Asia/Colombo")
    msgs = build_test_messages(fmt, "delta", "R_10", 1_800_000_000)
    assert [k for k, _ in msgs] == ["TEST_START", "TEST_STOP"]
    start, stop = msgs[0][1], msgs[1][1]
    for text in (start, stop):
        assert text.startswith("🧪 TEST MESSAGE — NOT A REAL SIGNAL")
        assert "Do NOT run any bot" in text and "no signal state was changed" in text
        assert "Delta" in text and "R_10" in text and "Asia/Colombo" in text
        assert "%" not in text and "Win rate" not in text and "Break-even" not in text
    assert "START SIGNAL (TEST)" in start and "STOP SIGNAL (TEST)" in stop


def test_script_sends_both_messages_without_touching_the_database(env, capsys, bots):
    # a database holding a live active signal and a pending notification
    conn = init_db(env)
    conn.execute("INSERT INTO signal_state(bot, symbol, active) VALUES ('delta','R_10',1)")
    conn.execute("INSERT INTO notifications(kind, dedupe_key, text, created_at) VALUES ('START','k','t',1)")
    conn.close()
    before = hashlib.sha256(env.read_bytes()).hexdigest()
    sent = []
    rc = load_script().main(["--bot", "delta", "--symbol", "R_10"], transport=sent.append)
    out = capsys.readouterr().out
    assert rc == 0 and len(sent) == 2
    assert "TEST_START: sent" in out and "TEST_STOP: sent" in out
    assert TOKEN not in out and TOKEN.split(":")[1] not in out
    assert hashlib.sha256(env.read_bytes()).hexdigest() == before          # byte-for-byte unchanged


def test_script_print_mode_sends_nothing(env, capsys):
    sent = []
    rc = load_script().main(["--print"], transport=sent.append)
    out = capsys.readouterr().out
    assert rc == 0 and sent == [] and "nothing was sent" in out and "TEST MESSAGE" in out
    assert not env.exists()


def test_script_without_credentials_sends_nothing(env, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    sent = []
    assert load_script().main([], transport=sent.append) == 1 and sent == []


def test_send_failure_is_reported_without_the_token(env, monkeypatch, capsys):
    def http_error(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"ok":false,"description":"Unauthorized"}'))
    monkeypatch.setattr("urllib.request.urlopen", http_error)
    rc = load_script().main([])                  # real _http_send path, mocked network
    out = capsys.readouterr().out
    assert rc == 2 and "Telegram HTTP 401: Unauthorized" in out
    assert TOKEN not in out and TOKEN.split(":")[1] not in out


def test_unknown_bot_is_rejected(env, capsys):
    assert load_script().main(["--bot", "nope"], transport=lambda t: None) == 3

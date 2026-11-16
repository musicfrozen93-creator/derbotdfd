"""Structured, rotating, secret-redacting logging (UTC timestamps)."""
from __future__ import annotations

import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Telegram bot tokens look like 123456789:AA... (35 chars after the colon).
_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


class RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        red = _TOKEN_RE.sub("<redacted>", msg)
        for secret in self.secrets:
            red = red.replace(secret, "<redacted>")
        if red != msg or record.args:
            record.msg = red
            record.args = None
        return True


class UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):  # noqa: N802
        ct = self.converter(record.created)
        return time.strftime("%Y-%m-%dT%H:%M:%S", ct) + f".{int(record.msecs):03d}Z"


FORMAT = "%(asctime)s level=%(levelname)s logger=%(name)s %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO", max_bytes: int = 10 * 1024 * 1024,
                  backups: int = 10, secrets: list[str] | None = None) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = UTCFormatter(FORMAT)
    redact = RedactFilter(secrets)
    fh = RotatingFileHandler(log_dir / "analyzer.log", maxBytes=max_bytes, backupCount=backups,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(redact)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.addFilter(redact)
    root.addHandler(fh)
    root.addHandler(sh)
    # websockets is chatty at DEBUG; keep it at WARNING.
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _fmt_value(value) -> str:
    s = str(value)
    if not s or any(c in s for c in ' ="'):
        s = '"' + s.replace('"', "'") + '"'
    return s


def event(logger: logging.Logger, event_name: str, level: int = logging.INFO, /, **fields) -> None:
    """Log a structured event: ``event=<event_name> key=value ...``."""
    parts = [f"event={event_name}"] + [f"{k}={_fmt_value(v)}" for k, v in fields.items()]
    logger.log(level, " ".join(parts))

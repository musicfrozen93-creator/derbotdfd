"""Configuration loading: config.yaml + environment variables (.env).

Secrets (Telegram token / chat id) only ever come from the environment and are
never included in repr() or log output.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv is optional at import time (tests run without it)
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "deriv": {
        "ws_url": "wss://api.derivws.com/trading/v1/options/ws/public",
        "request_timeout_s": 30,
        "open_timeout_s": 20,
        "ping_interval_s": 30,
        "idle_reconnect_s": 60,
        "reconnect_backoff_min_s": 1,
        "reconnect_backoff_max_s": 60,
        "rate_limit_general_per_min": 150,
        "rate_limit_proposal_per_min": 30,
    },
    "discovery": {
        "interval_minutes": 360,
        "markets": ["synthetic_index"],
        "include_submarkets": ["random_index"],        # Deriv's Continuous Indices only
        "one_second_symbols": ["1HZ10V", "1HZ50V"],    # 1-second indices the bot XML files use
        "exclude_symbols": [],
        "priority_symbols": ["R_100", "R_10", "1HZ10V", "1HZ50V"],
        "max_symbols": 40,
    },
    "collection": {
        "flush_interval_s": 1.0,
        "bootstrap_target_ticks": 400000,
        "bootstrap_max_days": 14,
        "history_page_size": 1000,
        "gap_audit_interval_minutes": 10,
        "gap_audit_lookback_hours": 6,
        "retention_days": 30,
    },
    "data_quality": {
        "gap_factor": 10,
        "missing_factor": 1.5,
        "stale_factor": 10,
        "critical_stale_s": 120,
        "critical_disconnect_s": 120,
    },
    "analysis": {
        "interval_s": 60,
        "risk_streak": 6,
    },
    "signal_rule": {
        "mode": "individual",
        "threshold_pct": 10,        # legacy digit rule, now shown for information only
        "window_ticks": 500,        # recent window (statistics, legacy rule, minimum data)
        "contracts": "all",
    },
    "evaluation": {
        "long_window_ticks": 100000,
        "confidence": 0.99,
        "min_outcomes": 100,
        "entry_gap_ticks": 1,
        "start_requires": ["entry", "statistics", "economics", "risk"],
    },
    "economics": {
        "currency": "USD",
        "refresh_hours": 6,
        "max_quote_age_hours": 24,
        "retry_seconds": 60,
    },
    "risk": {
        "bankroll_usd": None,
        "bankroll_per_bot": {},
        "base_stakes": {},
        "max_payout_override": None,
        "min_stake_override": None,
        "max_ladder_steps": 60,
    },
    "signals": {
        "start_confirm_cycles": 3,
        "stop_confirm_cycles": 3,
        "restart_cooldown_minutes": 30,
        "startup_grace_s": 300,
        "offline_stop_s": 120,
        "max_starts_per_bot": 5,
        "start_limit_window_hours": 12,
    },
    "telegram": {
        "enabled": True,
        "timezone": "Asia/Colombo",
        "daily_summary": False,
        "daily_summary_time": "08:00",
        "send_timeout_s": 15,
        "max_attempts": 20,
        "critical_alerts": True,
        "error_alert_cooldown_minutes": 60,
    },
    "storage": {"db_path": "data/analyzer.db"},
    "logging": {"dir": "logs", "level": "INFO", "max_bytes": 10485760, "backups": 10},
    "strategies": {"dir": "strategies", "enabled": ["all"]},
    "heartbeat": {"file": "data/heartbeat.json", "interval_s": 30, "silence_alert_minutes": 5},
}


class Section(dict):
    """dict with attribute access for nested config sections."""

    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as exc:  # pragma: no cover - programming error
            raise AttributeError(key) from exc
        return Section(value) if isinstance(value, dict) else value


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Settings:
    """Resolved runtime settings."""

    def __init__(self, data: dict, root: Path, telegram_token: str | None,
                 telegram_chat_id: str | None, dry_run: bool) -> None:
        self.data = Section(data)
        self.root = root
        self._telegram_token = telegram_token or None
        self.telegram_chat_id = telegram_chat_id or None
        self.dry_run = dry_run

    def __getattr__(self, key: str) -> Any:
        if key in ("data", "root"):
            raise AttributeError(key)
        return getattr(self.data, key)

    @property
    def telegram_token(self) -> str | None:
        return self._telegram_token

    @property
    def telegram_configured(self) -> bool:
        return bool(self._telegram_token and self.telegram_chat_id)

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    def __repr__(self) -> str:  # never leak secrets
        return (f"Settings(root={self.root}, telegram_configured={self.telegram_configured}, "
                f"dry_run={self.dry_run})")


def load_settings(config_path: str | os.PathLike | None = None, *, dry_run: bool | None = None,
                  env_file: str | os.PathLike | None = None) -> Settings:
    root = PROJECT_ROOT
    if load_dotenv is not None:
        load_dotenv(env_file or (root / ".env"), override=False)
    cfg_path = Path(config_path or os.environ.get("ANALYZER_CONFIG") or (root / "config.yaml"))
    user_cfg: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh) or {}
    data = deep_merge(DEFAULTS, user_cfg)
    if dry_run is None:
        dry_run = os.environ.get("ANALYZER_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    return Settings(
        data=data,
        root=root,
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        dry_run=bool(dry_run),
    )

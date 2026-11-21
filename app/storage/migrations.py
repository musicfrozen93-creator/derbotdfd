"""Versioned, reversible schema migrations (applied after the base schema in ``init_db``).

Rules:
* a migration only ADDS tables / indexes; it never alters or deletes an existing table, so every
  record written by an older version stays readable and untouched;
* ``up`` and ``down`` each run inside one transaction (all or nothing); the version is recorded in
  ``meta.schema_version`` and, while v4+ is applied, in ``schema_migrations``;
* ``down`` removes only what its ``up`` created.

Roll back to the v3 schema (keeps all v1-v3 data):  ``python scripts/migrate.py --to 3``
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.storage.database import stored_schema_version, transaction


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: tuple[str, ...]
    down: tuple[str, ...]


V4 = Migration(
    4, "strategy alignment: quotes, platform limits, strategy evaluations, evaluation history",
    up=(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY, name TEXT, applied_at INTEGER)""",
        # latest Deriv proposal answer per exact contract and stake (successful quote or refusal)
        """CREATE TABLE IF NOT EXISTS quotes (
               symbol TEXT NOT NULL, contract_type TEXT NOT NULL, barrier TEXT NOT NULL, duration INTEGER NOT NULL,
               stake TEXT NOT NULL, ok INTEGER NOT NULL, payout TEXT, ask_price TEXT, error_code TEXT,
               error_message TEXT, reported_payout TEXT, max_payout TEXT, min_stake TEXT, fetched_at INTEGER NOT NULL,
               PRIMARY KEY (symbol, contract_type, barrier, duration, stake))""",
        # platform limits stated in Deriv's own refusal messages (public quote context)
        """CREATE TABLE IF NOT EXISTS platform_limits (
               symbol TEXT NOT NULL, contract_type TEXT NOT NULL, max_payout TEXT, min_stake TEXT, source TEXT,
               context TEXT, observed_at INTEGER, PRIMARY KEY (symbol, contract_type))""",
        # latest bot-specific verdict per combination: entry / statistics / economics / risk / decision
        """CREATE TABLE IF NOT EXISTS strategy_evaluations (
               bot TEXT NOT NULL, symbol TEXT NOT NULL, decision TEXT, entry_status TEXT, statistics_status TEXT,
               economics_status TEXT, risk_status TEXT, critical_kind TEXT, reasons TEXT, detail TEXT,
               evaluated_at INTEGER, PRIMARY KEY (bot, symbol))""",
        # latest per-leg statistics and economics
        """CREATE TABLE IF NOT EXISTS leg_evaluations (
               bot TEXT NOT NULL, symbol TEXT NOT NULL, leg_id TEXT NOT NULL, contract TEXT, used_when TEXT,
               stake TEXT, triggers_recent INTEGER, settled_recent INTEGER, wins_recent INTEGER,
               settled_long INTEGER, wins_long INTEGER, win_rate_long REAL, ci_low REAL, ci_high REAL,
               p_fair REAL, statistics_status TEXT, payout TEXT, break_even REAL, ev_fair REAL, ev_low REAL,
               ev_high REAL, economics_status TEXT, legacy_rule TEXT, evaluated_at INTEGER,
               PRIMARY KEY (bot, symbol, leg_id))""",
        # append-only history: one row whenever a combination's verdict tuple changes
        """CREATE TABLE IF NOT EXISTS evaluation_history (
               id INTEGER PRIMARY KEY AUTOINCREMENT, bot TEXT NOT NULL, symbol TEXT NOT NULL,
               evaluated_at INTEGER NOT NULL, decision TEXT, entry_status TEXT, statistics_status TEXT,
               economics_status TEXT, risk_status TEXT, reasons TEXT)""",
        "CREATE INDEX IF NOT EXISTS idx_evaluation_history ON evaluation_history(bot, symbol, evaluated_at)",
    ),
    down=(
        "DROP INDEX IF EXISTS idx_evaluation_history",
        "DROP TABLE IF EXISTS evaluation_history",
        "DROP TABLE IF EXISTS leg_evaluations",
        "DROP TABLE IF EXISTS strategy_evaluations",
        "DROP TABLE IF EXISTS platform_limits",
        "DROP TABLE IF EXISTS quotes",
        "DROP TABLE IF EXISTS schema_migrations",
    ),
)

MIGRATIONS: tuple[Migration, ...] = (V4,)
BASE_VERSION = 3                 # the schema created by database.SCHEMA (+ the v3 ledger back-fill)
LATEST = MIGRATIONS[-1].version


def _set_version(conn, version: int) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(version),))


def migrate(conn, target: int | None = None) -> list[int]:
    """Apply every pending ``up`` (each in its own transaction). Returns the versions applied."""
    target = LATEST if target is None else target
    current = stored_schema_version(conn) or BASE_VERSION
    done = []
    for m in MIGRATIONS:
        if current < m.version <= target:
            with transaction(conn, f"migrate_up_v{m.version}"):
                for sql in m.up:
                    conn.execute(sql)
                conn.execute("INSERT OR REPLACE INTO schema_migrations(version, name, applied_at) VALUES (?,?,?)",
                             (m.version, m.name, int(time.time())))
                _set_version(conn, m.version)
            current = m.version
            done.append(m.version)
    return done


def rollback(conn, to_version: int) -> list[int]:
    """Apply ``down`` for every migration above ``to_version`` (newest first). Never touches v1-v3 data."""
    if to_version < BASE_VERSION:
        raise ValueError(f"cannot roll back below the base schema v{BASE_VERSION}")
    current = stored_schema_version(conn) or BASE_VERSION
    done = []
    for m in reversed(MIGRATIONS):
        if to_version < m.version <= current:
            with transaction(conn, f"migrate_down_v{m.version}"):
                for sql in m.down:
                    conn.execute(sql)
                _set_version(conn, m.version - 1)
            current = m.version - 1
            done.append(m.version)
    return done

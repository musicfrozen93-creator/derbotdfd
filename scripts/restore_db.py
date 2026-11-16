#!/usr/bin/env python3
"""Restore the SQLite database from a backup made by backup_db.py.

Run it ONLY while the analyzer is stopped (in Docker: from a one-off container that mounts
the data volume, see docs/PORTAINER_DEPLOYMENT.md). It refuses to run if the heartbeat
shows the service was alive in the last 2 minutes.

    python scripts/restore_db.py /app/data/backups/analyzer-20261116-120000.db

Steps: integrity-check the backup -> keep the current database as
``analyzer.db.before-restore-<time>`` -> remove the old -wal/-shm files -> copy the backup in.

After a restore the analyzer sees the backup's "last alive" time. If that is 120 s or more in
the past, any signal that was active in the backup gets a critical STOP on start — the safe
outcome, because the analyzer did not monitor it in between.
"""
import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.monitoring.heartbeat import read_heartbeat  # noqa: E402


def restore(backup: Path, db: Path, heartbeat: Path, force: bool = False, now: float | None = None) -> Path | None:
    now = now or time.time()
    hb = read_heartbeat(heartbeat)
    if hb and now - float(hb.get("written_at") or 0) < 120 and not force:
        raise RuntimeError("the analyzer looks like it is running (heartbeat < 2 min old); stop it first")
    if not backup.exists():
        raise FileNotFoundError(f"backup not found: {backup}")
    con = sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA quick_check").fetchone()[0]
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if ok != "ok" or not {"ticks", "signal_state", "notifications"} <= tables:
        raise RuntimeError(f"backup is not a valid analyzer database (check={ok}, tables={sorted(tables)})")
    db.parent.mkdir(parents=True, exist_ok=True)
    kept = None
    if db.exists():
        kept = db.with_name(f"{db.name}.before-restore-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}")
        db.replace(kept)
    for suffix in ("-wal", "-shm"):
        side = db.with_name(db.name + suffix)
        if side.exists():
            if kept is not None:
                side.replace(kept.with_name(kept.name + suffix))
            else:
                side.unlink()
    shutil.copy2(backup, db)
    return kept


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Restore the analyzer database from a backup")
    ap.add_argument("backup", help="path to analyzer-YYYYmmdd-HHMMSS.db")
    ap.add_argument("--force", action="store_true", help="skip the 'service is running' check")
    args = ap.parse_args(argv)
    s = load_settings()
    db = s.path(s.storage.db_path)
    try:
        kept = restore(Path(args.backup), db, s.path(s.heartbeat.file), force=args.force)
    except Exception as exc:
        print(f"restore failed: {exc}")
        return 1
    print(f"restored {args.backup} -> {db}")
    if kept:
        print(f"previous database kept as {kept}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

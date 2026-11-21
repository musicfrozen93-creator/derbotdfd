#!/usr/bin/env python3
"""Database schema status, upgrade and rollback.

    python scripts/migrate.py --status          # recorded version and the migrations available
    python scripts/migrate.py                   # upgrade to the latest version
    python scripts/migrate.py --to 3            # roll back to v3 (drops only the v4 tables)

Stop the analyzer first and back up the database (scripts/backup_db.py). A rollback removes only
tables that a migration created; ticks, signal history, the START ledger and notifications are kept.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.storage import migrations  # noqa: E402
from app.storage.database import connect, init_db, stored_schema_version  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="database path (default: storage.db_path from config.yaml)")
    ap.add_argument("--to", type=int, help="target schema version")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)
    s = load_settings()
    path = Path(args.db) if args.db else s.path(s.storage.db_path)
    if args.status:
        if not path.exists():
            print(f"no database at {path}")
            return 1
        conn = connect(path, readonly=True)
        print(f"{path}: schema v{stored_schema_version(conn)}; latest v{migrations.LATEST}")
        for m in migrations.MIGRATIONS:
            print(f"  v{m.version}: {m.name}")
        return 0
    target = migrations.LATEST if args.to is None else args.to
    current = None
    if path.exists():
        conn = connect(path)
        current = stored_schema_version(conn)
        conn.close()
    if current is not None and target < current:
        conn = connect(path)
        done = migrations.rollback(conn, target)
        print(f"rolled back {done} -> v{stored_schema_version(conn)}")
    else:
        conn = init_db(path, migrate_to=target)
        print(f"schema v{stored_schema_version(conn)}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

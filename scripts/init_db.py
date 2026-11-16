#!/usr/bin/env python3
"""Create / migrate the SQLite database (WAL mode). Safe to run repeatedly."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.storage.database import init_db  # noqa: E402


def main() -> int:
    s = load_settings()
    path = s.path(s.storage.db_path)
    conn = init_db(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    conn.close()
    print(f"database: {path}\njournal_mode: {mode}\ntables: {', '.join(tables)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

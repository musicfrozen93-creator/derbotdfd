#!/usr/bin/env python3
"""Consistent online backup of the SQLite database (safe while the analyzer is running).

Uses SQLite's backup API, which copies a consistent snapshot even in WAL mode, then runs
``PRAGMA quick_check`` on the copy. Backups go to ``<data dir>/backups/`` (inside the Docker
data volume) unless --out-dir is given.

    python scripts/backup_db.py                 # one backup, keep the newest 3
    python scripts/backup_db.py --keep 5
    python scripts/backup_db.py --out-dir /tmp  # any writable directory

In Docker:  docker exec deriv-analyzer python scripts/backup_db.py
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402


def backup(db: Path, out_dir: Path, keep: int = 3, now: float | None = None) -> Path:
    if not db.exists():
        raise FileNotFoundError(f"no database at {db}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now or time.time()))
    target = out_dir / f"analyzer-{stamp}.db"
    tmp = target.with_suffix(".db.part")
    src = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=60)
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst, pages=4096, sleep=0.05)     # small steps: the live service keeps writing
        ok = dst.execute("PRAGMA quick_check").fetchone()[0]
        dst.execute("PRAGMA journal_mode=DELETE")   # a single self-contained file
    finally:
        dst.close()
        src.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"integrity check of the backup failed: {ok}")
    tmp.replace(target)
    olds = sorted(out_dir.glob("analyzer-*.db"))
    for old in olds[:-keep] if keep > 0 else []:
        old.unlink()
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Back up the analyzer SQLite database")
    ap.add_argument("--out-dir", help="directory for the backup (default: <data dir>/backups)")
    ap.add_argument("--keep", type=int, default=3,
                    help="backups to keep in that directory (each is ~the database size, up to ~1.2 GB)")
    args = ap.parse_args(argv)
    s = load_settings()
    db = s.path(s.storage.db_path)
    out_dir = Path(args.out_dir) if args.out_dir else db.parent / "backups"
    try:
        target = backup(db, out_dir, args.keep)
    except Exception as exc:
        print(f"backup failed: {exc}")
        return 1
    print(f"backup written: {target} ({target.stat().st_size / 1e6:.1f} MB, integrity ok)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Tiny versioned migration runner.

Each `migrations/NNNN_name.sql` file runs once, in order, inside a transaction. The applied
version is tracked in SQLite's `PRAGMA user_version`. (The SDD proposes Alembic; plain SQL
files keep the FTS5 virtual table and triggers readable and package cleanly with PyInstaller.)
"""

from __future__ import annotations

from pathlib import Path

from app.db.database import Database

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def migrate(db: Database) -> int:
    files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    with db._write_lock:
        conn = db._writer
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        for f in files:
            version = int(f.name[:4])
            if version <= current:
                continue
            sql = f.read_text(encoding="utf-8")
            try:
                conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            current = version
    return current

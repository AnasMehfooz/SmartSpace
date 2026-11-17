"""SQLite access: one writer connection behind a lock, per-thread reader connections (SDD 15.4)."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


def utcnow() -> str:
    """ISO 8601 UTC timestamp with a Z suffix, second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ns_to_iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    dt = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._write_lock = threading.RLock()
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        self._writer = self._open()
        self._writer.execute("PRAGMA journal_mode = WAL")

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), timeout=30, check_same_thread=False, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = NORMAL")
        with self._all_lock:
            self._all.append(conn)
        return conn

    # ---- writes -------------------------------------------------------------------------
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Serialized write transaction. Nested calls on the same thread join the outer one."""
        with self._write_lock:
            conn = self._writer
            if conn.in_transaction:
                yield conn
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Cursor:
        with self.write() as conn:
            return conn.execute(sql, params)

    # ---- reads --------------------------------------------------------------------------
    def read(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def query(self, sql: str, params: Sequence[Any] | dict = ()) -> list[sqlite3.Row]:
        return self.read().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] | dict = ()) -> sqlite3.Row | None:
        return self.read().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | dict = ()) -> Any:
        row = self.read().execute(sql, params).fetchone()
        return row[0] if row is not None else None

    # ---- maintenance --------------------------------------------------------------------
    def checkpoint(self) -> None:
        with self._write_lock:
            self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def vacuum(self) -> None:
        with self._write_lock:
            self._writer.execute("VACUUM")
            self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total

    def close(self) -> None:
        with self._all_lock:
            conns, self._all = self._all, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

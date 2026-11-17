"""Indexing pipeline (SDD 6): discover -> change detection -> extract -> chunk -> embed -> summarize.

Workers: `index_cpu_threads` extract threads, 1 embedding thread (batching), 1 LLM (summary) thread.
Jobs live in the `jobs` table so a crash or restart resumes the queue (running -> queued).
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import numpy as np
import psutil
import xxhash

from app.db import utcnow
from app.errors import AppError
from app.logging_setup import get_logger
from app.services.chunker import chunk_segments
from app.services.extractors import ExtractError, extract, kind_for_ext
from app.services.extractors.image_ocr import OcrEngine
from app.services.hardware import battery_status
from app.services.language import detect_language
from app.services.paths import ExclusionMatcher, is_hidden, is_link_or_junction, is_within

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("indexer")

TOPIC = "index"
PRIORITY_USER = 1
PRIORITY_CHANGED = 5
PRIORITY_BACKLOG = 6
PRIORITY_SUMMARY = 8
BACKOFF_S = (5, 30, 120)
MAX_ATTEMPTS = 3
EMBED_FILES_PER_BATCH = 8
EMBED_TEXTS_PER_CALL = 128
SCAN_BATCH = 200
MB = 1024 * 1024


def hash_file(path: Path) -> str:
    h = xxhash.xxh3_128()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(4 * MB)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _later(seconds: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ext_of(name: str) -> str:
    _, dot, ext = name.rpartition(".")
    return ext.lower() if dot and _ else ""


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class ScanItem:
    rel: str
    name: str
    ext: str
    size: int
    mtime_ns: int


class RetryLater(Exception):
    """The file is busy (e.g. still being written); retry later without counting an error."""


class Preempted(Exception):
    """A summary was interrupted because an interactive LLM request needs the model."""


class IndexService:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.db = ctx.db
        self.ocr = OcrEngine()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._paused: set[str] = set()
        self._active = 0  # workers currently processing a job
        self._maint = 0  # purge / maintenance in progress
        self._scan_lock = threading.Lock()
        self._scanning = False
        self._rescan_requested = False
        self._scan_cancel = threading.Event()
        self._scan_thread: threading.Thread | None = None
        self._extract_threads: dict[int, threading.Thread] = {}
        self._threads: list[threading.Thread] = []
        self._rate: deque[float] = deque(maxlen=100)
        self._last_progress = 0.0
        self._last_state: str | None = None

    # =========================================================================================
    # lifecycle
    # =========================================================================================
    def start(self) -> None:
        self._stop.clear()
        self._ensure_extract_workers()
        for name, target in (("embed", self._embed_loop), ("summary", self._summary_loop), ("index-monitor", self._monitor_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self.ctx.settings.on_change(self._on_settings_change)

    def stop(self) -> None:
        self._stop.set()
        self._scan_cancel.set()
        with self._cond:
            self._cond.notify_all()
        for t in list(self._extract_threads.values()) + self._threads + ([self._scan_thread] if self._scan_thread else []):
            t.join(timeout=5)

    def _desired_extract_workers(self) -> int:
        return max(1, int(self.ctx.settings.get("index_cpu_threads")))

    def _ensure_extract_workers(self) -> None:
        with self._cond:
            for i in range(self._desired_extract_workers()):
                t = self._extract_threads.get(i)
                if t is None or not t.is_alive():
                    t = threading.Thread(target=self._extract_loop, args=(i,), name=f"extract-{i}", daemon=True)
                    self._extract_threads[i] = t
                    t.start()
            self._cond.notify_all()

    def _on_settings_change(self, changed: dict[str, Any]) -> None:
        if "index_cpu_threads" in changed:
            self._ensure_extract_workers()
        if "summaries_enabled" in changed:
            if changed["summaries_enabled"]:
                self.queue_missing_summaries()
            else:
                self.cancel_summaries()
        if "index_on_battery" in changed and changed["index_on_battery"]:
            self._set_paused("battery", False)

    # =========================================================================================
    # state
    # =========================================================================================
    def _publish(self, event: str, data: dict[str, Any]) -> None:
        self.ctx.bus.publish(TOPIC, event, data)

    def _set_paused(self, reason: str, on: bool) -> None:
        with self._cond:
            changed = (reason in self._paused) != on
            if on:
                self._paused.add(reason)
            else:
                self._paused.discard(reason)
            self._cond.notify_all()
        if changed:
            log.info("index_pause" if on else "index_resume", reason=reason)
            self._publish_state()

    def pause(self, reason: str = "user") -> None:
        self._set_paused(reason, True)

    def resume(self, reason: str = "user") -> None:
        self._set_paused(reason, False)

    def paused_reason(self) -> str | None:
        for r in ("user", "battery", "cpu"):
            if r in self._paused:
                return r
        return None

    def is_scanning(self) -> bool:
        return self._scanning

    def _has_pipeline_jobs(self) -> bool:
        return bool(
            self.db.scalar(
                "SELECT 1 FROM jobs WHERE type IN ('extract','embed') AND state IN ('queued','running') LIMIT 1"
            )
        )

    def is_busy(self) -> bool:
        return self._scanning or (self._has_pipeline_jobs() and not self._paused)

    def state(self) -> str:
        if self._scanning:
            return "scanning"
        if self._paused and self._has_pipeline_jobs():
            return "paused"
        if self._paused and "user" in self._paused:
            return "paused"
        return "indexing" if self._has_pipeline_jobs() else "idle"

    def _publish_state(self) -> None:
        st = self.state()
        self._last_state = st
        self._publish("state_changed", {"state": st, "paused_reason": self.paused_reason()})

    def status(self) -> dict[str, Any]:
        counts = {r["status"]: r["n"] for r in self.db.query("SELECT status, COUNT(*) AS n FROM files GROUP BY status")}
        summ = {r["summary_state"]: r["n"] for r in self.db.query(
            "SELECT summary_state, COUNT(*) AS n FROM files GROUP BY summary_state")}
        total = sum(n for s, n in counts.items() if s != "deleted")
        pending = counts.get("pending", 0)
        processing = counts.get("processing", 0)
        return {
            "state": self.state(),
            "total": total,
            "indexed": counts.get("indexed", 0),
            "pending": pending,
            "processing": processing,
            "skipped": counts.get("skipped", 0),
            "errors": counts.get("error", 0),
            "summaries_done": summ.get("done", 0),
            "summaries_pending": summ.get("pending", 0),
            "summaries_failed": summ.get("failed", 0),
            "eta_s": self._eta(pending + processing),
            "paused_reason": self.paused_reason(),
            "embedding_model": self.ctx.embedders.active_model_id(),
        }

    def _eta(self, remaining: int) -> int | None:
        if remaining <= 0:
            return 0
        now = time.monotonic()
        recent = [t for t in self._rate if now - t <= 120]
        if len(recent) < 2 or recent[-1] <= recent[0]:
            return None
        rate = (len(recent) - 1) / (recent[-1] - recent[0])
        return int(remaining / rate) if rate > 0 else None

    def _progress(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress < 0.5:
            return
        self._last_progress = now
        s = self.status()
        self._publish(
            "progress",
            {k: s[k] for k in ("indexed", "total", "eta_s", "pending", "processing", "skipped", "errors",
                               "summaries_done", "summaries_pending")},
        )

    # =========================================================================================
    # job queue
    # =========================================================================================
    def _notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _enqueue(self, conn, file_id: int, job_type: str, priority: int) -> None:
        row = conn.execute(
            "SELECT id, priority FROM jobs WHERE file_id = ? AND type = ? AND state = 'queued'",
            (file_id, job_type),
        ).fetchone()
        if row is not None:
            if priority < row["priority"]:
                conn.execute("UPDATE jobs SET priority = ?, run_after = NULL WHERE id = ?", (priority, row["id"]))
        else:
            conn.execute(
                "INSERT INTO jobs (file_id, type, state, priority, attempts, created_at) VALUES (?, ?, 'queued', ?, 0, ?)",
                (file_id, job_type, priority, utcnow()),
            )
        self._notify()

    def _claim(self, types: tuple[str, ...], limit: int = 1) -> list[dict[str, Any]]:
        now = utcnow()
        qs = ",".join("?" * len(types))
        with self.db.write() as conn:
            rows = conn.execute(
                f"SELECT id, file_id, type, priority, attempts FROM jobs AS j "
                f"WHERE state = 'queued' AND type IN ({qs}) AND (run_after IS NULL OR run_after <= ?) "
                f"AND NOT EXISTS (SELECT 1 FROM jobs AS r WHERE r.file_id = j.file_id AND r.state = 'running') "
                f"ORDER BY priority, id LIMIT ?",
                (*types, now, limit * 4),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            seen_files: set[int] = set()
            for r in rows:
                if r["file_id"] in seen_files:
                    continue
                seen_files.add(r["file_id"])
                conn.execute(
                    "UPDATE jobs SET state = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?",
                    (now, r["id"]),
                )
                d = dict(r)
                d["attempts"] += 1
                claimed.append(d)
                if len(claimed) >= limit:
                    break
        return claimed

    def _finish_job(self, job_id: int) -> None:
        self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def _requeue(self, job: dict[str, Any], *, count_attempt: bool, delay_s: int = 0) -> None:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'queued', run_after = ?, attempts = attempts - ? WHERE id = ?",
                (_later(delay_s) if delay_s else None, 0 if count_attempt else 1, job["id"]),
            )
            if job["type"] in ("extract", "embed"):
                conn.execute(
                    "UPDATE files SET status = 'pending', updated_at = ? WHERE id = ? AND status = 'processing'",
                    (utcnow(), job["file_id"]),
                )
        self._notify()

    def _fail_job(self, job: dict[str, Any], exc: BaseException) -> None:
        msg = str(exc) if isinstance(exc, (ExtractError, AppError)) else f"{exc.__class__.__name__}: {exc}"
        if isinstance(exc, AppError):
            msg = exc.message
        msg = msg[:500]
        fid = job["file_id"]
        attempts = int(job["attempts"])
        now = utcnow()
        with self.db.write() as conn:
            if attempts < MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE jobs SET state = 'queued', error = ?, run_after = ? WHERE id = ?",
                    (msg, _later(BACKOFF_S[min(attempts, len(BACKOFF_S)) - 1]), job["id"]),
                )
                if job["type"] in ("extract", "embed"):
                    conn.execute("UPDATE files SET status = 'pending', updated_at = ? WHERE id = ?", (now, fid))
            else:
                conn.execute(
                    "UPDATE jobs SET state = 'failed', error = ?, finished_at = ? WHERE id = ?", (msg, now, job["id"])
                )
                if job["type"] == "summarize":
                    conn.execute("UPDATE files SET summary_state = 'failed', updated_at = ? WHERE id = ?", (now, fid))
                else:
                    conn.execute(
                        "UPDATE files SET status = 'error', error_message = ?, updated_at = ? WHERE id = ?",
                        (msg, now, fid),
                    )
        log.warning("job_failed", job_id=job["id"], file_id=fid, type=job["type"], attempts=attempts,
                    error=exc.__class__.__name__)
        log.debug("job_failed_traceback", job_id=job["id"], exc_info=exc)  # SS_LOG_LEVEL=DEBUG to see it
        if attempts >= MAX_ATTEMPTS:
            self._publish("summary_failed" if job["type"] == "summarize" else "file_error",
                          {"file_id": fid, "message": msg})
        self._progress()

    # ---- worker gating ------------------------------------------------------------------
    def _enter(self) -> bool:
        with self._cond:
            while not self._stop.is_set() and (self._paused or self._maint):
                self._cond.wait(1.0)
            if self._stop.is_set():
                return False
            self._active += 1
            return True

    def _leave(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def _idle_wait(self, seconds: float = 1.0) -> None:
        with self._cond:
            if not self._stop.is_set():
                self._cond.wait(seconds)

    @contextmanager
    def maintenance(self) -> Iterator[None]:
        """Exclusive access for purge: cancels a running scan and waits for workers to go idle."""
        self._scan_cancel.set()
        if self._scan_thread is not None:
            self._scan_thread.join(timeout=30)
        with self._cond:
            self._maint += 1
            deadline = time.monotonic() + 120
            while self._active > 0 and time.monotonic() < deadline:
                self._cond.wait(0.5)
        try:
            yield
        finally:
            with self._cond:
                self._maint -= 1
                self._cond.notify_all()
            self._scan_cancel.clear()
            self._publish_state()

    # =========================================================================================
    # discovery (full scan, single path, moves)
    # =========================================================================================
    def start_full_scan(self, priority: int = PRIORITY_BACKLOG) -> bool:
        with self._scan_lock:
            if self._scanning:
                self._rescan_requested = True
                return False
            self._scanning = True
            self._scan_cancel.clear()
            t = threading.Thread(target=self._scan_runner, args=(priority,), name="scanner", daemon=True)
            self._scan_thread = t
        t.start()
        self._publish_state()
        return True

    def _scan_runner(self, priority: int) -> None:
        t0 = time.monotonic()
        try:
            self._full_scan(priority)
        except Exception:
            log.exception("scan_failed")
        finally:
            with self._scan_lock:
                self._scanning = False
                again = self._rescan_requested
                self._rescan_requested = False
            log.info("scan_finished", duration_ms=int((time.monotonic() - t0) * 1000))
            self._publish_state()
            self._progress(force=True)
            if again and not self._stop.is_set():
                self.start_full_scan(PRIORITY_CHANGED)

    def _walk(self, root: Path, matcher: ExclusionMatcher, base: Path | None = None) -> Iterator[ScanItem]:
        """Iterative os.scandir walk; skips hidden, excluded, symlinks/junctions and the data dir."""
        root_str = str(root)
        data_dir = self.ctx.cfg.data_dir
        stack = [base or root]
        while stack:
            if self._scan_cancel.is_set() or self._stop.is_set():
                return
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        if is_hidden(e) or is_link_or_junction(e):
                            continue
                        rel = e.path[len(root_str):].lstrip("\\/").replace("\\", "/")
                        if e.is_dir(follow_symlinks=False):
                            if matcher.matches(rel) or is_within(e.path, data_dir):
                                continue
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            if matcher.matches(rel):
                                continue
                            st = e.stat(follow_symlinks=False)
                            yield ScanItem(rel, e.name, _ext_of(e.name), st.st_size, st.st_mtime_ns)
                    except OSError:
                        continue

    def _full_scan(self, priority: int) -> None:
        root = self.ctx.workspace.root()
        if root is None:
            return
        matcher = self.ctx.workspace.matcher()
        existing = {
            r["rel_path"]: dict(r)
            for r in self.db.query(
                "SELECT id, rel_path, size_bytes, mtime_ns, content_hash, status, skip_reason, ext FROM files"
            )
        }
        seen: set[str] = set()
        new_by_size: dict[int, list[tuple[int, ScanItem]]] = defaultdict(list)
        batch: list[ScanItem] = []
        for item in self._walk(root, matcher):
            seen.add(item.rel)
            batch.append(item)
            if len(batch) >= SCAN_BATCH:
                self._apply_items(root, batch, existing, priority, new_by_size)
                batch = []
        if self._scan_cancel.is_set() or self._stop.is_set():
            return
        if batch:
            self._apply_items(root, batch, existing, priority, new_by_size)
        missing = [r for rel, r in existing.items() if rel not in seen]
        if missing:
            self._handle_missing(root, missing, new_by_size)
        self.ctx.workspace.mark_scanned()

    def _apply_items(
        self,
        root: Path,
        items: list[ScanItem],
        existing: dict[str, dict[str, Any]],
        priority: int,
        new_by_size: dict[int, list[tuple[int, ScanItem]]] | None = None,
    ) -> None:
        max_bytes = int(self.ctx.settings.get("max_file_size_mb")) * MB
        # hash changed files outside the DB transaction (I/O)
        hashes: dict[str, str | None] = {}
        for it in items:
            row = existing.get(it.rel)
            if row is None or (row["size_bytes"] == it.size and row["mtime_ns"] == it.mtime_ns):
                continue
            if row["content_hash"] and kind_for_ext(it.ext) and it.size <= max_bytes and row["size_bytes"] == it.size:
                try:
                    hashes[it.rel] = hash_file(root / it.rel)
                except OSError:
                    hashes[it.rel] = None
        drop_vectors: list[int] = []
        now = utcnow()
        with self.db.write() as conn:
            for it in items:
                row = existing.get(it.rel)
                kind = kind_for_ext(it.ext)
                if row is None:
                    if kind is None:
                        status, reason = "skipped", "unsupported"
                    elif it.size > max_bytes:
                        status, reason = "skipped", "too_large"
                    else:
                        status, reason = "pending", None
                    cur = conn.execute(
                        "INSERT INTO files (rel_path, name, ext, kind, size_bytes, mtime_ns, status, skip_reason, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(rel_path) DO NOTHING",
                        (it.rel, it.name, it.ext, kind or "other", it.size, it.mtime_ns, status, reason, now, now),
                    )
                    if cur.rowcount:
                        fid = int(cur.lastrowid)
                        existing[it.rel] = {"id": fid, "rel_path": it.rel, "size_bytes": it.size, "mtime_ns": it.mtime_ns,
                                            "content_hash": None, "status": status, "skip_reason": reason, "ext": it.ext}
                        if status == "pending":
                            self._enqueue(conn, fid, "extract", priority)
                        if new_by_size is not None:
                            new_by_size[it.size].append((fid, it))
                    continue

                fid = row["id"]
                changed = row["size_bytes"] != it.size or row["mtime_ns"] != it.mtime_ns
                if row["status"] == "skipped" and row["skip_reason"] == "excluded":
                    if changed:
                        conn.execute("UPDATE files SET size_bytes=?, mtime_ns=?, updated_at=? WHERE id=?",
                                     (it.size, it.mtime_ns, now, fid))
                    continue
                if not changed:
                    if row["status"] == "pending":
                        self._enqueue(conn, fid, "extract", priority)
                    continue
                if kind is None or it.size > max_bytes:
                    reason = "unsupported" if kind is None else "too_large"
                    self._clear_derived(conn, fid)
                    drop_vectors.append(fid)
                    conn.execute(
                        "UPDATE files SET size_bytes=?, mtime_ns=?, kind=?, status='skipped', skip_reason=?, "
                        "summary_state='none', indexed_at=NULL, updated_at=? WHERE id=?",
                        (it.size, it.mtime_ns, kind or "other", reason, now, fid),
                    )
                elif hashes.get(it.rel) and hashes[it.rel] == row["content_hash"] and row["status"] in ("indexed", "skipped"):
                    conn.execute("UPDATE files SET mtime_ns=?, updated_at=? WHERE id=?", (it.mtime_ns, now, fid))
                else:
                    conn.execute(
                        "UPDATE files SET size_bytes=?, mtime_ns=?, kind=?, status='pending', skip_reason=NULL, "
                        "error_message=NULL, updated_at=? WHERE id=?",
                        (it.size, it.mtime_ns, kind, now, fid),
                    )
                    self._enqueue(conn, fid, "extract", min(priority, PRIORITY_CHANGED))
                row.update({"size_bytes": it.size, "mtime_ns": it.mtime_ns})
        if drop_vectors:
            self.ctx.vectors.delete_files(drop_vectors)
        self._progress()

    def _handle_missing(
        self, root: Path, missing: list[dict[str, Any]], new_by_size: dict[int, list[tuple[int, ScanItem]]]
    ) -> None:
        """Files that disappeared: renames/moves keep their index (matched by size + hash), others are removed."""
        renamed: set[int] = set()
        hash_cache: dict[int, str | None] = {}
        for r in missing:
            if not r["content_hash"]:
                continue
            candidates = new_by_size.get(r["size_bytes"]) or []
            for new_fid, it in list(candidates):
                if new_fid not in hash_cache:
                    try:
                        hash_cache[new_fid] = hash_file(root / it.rel)
                    except OSError:
                        hash_cache[new_fid] = None
                if hash_cache[new_fid] == r["content_hash"]:
                    with self.db.write() as conn:
                        conn.execute("DELETE FROM files WHERE id = ?", (new_fid,))
                        conn.execute(
                            "UPDATE files SET rel_path=?, name=?, ext=?, mtime_ns=?, updated_at=? WHERE id=?",
                            (it.rel, it.name, it.ext, it.mtime_ns, utcnow(), r["id"]),
                        )
                    candidates.remove((new_fid, it))
                    renamed.add(r["id"])
                    log.info("file_moved", file_id=r["id"])
                    break
        self.remove_files([r["id"] for r in missing if r["id"] not in renamed])

    def check_path(self, path: Path) -> None:
        """Change detection for one path reported by the watcher (file, folder, or deletion)."""
        root = self.ctx.workspace.root()
        if root is None or not is_within(path, root) or is_within(path, self.ctx.cfg.data_dir):
            return
        rel = str(path)[len(str(root)):].lstrip("\\/").replace("\\", "/")
        if not rel:
            self.start_full_scan(PRIORITY_CHANGED)
            return
        if any(part.startswith(".") for part in rel.split("/")):
            return
        matcher = self.ctx.workspace.matcher()
        if matcher.matches(rel):
            return
        try:
            if path.is_dir():
                items = list(self._walk(root, matcher, base=path))
                prefix = _like_escape(rel) + "/%"
                rows = self.db.query(
                    "SELECT id, rel_path, size_bytes, mtime_ns, content_hash, status, skip_reason, ext FROM files "
                    "WHERE rel_path LIKE ? ESCAPE '\\'", (prefix,))
                existing = {r["rel_path"]: dict(r) for r in rows}
                for i in range(0, len(items), SCAN_BATCH):
                    self._apply_items(root, items[i : i + SCAN_BATCH], existing, PRIORITY_CHANGED)
                seen = {it.rel for it in items}
                self.remove_files([r["id"] for rp, r in existing.items() if rp not in seen])
                return
            if path.is_file():
                st = path.stat()
                item = ScanItem(rel, path.name, _ext_of(path.name), st.st_size, st.st_mtime_ns)
                row = self.db.query_one(
                    "SELECT id, rel_path, size_bytes, mtime_ns, content_hash, status, skip_reason, ext FROM files "
                    "WHERE rel_path = ?", (rel,))
                self._apply_items(root, [item], {rel: dict(row)} if row else {}, PRIORITY_CHANGED)
                return
        except OSError:
            return
        # gone: a file or a whole folder
        rows = self.db.query(
            "SELECT id FROM files WHERE rel_path = ? OR rel_path LIKE ? ESCAPE '\\'", (rel, _like_escape(rel) + "/%"))
        self.remove_files([r["id"] for r in rows])

    def handle_move(self, src: Path, dest: Path) -> None:
        """Watcher move/rename: update rel_path and keep chunks + vectors (no re-embedding)."""
        root = self.ctx.workspace.root()
        if root is None:
            return
        if not is_within(dest, root):
            self.check_path(src)
            return
        src_rel = str(src)[len(str(root)):].lstrip("\\/").replace("\\", "/")
        dest_rel = str(dest)[len(str(root)):].lstrip("\\/").replace("\\", "/")
        matcher = self.ctx.workspace.matcher()
        if matcher.matches(dest_rel) or any(p.startswith(".") for p in dest_rel.split("/")):
            self.check_path(src)
            return
        now = utcnow()
        moved = False
        with self.db.write() as conn:
            row = conn.execute("SELECT id FROM files WHERE rel_path = ?", (src_rel,)).fetchone()
            if row is not None:
                conn.execute("DELETE FROM files WHERE rel_path = ? AND id != ?", (dest_rel, row["id"]))
                name = dest_rel.rsplit("/", 1)[-1]
                conn.execute("UPDATE files SET rel_path=?, name=?, ext=?, updated_at=? WHERE id=?",
                             (dest_rel, name, _ext_of(name), now, row["id"]))
                moved = True
            else:
                rows = conn.execute("SELECT id, rel_path FROM files WHERE rel_path LIKE ? ESCAPE '\\'",
                                    (_like_escape(src_rel) + "/%",)).fetchall()
                for r in rows:
                    new_rel = dest_rel + r["rel_path"][len(src_rel):]
                    conn.execute("DELETE FROM files WHERE rel_path = ? AND id != ?", (new_rel, r["id"]))
                    conn.execute("UPDATE files SET rel_path=?, updated_at=? WHERE id=?", (new_rel, now, r["id"]))
                moved = bool(rows)
        if moved:
            log.info("path_moved")
        self.check_path(dest)  # picks up content changes (or new files if nothing was known)

    # =========================================================================================
    # derived data helpers
    # =========================================================================================
    def _clear_derived(self, conn, file_id: int) -> None:
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM summaries WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM file_tags WHERE file_id = ? AND source = 'ai'", (file_id,))

    def remove_files(self, file_ids: list[int]) -> None:
        """A file disappeared from disk: drop its row (chunks, summary, tags, jobs cascade) and vectors."""
        if not file_ids:
            return
        self.ctx.vectors.delete_files(file_ids)
        with self.db.write() as conn:
            for i in range(0, len(file_ids), 500):
                part = file_ids[i : i + 500]
                conn.execute(f"DELETE FROM files WHERE id IN ({','.join('?' * len(part))})", part)
        for fid in file_ids:
            self._publish("file_removed", {"file_id": fid})
        self._progress()

    def _mark_skipped(self, fid: int, reason: str, st: os.stat_result | None = None, content_hash: str | None = None,
                      page_count: int | None = None) -> None:
        now = utcnow()
        with self.db.write() as conn:
            self._clear_derived(conn, fid)
            conn.execute(
                "UPDATE files SET status='skipped', skip_reason=?, error_message=NULL, summary_state='none', "
                "indexed_at=NULL, size_bytes=COALESCE(?, size_bytes), mtime_ns=COALESCE(?, mtime_ns), "
                "content_hash=COALESCE(?, content_hash), page_count=COALESCE(?, page_count), updated_at=? WHERE id=?",
                (reason, st.st_size if st else None, st.st_mtime_ns if st else None, content_hash, page_count, now, fid),
            )
        self.ctx.vectors.delete_files([fid])
        self._rate.append(time.monotonic())
        self._publish("file_skipped", {"file_id": fid, "reason": reason})

    def remove_file_index(self, fid: int) -> None:
        """DELETE /files/{id}/index: drop derived data, keep the row as skipped/excluded."""
        with self.db.write() as conn:
            conn.execute("DELETE FROM jobs WHERE file_id = ? AND state IN ('queued','failed')", (fid,))
        self._mark_skipped(fid, "excluded")

    # =========================================================================================
    # (re)queue operations
    # =========================================================================================
    def reindex_file(self, fid: int) -> None:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE files SET status='pending', skip_reason=NULL, error_message=NULL, updated_at=? WHERE id=?",
                (utcnow(), fid))
            conn.execute("DELETE FROM jobs WHERE file_id = ? AND state = 'failed'", (fid,))
            self._enqueue(conn, fid, "extract", PRIORITY_USER)
        self._publish_state()

    def reindex_all(self, reason: str = "") -> int:
        """Re-embed indexed files (chunks are kept) and re-extract everything else."""
        now = utcnow()
        with self.db.write() as conn:
            conn.execute("DELETE FROM jobs WHERE state IN ('queued','failed')")
            rows = conn.execute("SELECT id, status FROM files WHERE status IN ('indexed','pending','error','processing')").fetchall()
            for r in rows:
                conn.execute("UPDATE files SET status='pending', error_message=NULL, updated_at=? WHERE id=?", (now, r["id"]))
                self._enqueue(conn, r["id"], "embed" if r["status"] == "indexed" else "extract", PRIORITY_BACKLOG)
        log.info("reindex_all", reason=reason, files=len(rows))
        self._publish_state()
        return len(rows)

    def retry_failed(self) -> int:
        now = utcnow()
        with self.db.write() as conn:
            conn.execute("DELETE FROM jobs WHERE state = 'failed'")
            rows = conn.execute("SELECT id FROM files WHERE status = 'error'").fetchall()
            for r in rows:
                conn.execute("UPDATE files SET status='pending', error_message=NULL, updated_at=? WHERE id=?", (now, r["id"]))
                self._enqueue(conn, r["id"], "extract", PRIORITY_CHANGED)
            srows = conn.execute("SELECT id FROM files WHERE status='indexed' AND summary_state='failed'").fetchall()
        if srows and self._summaries_active():
            with self.db.write() as conn:
                for r in srows:
                    conn.execute("UPDATE files SET summary_state='pending' WHERE id=?", (r["id"],))
                    self._enqueue(conn, r["id"], "summarize", PRIORITY_SUMMARY)
        self._publish_state()
        return len(rows) + len(srows)

    def _summaries_active(self) -> bool:
        return bool(self.ctx.settings.get("summaries_enabled")) and self.ctx.models.active("llm") is not None

    def queue_missing_summaries(self) -> int:
        """Queue summaries that are missing, failed, or made with an older prompt version (SDD 6.4)."""
        if not self._summaries_active():
            return 0
        from app.services.summarizer import PROMPT_VERSION

        with self.db.write() as conn:
            rows = conn.execute(
                "SELECT f.id FROM files f LEFT JOIN summaries s ON s.file_id = f.id WHERE f.status='indexed' AND "
                "(f.summary_state IN ('none','failed','pending') OR (s.prompt_version IS NOT NULL AND s.prompt_version != ?))",
                (PROMPT_VERSION,)).fetchall()
            for r in rows:
                conn.execute("UPDATE files SET summary_state='pending' WHERE id=?", (r["id"],))
                self._enqueue(conn, r["id"], "summarize", PRIORITY_SUMMARY)
        return len(rows)

    def cancel_summaries(self) -> None:
        with self.db.write() as conn:
            conn.execute("DELETE FROM jobs WHERE type='summarize' AND state IN ('queued','failed')")
            conn.execute("UPDATE files SET summary_state='none' WHERE summary_state='pending'")

    # =========================================================================================
    # workers
    # =========================================================================================
    def _extract_loop(self, idx: int) -> None:
        while not self._stop.is_set():
            if idx >= self._desired_extract_workers():
                with self._cond:
                    if self._extract_threads.get(idx) is threading.current_thread():
                        del self._extract_threads[idx]
                return
            if not self._enter():
                return
            jobs: list[dict[str, Any]] = []
            try:
                jobs = self._claim(("extract",), 1)
                if jobs:
                    self._run_extract(jobs[0])
            except Exception:
                log.exception("extract_worker_error")
            finally:
                self._leave()
            if not jobs:
                self._idle_wait(1.0)

    def _run_extract(self, job: dict[str, Any]) -> None:
        fid = job["file_id"]
        row = self.db.query_one("SELECT * FROM files WHERE id = ?", (fid,))
        root = self.ctx.workspace.root()
        if row is None or root is None:
            self._finish_job(job["id"])
            return
        self.db.execute("UPDATE files SET status='processing', updated_at=? WHERE id=?", (utcnow(), fid))
        t0 = time.monotonic()
        try:
            self._extract_file(fid, dict(row), root / row["rel_path"], job["priority"])
            self._finish_job(job["id"])
            log.debug("extracted", file_id=fid, job_id=job["id"], duration_ms=int((time.monotonic() - t0) * 1000))
        except RetryLater:
            if job["attempts"] >= 5:  # SDD 6.6: give up on a locked file after 5 tries
                job["attempts"] = MAX_ATTEMPTS
                self._fail_job(job, RuntimeError("File is locked by another program"))
            else:
                self._requeue(job, count_attempt=True, delay_s=5)
        except ExtractError as e:  # deterministic (corrupt / unreadable format): no point retrying
            job["attempts"] = MAX_ATTEMPTS
            self._fail_job(job, e)
        except Exception as e:
            self._fail_job(job, e)

    def _extract_file(self, fid: int, row: dict[str, Any], path: Path, priority: int) -> None:
        s = self.ctx.settings
        try:
            st = path.stat()
        except FileNotFoundError:
            self.remove_files([fid])
            return
        kind = kind_for_ext(row["ext"])
        if kind is None:
            self._mark_skipped(fid, "unsupported", st)
            return
        if st.st_size > int(s.get("max_file_size_mb")) * MB:
            self._mark_skipped(fid, "too_large", st)
            return
        if st.st_size == 0:
            self._mark_skipped(fid, "empty", st)
            return
        try:
            content_hash = hash_file(path)  # also proves the file is readable (not locked mid-write)
        except PermissionError:
            raise RetryLater() from None
        result = extract(path, row["ext"], max_pdf_pages=int(s.get("max_pdf_pages")), ocr=self.ocr)
        if result.skip_reason:
            self._mark_skipped(fid, result.skip_reason, st, content_hash, result.page_count)
            return
        if result.text_chars == 0:
            self._mark_skipped(fid, "no_text" if kind == "image" else "empty", st, content_hash, result.page_count)
            return
        language = detect_language("\n".join(seg.text for seg in result.segments)[:2000])
        chunks = chunk_segments(result.segments, int(s.get("chunk_target_chars")), int(s.get("chunk_overlap_chars")))
        emb_id = self.ctx.embedders.active_model_id() or ""
        now = utcnow()
        with self.db.write() as conn:
            if conn.execute("SELECT 1 FROM files WHERE id = ?", (fid,)).fetchone() is None:
                return
            self._clear_derived(conn, fid)
            conn.executemany(
                "INSERT INTO chunks (file_id, chunk_index, source, page_start, page_end, text, char_count, "
                "embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(fid, i, c.source, c.page_start, c.page_end, c.text, c.char_count, emb_id) for i, c in enumerate(chunks)],
            )
            conn.execute(
                "UPDATE files SET size_bytes=?, mtime_ns=?, content_hash=?, language=?, page_count=?, "
                "status='processing', skip_reason=NULL, error_message=NULL, summary_state='none', updated_at=? WHERE id=?",
                (st.st_size, st.st_mtime_ns, content_hash, language, result.page_count, now, fid),
            )
            self._enqueue(conn, fid, "embed", priority)
        self.ctx.vectors.delete_files([fid])  # old vectors point at deleted chunks

    def _embed_loop(self) -> None:
        while not self._stop.is_set():
            if not self._enter():
                return
            jobs: list[dict[str, Any]] = []
            try:
                jobs = self._claim(("embed",), EMBED_FILES_PER_BATCH)
                if jobs:
                    self._run_embed(jobs)
            except Exception:
                log.exception("embed_worker_error")
            finally:
                self._leave()
            if not jobs:
                self._idle_wait(1.0)

    def _run_embed(self, jobs: list[dict[str, Any]]) -> None:
        fids = [j["file_id"] for j in jobs]
        t0 = time.monotonic()
        try:
            embedder = self.ctx.embedders.get()
            qs = ",".join("?" * len(fids))
            rows = self.db.query(
                f"SELECT id, file_id, text FROM chunks WHERE file_id IN ({qs}) ORDER BY file_id, chunk_index", fids)
            emb_id = ""
            if embedder is not None and rows:
                texts = [r["text"] for r in rows]
                parts = []
                for i in range(0, len(texts), EMBED_TEXTS_PER_CALL):
                    if self._stop.is_set():
                        raise RetryLater()
                    parts.append(embedder.embed_passages(texts[i : i + EMBED_TEXTS_PER_CALL]))
                vectors = np.vstack(parts)
                self.ctx.vectors.replace_files(
                    embedder.model_id, embedder.dim, fids, [r["id"] for r in rows], [r["file_id"] for r in rows], vectors)
                emb_id = embedder.model_id
            elif embedder is None and self.ctx.models.active("embedding") is not None:
                raise RuntimeError(f"Embedding model failed to load: {self.ctx.embedders.last_error}")
            summarize = self._summaries_active()
            now = utcnow()
            done: list[int] = []
            with self.db.write() as conn:
                for j in jobs:
                    fid = j["file_id"]
                    conn.execute("DELETE FROM jobs WHERE id = ?", (j["id"],))
                    if conn.execute("SELECT 1 FROM files WHERE id = ?", (fid,)).fetchone() is None:
                        continue
                    conn.execute("UPDATE chunks SET embedding_model = ? WHERE file_id = ?", (emb_id, fid))
                    srow = conn.execute("SELECT summary_state FROM files WHERE id = ?", (fid,)).fetchone()
                    need_summary = summarize and srow["summary_state"] in ("none", "failed")
                    conn.execute(
                        "UPDATE files SET status='indexed', indexed_at=?, error_message=NULL, updated_at=?, "
                        "summary_state = CASE WHEN ? THEN 'pending' ELSE summary_state END WHERE id=?",
                        (now, now, int(need_summary), fid),
                    )
                    if need_summary:
                        self._enqueue(conn, fid, "summarize", PRIORITY_SUMMARY)
                    done.append(fid)
            for fid in done:
                self._rate.append(time.monotonic())
                self._publish("file_indexed", {"file_id": fid})
            log.debug("embedded", files=len(done), chunks=len(rows), duration_ms=int((time.monotonic() - t0) * 1000))
            self._progress()
        except RetryLater:
            for j in jobs:
                self._requeue(j, count_attempt=False)
        except Exception as e:
            for j in jobs:
                self._fail_job(j, e)

    def _summary_loop(self) -> None:
        while not self._stop.is_set():
            if not self._summaries_active() or self.ctx.gate.busy():
                self._idle_wait(2.0)
                continue
            if not self._enter():
                return
            jobs: list[dict[str, Any]] = []
            try:
                jobs = self._claim(("summarize",), 1)
                if jobs:
                    self._run_summary(jobs[0])
            except Exception:
                log.exception("summary_worker_error")
            finally:
                self._leave()
            if not jobs:
                self._idle_wait(2.0)

    def _run_summary(self, job: dict[str, Any]) -> None:
        fid = job["file_id"]

        def should_abort() -> bool:
            return self._stop.is_set() or self.ctx.gate.busy() or bool(self._maint) or "user" in self._paused

        try:
            ok = self.ctx.summarizer.summarize_file(fid, should_abort)
            self._finish_job(job["id"])
            if ok:
                self._publish("summary_done", {"file_id": fid})
            self._progress()
        except Preempted:
            self._requeue(job, count_attempt=False)
        except Exception as e:
            self._fail_job(job, e)

    # =========================================================================================
    # resource monitor (battery / CPU auto-pause, state + progress events, vector maintenance)
    # =========================================================================================
    def _monitor_loop(self) -> None:
        proc = psutil.Process()
        ncpu = psutil.cpu_count() or 1
        psutil.cpu_percent(None)
        proc.cpu_percent(None)
        high_since: float | None = None
        low_since: float | None = None
        last_optimize_state = None
        while not self._stop.wait(2.0):
            try:
                b = battery_status()
                on_battery = b["present"] and not b["plugged"]
                self._set_paused("battery", on_battery and not self.ctx.settings.get("index_on_battery"))

                total = psutil.cpu_percent(None)
                own = proc.cpu_percent(None) / ncpu
                other = max(0.0, total - own)
                now = time.monotonic()
                if other > 85.0:
                    low_since = None
                    high_since = high_since or now
                    if now - high_since >= 10:
                        self._set_paused("cpu", True)
                else:
                    high_since = None
                    if "cpu" in self._paused:
                        low_since = low_since or now
                        if other < 70.0 and now - low_since >= 10:
                            self._set_paused("cpu", False)
                            low_since = None

                st = self.state()
                if st != self._last_state:
                    self._publish_state()
                    self.ctx.workspace.set_status(st)
                if st == "indexing":
                    self._progress()
                    last_optimize_state = "indexing"
                elif st == "idle" and last_optimize_state == "indexing":
                    last_optimize_state = "idle"
                    emb = self.ctx.embedders.active_model_id()
                    if emb:
                        self.ctx.vectors.optimize(emb)
                    self.db.checkpoint()
            except Exception:
                log.exception("monitor_error")

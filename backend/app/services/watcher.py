"""watchdog observer on the workspace root with a 2 s per-path debounce (SDD 6.6)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.logging_setup import get_logger
from app.services.paths import is_within

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("watcher")

DEBOUNCE_S = 2.0


class _Handler(FileSystemEventHandler):
    def __init__(self, svc: "WatcherService"):
        self.svc = svc

    def on_created(self, event: FileSystemEvent) -> None:
        self.svc.touch(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:  # folder "modified" events are noise
            self.svc.touch(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self.svc.touch(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self.svc.move(event.src_path, event.dest_path)


class WatcherService:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self._observer: Observer | None = None
        self._pending: dict[str, float] = {}
        self._moves: list[tuple[float, str, str]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def running(self) -> bool:
        return self._observer is not None

    def start(self) -> None:
        root = self.ctx.workspace.root()
        if root is None or not root.is_dir():
            return
        with self._lock:
            if self._observer is not None:
                return
            obs = Observer()
            obs.schedule(_Handler(self), str(root), recursive=True)
            obs.daemon = True
            try:
                obs.start()
            except Exception:
                log.exception("watcher_start_failed")
                return
            self._observer = obs
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._loop, name="watcher-debounce", daemon=True)
                self._thread.start()
        log.info("watcher_started")

    def stop(self) -> None:
        with self._lock:
            obs, self._observer = self._observer, None
            self._pending.clear()
            self._moves.clear()
        self._stop.set()
        if obs is not None:
            try:
                obs.stop()
                obs.join(timeout=5)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def _ignored(self, path: str) -> bool:
        return is_within(path, self.ctx.cfg.data_dir)

    def touch(self, path: str | bytes) -> None:
        path = path.decode() if isinstance(path, bytes) else path
        if self._ignored(path):
            return
        with self._lock:
            self._pending[path] = time.monotonic()

    def move(self, src: str | bytes, dest: str | bytes) -> None:
        src = src.decode() if isinstance(src, bytes) else src
        dest = dest.decode() if isinstance(dest, bytes) else dest
        with self._lock:
            self._pending.pop(src, None)
            self._moves.append((time.monotonic(), src, dest))

    def _loop(self) -> None:
        while not self._stop.wait(0.5):
            now = time.monotonic()
            with self._lock:
                due = [p for p, t in self._pending.items() if now - t >= DEBOUNCE_S]
                for p in due:
                    del self._pending[p]
                moves = [m for m in self._moves if now - m[0] >= DEBOUNCE_S]
                self._moves = [m for m in self._moves if now - m[0] < DEBOUNCE_S]
            for _, src, dest in moves:
                try:
                    self.ctx.indexer.handle_move(Path(src), Path(dest))
                except Exception:
                    log.exception("watcher_move_failed")
            for p in sorted(due, key=len):  # parents before children
                try:
                    self.ctx.indexer.check_path(Path(p))
                except Exception:
                    log.exception("watcher_check_failed")

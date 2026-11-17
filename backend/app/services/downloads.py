"""Model download manager (SDD 9.4).

One download at a time, others queue. httpx streaming with Range resume into tmp/<file>.part,
progress events every 500 ms, SHA-256 verification, atomic move into models/<role>/<id>/.
Every redirect hop is checked against download_allowlist.json (HTTPS only).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import httpx

from app.errors import AppError
from app.logging_setup import get_logger
from app.services.catalog import CatalogFile, CatalogModel

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("downloads")

TOPIC = "downloads"


class DownloadError(Exception):
    pass


class Cancelled(Exception):
    pass


class _Progress:
    def __init__(self, model_id: str, total: int, publish):
        self.model_id = model_id
        self.total = total
        self.done = 0
        self._publish = publish
        self._last_emit = 0.0
        self._samples: deque[tuple[float, int]] = deque(maxlen=50)

    def add(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        self._samples.append((now, self.done))
        if now - self._last_emit >= 0.5:
            self._last_emit = now
            self._publish("progress", self.snapshot())

    def speed_bps(self) -> int:
        now = time.monotonic()
        recent = [(t, d) for t, d in self._samples if now - t <= 5.0]
        if len(recent) < 2:
            return 0
        (t0, d0), (t1, d1) = recent[0], recent[-1]
        return int((d1 - d0) / (t1 - t0)) if t1 > t0 else 0

    def snapshot(self) -> dict[str, Any]:
        speed = self.speed_bps()
        remaining = max(0, self.total - self.done)
        return {
            "model_id": self.model_id,
            "bytes_done": self.done,
            "bytes_total": self.total,
            "speed_bps": speed,
            "eta_s": int(remaining / speed) if speed > 0 else None,
        }


class DownloadManager:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self._queue: deque[str] = deque()
        self._cond = threading.Condition()
        self._current: str | None = None
        self._progress: _Progress | None = None
        self._cancel: set[str] = set()
        self._stop = False
        self._thread: threading.Thread | None = None
        allow = json.loads(ctx.cfg.allowlist_path.read_text(encoding="utf-8"))
        self.allow_hosts = {h.lower() for h in allow.get("hosts", [])}
        self.allow_suffixes = tuple(s.lower() for s in allow.get("host_suffixes", []))

    # ---- lifecycle ------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="downloads", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stop = True
            if self._current:
                self._cancel.add("__shutdown__")
            self._cond.notify_all()
        if self._thread:
            self._thread.join(timeout=5)

    # ---- public API -----------------------------------------------------------------------
    def enqueue(self, model_id: str) -> str:
        m = self.ctx.models.get_or_404(model_id)
        if m.bundled:
            raise AppError("VALIDATION_ERROR", "This model is bundled with the app")
        if self.ctx.models.is_installed(model_id) and self.ctx.models.files_present(m):
            raise AppError("VALIDATION_ERROR", "Model is already installed")
        remaining = self._remaining_bytes(m)
        free = self.ctx.models.disk_free_bytes()
        if free < remaining * 1.1:
            raise AppError(
                "DISK_FULL",
                "Not enough free disk space for this model",
                details={"needed_bytes": int(remaining * 1.1), "free_bytes": free},
            )
        with self._cond:
            if model_id != self._current and model_id not in self._queue:
                self._queue.append(model_id)
                self._cancel.discard(model_id)
            self._cond.notify_all()
        self.ctx.models.set_status(model_id, "downloading")
        self._publish("queued", {"model_id": model_id, "position": self._position(model_id)})
        return f"dl-{model_id}"

    def cancel(self, model_id: str) -> bool:
        self.ctx.models.get_or_404(model_id)
        with self._cond:
            if model_id in self._queue:
                self._queue.remove(model_id)
                removed = True
            elif self._current == model_id:
                self._cancel.add(model_id)
                removed = True
            else:
                removed = False
        if removed and self._current != model_id:
            self._cleanup_parts(model_id)
            self.ctx.models.set_status(model_id, "not_installed")
            self._publish("cancelled", {"model_id": model_id})
        return removed

    def is_downloading(self, model_id: str) -> bool:
        with self._cond:
            return model_id == self._current or model_id in self._queue

    def progress_snapshot(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        with self._cond:
            if self._current and self._progress:
                snap = self._progress.snapshot()
                snap["state"] = "downloading"
                out[self._current] = snap
            for i, mid in enumerate(self._queue):
                out[mid] = {"model_id": mid, "state": "queued", "position": i + 1}
        return out

    # ---- worker ---------------------------------------------------------------------------
    def _position(self, model_id: str) -> int:
        with self._cond:
            if model_id == self._current:
                return 0
            return list(self._queue).index(model_id) + 1 if model_id in self._queue else -1

    def _publish(self, event: str, data: dict[str, Any]) -> None:
        self.ctx.bus.publish(TOPIC, event, data)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stop:
                    self._cond.wait(timeout=1.0)
                if self._stop:
                    return
                model_id = self._queue.popleft()
                self._current = model_id
            m = self.ctx.catalog.get(model_id)
            try:
                if m is None:
                    raise DownloadError("Unknown model")
                self._download_model(m)
                self.ctx.models.set_status(model_id, "installed")
                log.info("download_completed", model_id=model_id)
                self._publish("completed", {"model_id": model_id})
                try:
                    self.ctx.models.auto_activate(model_id)
                except Exception:
                    log.exception("auto_activate_failed", model_id=model_id)
            except Cancelled:
                if "__shutdown__" in self._cancel:
                    self.ctx.models.set_status(model_id, "not_installed")  # keep .part for resume
                else:
                    self._cleanup_parts(model_id)
                    self.ctx.models.set_status(model_id, "not_installed")
                    self._publish("cancelled", {"model_id": model_id})
                log.info("download_cancelled", model_id=model_id)
            except Exception as e:  # keep .part files for resume
                msg = str(e) if isinstance(e, DownloadError) else f"{e.__class__.__name__}: {e}"
                self.ctx.models.set_status(model_id, "failed")
                log.warning("download_failed", model_id=model_id, error=msg[:300])
                self._publish("failed", {"model_id": model_id, "message": msg[:500]})
            finally:
                with self._cond:
                    self._current = None
                    self._progress = None
                    self._cancel.discard(model_id)

    def _remaining_bytes(self, m: CatalogModel) -> int:
        d = self.ctx.models.model_dir(m)
        total = 0
        for f in m.files:
            dest = d / f.path
            if dest.exists() and dest.stat().st_size == f.size_bytes:
                continue
            part = self._part_path(m, f)
            have = part.stat().st_size if part.exists() else 0
            total += max(0, f.size_bytes - have)
        return total

    def _part_path(self, m: CatalogModel, f: CatalogFile):
        return self.ctx.cfg.tmp_dir / f"{m.id}__{f.path.replace('/', '_')}.part"

    def _cleanup_parts(self, model_id: str) -> None:
        from app.services.purge import safe_unlink

        for part in self.ctx.cfg.tmp_dir.glob(f"{model_id}__*.part"):
            try:
                safe_unlink(self.ctx, part)
            except OSError:
                pass

    def _check_cancel(self, model_id: str) -> None:
        if model_id in self._cancel or "__shutdown__" in self._cancel:
            raise Cancelled()

    def _check_host(self, url: str) -> None:
        u = urlparse(url)
        if u.scheme != "https":
            raise DownloadError("Only HTTPS downloads are allowed")
        host = (u.hostname or "").lower()
        if host in self.allow_hosts or any(host.endswith(s) for s in self.allow_suffixes):
            return
        raise DownloadError(f"Download host not in allowlist: {host}")

    def _open(self, client: httpx.Client, url: str, headers: dict[str, str]) -> httpx.Response:
        for _ in range(10):
            self._check_host(url)
            resp = client.send(client.build_request("GET", url, headers=headers), stream=True, follow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                resp.close()
                if not loc:
                    raise DownloadError("Redirect without location")
                url = urljoin(url, loc)
                continue
            return resp
        raise DownloadError("Too many redirects")

    def _download_model(self, m: CatalogModel) -> None:
        total = sum(f.size_bytes for f in m.files)
        prog = _Progress(m.id, total, self._publish)
        with self._cond:
            self._progress = prog
        free = self.ctx.models.disk_free_bytes()
        remaining = self._remaining_bytes(m)
        if free < remaining * 1.1:
            raise DownloadError("Not enough free disk space (DISK_FULL)")
        self.ctx.cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
        timeout = httpx.Timeout(30.0, read=60.0)
        with httpx.Client(timeout=timeout, headers={"User-Agent": "SmartSpace/0.1"}) as client:
            for f in m.files:
                self._download_file(client, m, f, prog)
        self._publish("progress", prog.snapshot())

    def _download_file(self, client: httpx.Client, m: CatalogModel, f: CatalogFile, prog: _Progress) -> None:
        dest = self.ctx.models.model_dir(m) / f.path
        if dest.exists() and dest.stat().st_size == f.size_bytes:
            prog.add(f.size_bytes)
            return
        part = self._part_path(m, f)
        hasher = hashlib.sha256()
        offset = 0
        if part.exists():
            offset = part.stat().st_size
            if offset > f.size_bytes:
                part.unlink()
                offset = 0
            else:  # re-hash what we already have so the final digest covers the whole file
                with open(part, "rb") as fh:
                    while True:
                        self._check_cancel(m.id)
                        buf = fh.read(8 << 20)
                        if not buf:
                            break
                        hasher.update(buf)
        prog.add(offset)

        if offset < f.size_bytes:
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            resp = self._open(client, f.url, headers)
            try:
                if offset and resp.status_code == 200:  # server ignored Range: restart this file
                    prog.add(-offset)
                    offset = 0
                    hasher = hashlib.sha256()
                elif resp.status_code not in (200, 206):
                    raise DownloadError(f"HTTP {resp.status_code} for {f.path}")
                with open(part, "ab" if offset else "wb") as fh:
                    for chunk in resp.iter_bytes(1 << 20):
                        self._check_cancel(m.id)
                        fh.write(chunk)
                        hasher.update(chunk)
                        prog.add(len(chunk))
            finally:
                resp.close()

        size = part.stat().st_size
        if size != f.size_bytes:
            raise DownloadError(f"Incomplete download for {f.path} ({size}/{f.size_bytes} bytes)")
        if hasher.hexdigest() != f.sha256:
            part.unlink()
            raise DownloadError(f"Checksum mismatch for {f.path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, dest)

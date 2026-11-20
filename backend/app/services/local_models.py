"""Find copies of catalog models that are already on this PC, so they are reused instead of downloaded.

Looks in other Smart Space data folders, the Hugging Face cache, LM Studio, Ollama's blob store, the Downloads
folder and any folder in SS_MODEL_DIRS. A file counts as a candidate when its name (or, for Ollama, its
sha256-named blob) and its size match the catalog; the download worker verifies the SHA-256 before using it,
so a wrong candidate is never installed. Nothing is uploaded or changed: found files are hard-linked or copied
into Smart Space's own models folder.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.context import AppContext
    from app.services.catalog import CatalogModel

log = get_logger("models")

MAX_DEPTH = 7  # HF cache: hub/models--org--name/snapshots/<rev>/file is depth 4
MAX_ENTRIES = 250_000  # stop walking a huge tree after this many directory entries
SCAN_TTL_S = 600


def default_roots(own_models_dir: Path) -> list[Path]:
    env = os.environ
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    roots: list[Path] = []
    for d in (env.get("SS_MODEL_DIRS") or "").split(os.pathsep):
        if d.strip():
            roots.append(Path(d.strip()))
    if env.get("SS_LOCAL_MODEL_SEARCH", "1") == "0":  # only SS_MODEL_DIRS (tests, or users who opt out)
        return [r for r in roots if r.is_dir() and _norm(r) != _norm(own_models_dir)]
    if env.get("APPDATA"):
        roots.append(Path(env["APPDATA"]) / "SmartSpace" / "models")
    if env.get("LOCALAPPDATA"):
        roots.append(Path(env["LOCALAPPDATA"]) / "SmartSpace" / "models")
    roots.append(Path("C:/SmartSpaceDemo/models") if os.name == "nt" else home / "SmartSpaceDemo" / "models")
    hf = env.get("HF_HUB_CACHE") or (str(Path(env["HF_HOME"]) / "hub") if env.get("HF_HOME") else None)
    roots.append(Path(hf) if hf else home / ".cache" / "huggingface" / "hub")
    roots += [home / ".lmstudio" / "models", home / ".cache" / "lm-studio" / "models",
              home / ".ollama" / "models" / "blobs", home / "Downloads"]
    own = _norm(own_models_dir)
    out, seen = [], set()
    for r in roots:
        key = _norm(r)
        if key in seen or key == own or own.startswith(key + os.sep) or key.startswith(own + os.sep):
            continue  # never our own folder (it is synced separately), never a parent of it
        seen.add(key)
        if r.is_dir():
            out.append(r)
    return out


def _norm(p: Path) -> str:
    try:
        return os.path.normcase(str(Path(p).resolve()))
    except OSError:
        return os.path.normcase(str(p))


def _walk(root: Path, wanted_names: set[str], budget: list[int]) -> Iterable[tuple[str, Path, int]]:
    """Yield (lowercase name, path, size) for files whose name is wanted, depth-limited and bounded."""
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    budget[0] -= 1
                    if budget[0] <= 0:
                        return
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if depth < MAX_DEPTH and not e.name.startswith("."):
                                stack.append((Path(e.path), depth + 1))
                        elif e.name.lower() in wanted_names:
                            yield e.name.lower(), Path(e.path), e.stat().st_size  # follows HF symlinks
                    except OSError:
                        continue
        except OSError:
            continue


class LocalModelFinder:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Path]] = {}
        self._scanned_at = 0.0

    def roots(self) -> list[Path]:
        return default_roots(self.ctx.cfg.models_dir)

    def scan(self, force: bool = False) -> dict[str, dict[str, Path]]:
        """{model_id: {catalog file path: local file}} for models whose files are ALL found (size match)."""
        with self._lock:
            if not force and self._scanned_at and time.monotonic() - self._scanned_at < SCAN_TTL_S:
                return self._cache
            t0 = time.monotonic()
            models = [m for m in self.ctx.catalog.models.values() if m.files and not m.bundled]
            # (name, size) -> list of catalog keys; Ollama blobs are named "sha256-<hex>"
            wanted: dict[tuple[str, int], list[tuple[str, str]]] = {}
            for m in models:
                for f in m.files:
                    for name in (Path(f.path).name.lower(), f"sha256-{f.sha256}"):
                        wanted.setdefault((name, f.size_bytes), []).append((m.id, f.path))
            names = {n for n, _ in wanted}
            found: dict[str, dict[str, Path]] = {}
            budget = [MAX_ENTRIES]
            for root in self.roots():
                for name, path, size in _walk(root, names, budget):
                    for mid, fpath in wanted.get((name, size), []):
                        found.setdefault(mid, {}).setdefault(fpath, path)
            by_id = {m.id: m for m in models}
            complete = {mid: files for mid, files in found.items() if len(files) == len(by_id[mid].files)}
            self._cache, self._scanned_at = complete, time.monotonic()
            log.info("local_models_scanned", found=sorted(complete), duration_ms=int((time.monotonic() - t0) * 1000))
            return complete

    def find(self, m: "CatalogModel") -> dict[str, Path] | None:
        files = self.scan().get(m.id)
        if not files:
            return None
        if all(p.exists() and p.stat().st_size == f.size_bytes for f in m.files for p in [files[f.path]]):
            return files
        return self.scan(force=True).get(m.id)

    def location(self, m: "CatalogModel") -> str | None:
        """Folder of a found copy, for display (the primary file's folder)."""
        files = self.scan().get(m.id)
        if not files:
            return None
        primary = files.get(m.files[0].path)
        return str(primary.parent) if primary else None

    def forget(self, model_id: str) -> None:
        with self._lock:
            self._cache.pop(model_id, None)

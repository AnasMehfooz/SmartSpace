"""Path safety (SDD 13) and exclusion glob matching (SDD 5.3)."""

from __future__ import annotations

import os
import re
import stat
import sys
from functools import lru_cache
from pathlib import Path

from app.errors import AppError

IS_WINDOWS = sys.platform == "win32"


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def is_within(child: Path | str, parent: Path | str) -> bool:
    """True if `child` equals or is inside `parent` (case-insensitive on Windows)."""
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    return c.startswith(p.rstrip(os.sep) + os.sep)


def _forbidden_roots() -> list[Path]:
    roots: list[Path] = []
    if IS_WINDOWS:
        for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
            val = os.environ.get(var)
            if val:
                roots.append(Path(val))
        roots.append(Path("C:/Windows"))
    else:
        for p in ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr",
                  "/var", "/System", "/Library", "/Applications", "/private"):
            roots.append(Path(p))
    return roots


def validate_workspace_root(path_str: str, data_dir: Path) -> tuple[Path, list[str]]:
    """Validate a workspace root: exists, readable, not a system location. Returns (path, warnings)."""

    def bad(msg: str) -> AppError:
        return AppError("WORKSPACE_PATH_INVALID", msg, details={"root_path": path_str})

    if not path_str or not path_str.strip():
        raise bad("Folder path is empty")
    raw = path_str.strip()
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise bad("Network (UNC) paths are not supported")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise bad("Folder path must be absolute")
    try:
        rp = p.resolve(strict=True)
    except (OSError, RuntimeError):
        raise bad("Folder does not exist") from None
    if not rp.is_dir():
        raise bad("Path is not a folder")
    try:
        with os.scandir(rp) as it:
            next(it, None)
    except PermissionError:
        raise bad("No permission to read this folder") from None
    except OSError as e:
        raise bad(f"Folder cannot be read ({e.__class__.__name__})") from None

    if _norm(rp) == _norm(Path(rp.anchor)):
        raise bad("A whole drive cannot be the workspace; pick a folder")
    for f in _forbidden_roots():
        if is_within(rp, f):
            raise bad("System folders cannot be the workspace")
    if is_within(rp, data_dir):
        raise bad("The app's own data folder cannot be the workspace")

    warnings: list[str] = []
    if _norm(rp) == _norm(Path.home()):
        warnings.append("Indexing your whole user folder can take a long time.")
    return rp, warnings


def safe_join(root: Path, rel: str | None) -> Path:
    """Resolve a client-supplied relative path and make sure it stays inside the root."""

    def bad() -> AppError:
        return AppError("VALIDATION_ERROR", "Invalid relative path", details={"rel_path": rel})

    rel = (rel or "").replace("\\", "/").strip()
    if rel.startswith("/") or ":" in rel or "\x00" in rel:
        raise bad()
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise bad()
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts) if parts else root_resolved
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        raise bad() from None
    if not is_within(resolved, root_resolved):  # symlink / junction escaping the root
        raise bad()
    return resolved


def clean_rel(rel: str | None) -> str:
    """Normalise a relative folder filter (no validation against the filesystem)."""
    rel = (rel or "").replace("\\", "/").strip().strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts) or ":" in rel:
        raise AppError("VALIDATION_ERROR", "Invalid folder path", details={"folder": rel})
    return "/".join(parts)


def rel_of(root: Path, path: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def is_hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    if IS_WINDOWS:
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
        return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
    return False


def is_link_or_junction(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        if IS_WINDOWS:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
            return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True
    return False


# ---- exclusion globs ------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern:
    """Glob -> regex. `**` crosses folders, `*`/`?` do not. Patterns without a leading `/`
    match at any depth (like .gitignore). `dir/**` also matches the folder itself."""
    pat = pattern.replace("\\", "/").strip()
    anchored = pat.startswith("/")
    pat = pat.lstrip("/")
    dir_suffix = pat.endswith("/**")
    if dir_suffix:
        pat = pat[: -len("/**")]
    out = []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if ch == "*":
            if i + 1 < len(pat) and pat[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < len(pat) and pat[i] == "/":
                    i += 1
                    out[-1] = "(?:.*/)?"
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    body = "".join(out)
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "(?:/.*)?" if dir_suffix else ""
    flags = re.IGNORECASE if IS_WINDOWS else 0
    return re.compile(f"^{prefix}{body}{suffix}$", flags)


class ExclusionMatcher:
    def __init__(self, patterns: list[str]):
        self.patterns = [p for p in patterns if p and p.strip()]
        self._regexes = [_glob_regex(p) for p in self.patterns]

    def matches(self, rel_path: str) -> bool:
        rel_path = rel_path.replace("\\", "/").strip("/")
        return any(r.match(rel_path) for r in self._regexes)

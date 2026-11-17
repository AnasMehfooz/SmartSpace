"""The single workspace root + exclusion patterns (SDD 10.3)."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db import utcnow
from app.errors import AppError
from app.logging_setup import get_logger
from app.services.paths import ExclusionMatcher, validate_workspace_root

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("workspace")


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class WorkspaceService:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self._lock = threading.Lock()
        self._root: Path | None = None
        self._matcher: ExclusionMatcher | None = None
        row = ctx.db.query_one("SELECT root_path FROM workspace WHERE id = 1")
        self._root = Path(row["root_path"]) if row else None

    # ---- root -------------------------------------------------------------------------------
    def root(self) -> Path | None:
        return self._root

    def require_root(self) -> Path:
        if self._root is None:
            raise AppError("WORKSPACE_NOT_SET", "Pick a workspace folder first")
        return self._root

    def was_scanned(self) -> bool:
        return bool(self.ctx.db.scalar("SELECT last_scan_at FROM workspace WHERE id = 1"))

    def get(self) -> dict[str, Any] | None:
        row = self.ctx.db.query_one("SELECT * FROM workspace WHERE id = 1")
        if row is None:
            return None
        counts = {r["status"]: r["n"] for r in self.ctx.db.query("SELECT status, COUNT(*) AS n FROM files GROUP BY status")}
        return {
            "root_path": row["root_path"],
            "status": self.ctx.indexer.state(),
            "created_at": row["created_at"],
            "last_scan_at": row["last_scan_at"],
            "exists": Path(row["root_path"]).is_dir(),
            "counts": {
                "files": sum(counts.values()),
                "indexed": counts.get("indexed", 0),
                "pending": counts.get("pending", 0) + counts.get("processing", 0),
                "skipped": counts.get("skipped", 0),
                "errors": counts.get("error", 0),
            },
        }

    def set(self, root_path: str, replace: bool = False) -> dict[str, Any]:
        rp, warnings = validate_workspace_root(root_path, self.ctx.cfg.data_dir)
        current = self._root
        if current is not None:
            if os.path.normcase(str(current)) == os.path.normcase(str(rp)):
                return {**(self.get() or {}), "warnings": warnings}
            if not replace:
                raise AppError("WORKSPACE_EXISTS", "A different workspace is already set",
                               details={"root_path": str(current)})
            self.ctx.purge.unlink_workspace(log_scope="index", target=str(current))
        with self.ctx.db.write() as conn:
            conn.execute(
                "INSERT INTO workspace (id, root_path, status, created_at) VALUES (1, ?, 'idle', ?)",
                (str(rp), utcnow()),
            )
        with self._lock:
            self._root = rp
            self._matcher = None
        log.info("workspace_set")
        return {**(self.get() or {}), "warnings": warnings}

    def clear(self) -> None:
        """Forget the root (rows are deleted by the purge service)."""
        with self._lock:
            self._root = None
            self._matcher = None

    def mark_scanned(self) -> None:
        self.ctx.db.execute("UPDATE workspace SET last_scan_at = ? WHERE id = 1", (utcnow(),))

    def set_status(self, status: str) -> None:
        if self._root is not None:
            self.ctx.db.execute("UPDATE workspace SET status = ? WHERE id = 1 AND status != ?", (status, status))

    # ---- stats ------------------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        self.require_root()
        db = self.ctx.db
        by_status = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM files GROUP BY status")}
        by_ext = {r["ext"] or "(none)": r["n"] for r in db.query(
            "SELECT ext, COUNT(*) AS n FROM files GROUP BY ext ORDER BY n DESC")}
        by_kind = {r["kind"]: r["n"] for r in db.query("SELECT kind, COUNT(*) AS n FROM files GROUP BY kind")}
        return {
            "files_total": sum(by_status.values()),
            "by_status": by_status,
            "by_ext": by_ext,
            "by_kind": by_kind,
            "chunks": db.scalar("SELECT COUNT(*) FROM chunks") or 0,
            "summaries": db.scalar("SELECT COUNT(*) FROM summaries") or 0,
            "size_bytes": db.scalar("SELECT COALESCE(SUM(size_bytes), 0) FROM files") or 0,
        }

    # ---- exclusions -------------------------------------------------------------------------
    def matcher(self) -> ExclusionMatcher:
        with self._lock:
            if self._matcher is None:
                pats = [r["pattern"] for r in self.ctx.db.query("SELECT pattern FROM exclusions")]
                self._matcher = ExclusionMatcher(pats)
            return self._matcher

    def exclusions(self) -> list[dict[str, Any]]:
        return [
            {"id": r["id"], "pattern": r["pattern"], "builtin": bool(r["builtin"])}
            for r in self.ctx.db.query("SELECT id, pattern, builtin FROM exclusions ORDER BY builtin DESC, id")
        ]

    def add_exclusion(self, pattern: str) -> dict[str, Any]:
        pattern = (pattern or "").strip().replace("\\", "/")
        if not pattern or len(pattern) > 200 or ".." in pattern.split("/") or ":" in pattern:
            raise AppError("VALIDATION_ERROR", "Invalid exclusion pattern", details={"pattern": pattern})
        with self.ctx.db.write() as conn:
            conn.execute("INSERT INTO exclusions (pattern, builtin) VALUES (?, 0) ON CONFLICT(pattern) DO NOTHING",
                         (pattern,))
            row = conn.execute("SELECT id, pattern, builtin FROM exclusions WHERE pattern = ?", (pattern,)).fetchone()
        with self._lock:
            self._matcher = None
        # remove matching files from the index
        m = ExclusionMatcher([pattern])
        removed = [r["id"] for r in self.ctx.db.query("SELECT id, rel_path FROM files") if m.matches(r["rel_path"])]
        self.ctx.indexer.remove_files(removed)
        return {"id": row["id"], "pattern": row["pattern"], "builtin": bool(row["builtin"]), "files_removed": len(removed)}

    def delete_exclusion(self, exclusion_id: int) -> None:
        row = self.ctx.db.query_one("SELECT builtin FROM exclusions WHERE id = ?", (exclusion_id,))
        if row is None:
            raise AppError("NOT_FOUND", f"Exclusion {exclusion_id} does not exist")
        if row["builtin"]:
            raise AppError("VALIDATION_ERROR", "Built-in exclusions cannot be removed")
        self.ctx.db.execute("DELETE FROM exclusions WHERE id = ?", (exclusion_id,))
        with self._lock:
            self._matcher = None
        if self._root is not None and self.was_scanned():
            self.ctx.indexer.start_full_scan()

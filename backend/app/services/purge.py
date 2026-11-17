"""Privacy: overview, purge, audit, export (SDD 10.8).

No scope ever touches the user's files. Every deletion goes through `safe_unlink`/`safe_rmtree`,
which refuse any path outside the app data directory (and, explicitly, anything inside the
workspace root that is not app data). tests/test_purge_safety.py enforces this.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.db import utcnow
from app.errors import AppError
from app.logging_setup import get_logger
from app.services.paths import is_within

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("purge")

SCOPES = ("index", "file", "chats", "models", "everything")


def _assert_deletable(ctx: "AppContext", path: Path | str) -> Path:
    p = Path(path).resolve()
    data = ctx.cfg.data_dir.resolve()
    if not is_within(p, data) or os.path.normcase(str(p)) == os.path.normcase(str(data)):
        raise PermissionError("Refusing to delete a path outside the app data directory")
    root = ctx.workspace.root()
    if root is not None and is_within(p, root) and not is_within(p, data):
        raise PermissionError("Refusing to delete inside the workspace root")
    return p


def safe_unlink(ctx: "AppContext", path: Path | str) -> None:
    p = _assert_deletable(ctx, path)
    if p.exists():
        os.unlink(p)


def safe_rmtree(ctx: "AppContext", path: Path | str) -> None:
    p = _assert_deletable(ctx, path)
    if p.exists():
        shutil.rmtree(p)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class PurgeService:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.db = ctx.db

    # ---- overview / audit / export ----------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        db = self.db
        return {
            "workspace_root": str(self.ctx.workspace.root()) if self.ctx.workspace.root() else None,
            "files_total": db.scalar("SELECT COUNT(*) FROM files") or 0,
            "files_indexed": db.scalar("SELECT COUNT(*) FROM files WHERE status = 'indexed'") or 0,
            "chunks": db.scalar("SELECT COUNT(*) FROM chunks") or 0,
            "vectors": self.ctx.vectors.count(),
            "summaries": db.scalar("SELECT COUNT(*) FROM summaries") or 0,
            "tags": db.scalar("SELECT COUNT(*) FROM tags") or 0,
            "chat_sessions": db.scalar("SELECT COUNT(*) FROM chat_sessions") or 0,
            "chat_messages": db.scalar("SELECT COUNT(*) FROM chat_messages") or 0,
            "db_size_bytes": db.size_bytes(),
            "vectors_size_bytes": self.ctx.vectors.size_bytes(),
            "models_size_bytes": self.ctx.models.models_size_bytes(),
            "logs_size_bytes": _dir_size(self.ctx.cfg.logs_dir),
            "data_dir": str(self.ctx.cfg.data_dir),
            "last_purge_at": db.scalar("SELECT MAX(performed_at) FROM purge_log"),
        }

    def audit(self, limit: int, offset: int) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM purge_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))
        total = self.db.scalar("SELECT COUNT(*) FROM purge_log") or 0
        return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    def export(self) -> dict[str, Any]:
        """Summaries, tags and doc types (never file content), for transparency/portability."""
        tags: dict[int, list[dict[str, str]]] = {}
        for r in self.db.query(
                "SELECT ft.file_id, t.name, ft.source FROM file_tags ft JOIN tags t ON t.id = ft.tag_id ORDER BY t.name"):
            tags.setdefault(r["file_id"], []).append({"name": r["name"], "source": r["source"]})
        files = []
        for r in self.db.query(
                "SELECT f.id, f.rel_path, f.name, f.language, s.summary, s.doc_type, s.model_id, s.generated_at "
                "FROM files f LEFT JOIN summaries s ON s.file_id = f.id ORDER BY f.rel_path"):
            if r["summary"] is None and r["id"] not in tags:
                continue
            files.append({
                "file_id": r["id"], "rel_path": r["rel_path"], "name": r["name"], "language": r["language"],
                "doc_type": r["doc_type"], "summary": r["summary"], "summary_model": r["model_id"],
                "generated_at": r["generated_at"], "tags": tags.get(r["id"], []),
            })
        from app.config import VERSION

        return {"exported_at": utcnow(), "app_version": VERSION,
                "workspace_root": str(self.ctx.workspace.root()) if self.ctx.workspace.root() else None,
                "files": files}

    # ---- purge ------------------------------------------------------------------------------
    def purge(self, scope: str, file_ids: list[int] | None, confirm: bool) -> dict[str, Any]:
        if not confirm:
            raise AppError("CONFIRM_REQUIRED", "Set confirm: true to purge")
        if scope not in SCOPES:
            raise AppError("VALIDATION_ERROR", f"Unknown scope: {scope}")
        if scope == "file" and not file_ids:
            raise AppError("VALIDATION_ERROR", "file_ids is required for scope 'file'")
        if scope == "index":
            items, freed = self._purge_index()
        elif scope == "file":
            items, freed = self._purge_files(list(file_ids or []))
        elif scope == "chats":
            items, freed = self._purge_chats()
        elif scope == "models":
            items, freed = self._purge_models()
        else:
            items, freed = self._purge_everything()
        target = ",".join(str(i) for i in (file_ids or [])[:50]) if scope == "file" else None
        self._log(scope, target, items, freed)
        return {"scope": scope, "items_deleted": items, "bytes_freed": freed}

    def _log(self, scope: str, target: str | None, items: int, freed: int) -> None:
        self.db.execute(
            "INSERT INTO purge_log (scope, target, items_deleted, bytes_freed, performed_at) VALUES (?, ?, ?, ?, ?)",
            (scope, target, items, freed, utcnow()))
        log.info("purge", scope=scope, items=items, bytes_freed=freed)

    def _sizes(self) -> int:
        self.db.checkpoint()
        return self.db.size_bytes() + self.ctx.vectors.size_bytes()

    def _purge_index(self) -> tuple[int, int]:
        with self.ctx.indexer.maintenance():
            before = self._sizes()
            vectors = self.ctx.vectors.count()
            with self.db.write() as conn:
                n_chunks = conn.execute("DELETE FROM chunks").rowcount
                n_summ = conn.execute("DELETE FROM summaries").rowcount
                n_tags = conn.execute("DELETE FROM file_tags").rowcount
                conn.execute("DELETE FROM tags")
                conn.execute("DELETE FROM jobs")
                conn.execute(
                    "UPDATE files SET status='pending', content_hash=NULL, language=NULL, page_count=NULL, "
                    "skip_reason=NULL, error_message=NULL, summary_state='none', indexed_at=NULL, updated_at=?",
                    (utcnow(),))
                conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')")
            self.ctx.vectors.drop_all()
            for leftover in self.ctx.cfg.vectors_dir.glob("*"):
                if leftover.is_dir():
                    safe_rmtree(self.ctx, leftover)
            self.db.vacuum()
            freed = max(0, before - self._sizes())
        return n_chunks + n_summ + n_tags + vectors, freed

    def _purge_files(self, file_ids: list[int]) -> tuple[int, int]:
        ids = [int(i) for i in file_ids]
        with self.ctx.indexer.maintenance():
            before = self._sizes()
            vectors_before = self.ctx.vectors.count()
            qs = ",".join("?" * len(ids))
            with self.db.write() as conn:
                n_chunks = conn.execute(f"DELETE FROM chunks WHERE file_id IN ({qs})", ids).rowcount
                n_summ = conn.execute(f"DELETE FROM summaries WHERE file_id IN ({qs})", ids).rowcount
                n_tags = conn.execute(f"DELETE FROM file_tags WHERE file_id IN ({qs})", ids).rowcount
                conn.execute(f"DELETE FROM jobs WHERE file_id IN ({qs})", ids)
                conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)")
                conn.execute(
                    f"UPDATE files SET status='pending', content_hash=NULL, language=NULL, page_count=NULL, "
                    f"skip_reason=NULL, error_message=NULL, summary_state='none', indexed_at=NULL, updated_at=? "
                    f"WHERE id IN ({qs})", [utcnow(), *ids])
            self.ctx.vectors.delete_files(ids)
            vectors = max(0, vectors_before - self.ctx.vectors.count())
            freed = max(0, before - self._sizes())
        return n_chunks + n_summ + n_tags + vectors, freed

    def _purge_chats(self) -> tuple[int, int]:
        before = self.db.size_bytes()
        self.ctx.chat.stop_all()
        with self.db.write() as conn:
            n_msgs = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
            n_sessions = conn.execute("DELETE FROM chat_sessions").rowcount
        self.db.vacuum()
        return n_sessions + n_msgs, max(0, before - self.db.size_bytes())

    def _purge_models(self) -> tuple[int, int]:
        self.ctx.llm.stop()
        for m in self.ctx.catalog.models.values():
            if self.ctx.downloads.is_downloading(m.id):
                self.ctx.downloads.cancel(m.id)
        items = 0
        freed = 0
        for m in self.ctx.catalog.models.values():
            if m.bundled:
                continue
            if self.ctx.models.model_dir(m).exists() or list(self.ctx.cfg.tmp_dir.glob(f"{m.id}__*.part")):
                freed += self.ctx.models.remove_files(m)
                items += 1
            self.ctx.models.set_status(m.id, "not_installed")
        self.ctx.embedders.reload()
        return items, freed

    def unlink_workspace(self, log_scope: str | None = None, target: str | None = None) -> tuple[int, int]:
        """Drop the whole index and the workspace link (used by DELETE /workspace and replace)."""
        self.ctx.watcher.stop()
        items, freed = self._purge_index()
        with self.ctx.indexer.maintenance():
            with self.db.write() as conn:
                items += conn.execute("DELETE FROM files").rowcount
                conn.execute("DELETE FROM jobs")
                conn.execute("DELETE FROM exclusions WHERE builtin = 0")
                conn.execute("DELETE FROM workspace")
            self.ctx.workspace.clear()
        if log_scope:
            self._log(log_scope, target, items, freed)
        return items, freed

    def _purge_everything(self) -> tuple[int, int]:
        items, freed = self._purge_chats()
        i2, f2 = self._purge_models()
        i3, f3 = self.unlink_workspace()
        tmp_freed = 0
        for p in self.ctx.cfg.tmp_dir.glob("*"):
            tmp_freed += p.stat().st_size if p.is_file() else _dir_size(p)
            if p.is_dir():
                safe_rmtree(self.ctx, p)
            else:
                safe_unlink(self.ctx, p)
        return items + i2 + i3, freed + f2 + f3 + tmp_freed

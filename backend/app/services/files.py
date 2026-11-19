"""File browsing for the UI: lazy tree, flat list, detail, chunks, image preview, user tags (SDD 10.4)."""

from __future__ import annotations

import io
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from app.db import utcnow
from app.db.database import ns_to_iso
from app.errors import AppError
from app.services.paths import clean_rel, safe_join

if TYPE_CHECKING:
    from app.context import AppContext

SORTS = {"name": "f.name COLLATE NOCASE", "mtime": "f.mtime_ns", "size": "f.size_bytes", "indexed_at": "f.indexed_at",
         "path": "f.rel_path COLLATE NOCASE"}
PREVIEW_MAX = 1024
SUMMARY_SHORT_CHARS = 160


def short_summary(text: str | None, limit: int = SUMMARY_SHORT_CHARS) -> str | None:
    """First ~`limit` characters of a summary, cut at a word boundary, with an ellipsis if cut."""
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class FilesService:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.db = ctx.db

    def require(self, file_id: int):
        row = self.db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
        if row is None:
            raise AppError("FILE_NOT_FOUND", f"File {file_id} does not exist")
        return row

    # ---- tree -------------------------------------------------------------------------------
    def tree(self, parent: str | None) -> list[dict[str, Any]]:
        self.ctx.workspace.require_root()
        prefix = clean_rel(parent)
        if prefix:
            rows = self.db.query("SELECT id, rel_path, name, status, skip_reason FROM files WHERE rel_path LIKE ? ESCAPE '\\'",
                                 (_like_escape(prefix) + "/%",))
        else:
            rows = self.db.query("SELECT id, rel_path, name, status, skip_reason FROM files")
        cut = len(prefix) + 1 if prefix else 0
        dirs: dict[str, dict[str, Any]] = {}
        children: dict[str, set[str]] = defaultdict(set)
        files: list[dict[str, Any]] = []
        for r in rows:
            rest = r["rel_path"][cut:]
            if "/" in rest:
                head, tail = rest.split("/", 1)
                d = dirs.setdefault(head, {"name": head, "rel_path": f"{prefix}/{head}" if prefix else head,
                                           "type": "dir", "file_count": 0, "indexed_count": 0})
                d["file_count"] += 1
                if r["status"] == "indexed":
                    d["indexed_count"] += 1
                children[head].add(tail.split("/", 1)[0])
            else:
                files.append({"name": r["name"], "rel_path": r["rel_path"], "type": "file", "file_id": r["id"],
                              "status": r["status"], "skip_reason": r["skip_reason"]})
        for name, d in dirs.items():
            d["child_count"] = len(children[name])
        return sorted(dirs.values(), key=lambda d: d["name"].casefold()) + sorted(files, key=lambda f: f["name"].casefold())

    # ---- list -------------------------------------------------------------------------------
    def list(self, *, folder: str | None, ext: str | None, kind: str | None, status: str | None, tag: str | None,
             doc_type: str | None, q_name: str | None, sort: str, order: str, limit: int, offset: int) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if folder:
            f = clean_rel(folder)
            if f:
                where.append("f.rel_path LIKE ? ESCAPE '\\'")
                params.append(_like_escape(f) + "/%")
        if ext:
            exts = [e.strip().lower().lstrip(".") for e in ext.split(",") if e.strip()]
            where.append(f"f.ext IN ({','.join('?' * len(exts))})")
            params += exts
        if kind:
            where.append("f.kind = ?")
            params.append(kind)
        if status:
            sts = [s.strip() for s in status.split(",") if s.strip()]
            where.append(f"f.status IN ({','.join('?' * len(sts))})")
            params += sts
        if tag:
            where.append("EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id = ft.tag_id WHERE ft.file_id = f.id AND t.name = ?)")
            params.append(tag)
        if doc_type:
            where.append("s.doc_type = ?")
            params.append(doc_type)
        if q_name:
            where.append("f.name LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(q_name)}%")
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        order_sql = SORTS.get(sort, SORTS["name"]) + (" DESC" if order == "desc" else " ASC")
        base = f"FROM files f LEFT JOIN summaries s ON s.file_id = f.id {sql_where}"
        total = self.db.scalar(f"SELECT COUNT(*) {base}", params) or 0
        rows = self.db.query(
            f"SELECT f.*, s.doc_type, s.summary {base} ORDER BY {order_sql}, f.id LIMIT ? OFFSET ?",
            [*params, limit, offset])
        tags: dict[int, list[str]] = {r["id"]: [] for r in rows}
        if tags:
            for t in self.db.query(
                    "SELECT ft.file_id, t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id "
                    f"WHERE ft.file_id IN ({','.join('?' * len(tags))}) ORDER BY ft.source DESC, t.name", list(tags)):
                tags[t["file_id"]].append(t["name"])
        items = [{
            "id": r["id"], "rel_path": r["rel_path"], "name": r["name"], "ext": r["ext"], "kind": r["kind"],
            "size_bytes": r["size_bytes"], "mtime": ns_to_iso(r["mtime_ns"]), "status": r["status"],
            "skip_reason": r["skip_reason"], "error_message": r["error_message"], "summary_state": r["summary_state"],
            "indexed_at": r["indexed_at"], "doc_type": r["doc_type"],
            "tags": tags[r["id"]], "summary_short": short_summary(r["summary"]),
        } for r in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # ---- detail -----------------------------------------------------------------------------
    def detail(self, file_id: int) -> dict[str, Any]:
        r = self.require(file_id)
        root = self.ctx.workspace.require_root()
        s = self.db.query_one("SELECT * FROM summaries WHERE file_id = ?", (file_id,))
        tags = [{"name": t["name"], "source": t["source"]} for t in self.db.query(
            "SELECT t.name, ft.source FROM file_tags ft JOIN tags t ON t.id = ft.tag_id WHERE ft.file_id = ? "
            "ORDER BY ft.source DESC, t.name", (file_id,))]
        return {
            "id": r["id"], "rel_path": r["rel_path"], "abs_path": str(root / r["rel_path"]), "name": r["name"],
            "ext": r["ext"], "kind": r["kind"], "size_bytes": r["size_bytes"], "mtime": ns_to_iso(r["mtime_ns"]),
            "language": r["language"], "page_count": r["page_count"], "status": r["status"],
            "skip_reason": r["skip_reason"], "summary_state": r["summary_state"],
            "summary": {"text": s["summary"], "doc_type": s["doc_type"], "model_id": s["model_id"],
                        "generated_at": s["generated_at"], "prompt_version": s["prompt_version"]} if s else None,
            "tags": tags,
            "chunk_count": self.db.scalar("SELECT COUNT(*) FROM chunks WHERE file_id = ? AND source != 'summary'",
                                          (file_id,)) or 0,
            "error_message": r["error_message"], "indexed_at": r["indexed_at"],
        }

    def chunks(self, file_id: int, limit: int, offset: int) -> dict[str, Any]:
        self.require(file_id)
        rows = self.db.query(
            "SELECT id, chunk_index, source, page_start, page_end, text, char_count, embedding_model FROM chunks "
            "WHERE file_id = ? ORDER BY CASE source WHEN 'summary' THEN 1 ELSE 0 END, chunk_index LIMIT ? OFFSET ?",
            (file_id, limit, offset))
        total = self.db.scalar("SELECT COUNT(*) FROM chunks WHERE file_id = ?", (file_id,)) or 0
        return {"items": [dict(x) for x in rows], "total": total, "limit": limit, "offset": offset}

    def preview(self, file_id: int, page: int = 1) -> bytes:
        """Downscaled JPEG (max 1024 px) of an image, or of a PDF page. Read-only."""
        from PIL import Image, ImageOps

        r = self.require(file_id)
        root = self.ctx.workspace.require_root()
        path = safe_join(root, r["rel_path"])
        if not path.is_file():
            raise AppError("FILE_NOT_FOUND", "File is no longer on disk")
        if r["kind"] == "image":
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
                img = im.convert("RGB")
        elif r["ext"] == "pdf":
            import pymupdf

            with pymupdf.open(str(path)) as doc:
                if doc.needs_pass or doc.page_count == 0:
                    raise AppError("VALIDATION_ERROR", "This PDF cannot be previewed")
                pg = doc.load_page(min(max(page, 1), doc.page_count) - 1)
                zoom = PREVIEW_MAX / max(pg.rect.width, pg.rect.height)
                pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        else:
            raise AppError("VALIDATION_ERROR", "Preview is available for images and PDFs only")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return buf.getvalue()

    def set_user_tags(self, file_id: int, tags: list[str]) -> dict[str, Any]:
        """Replace the user's tags (AI tags are untouched)."""
        self.require(file_id)
        clean: list[str] = []
        for t in tags:
            t = " ".join(str(t).split()).strip().lower()
            if t and len(t) <= 40 and t not in clean:
                clean.append(t)
        with self.db.write() as conn:
            conn.execute("DELETE FROM file_tags WHERE file_id = ? AND source = 'user'", (file_id,))
            for t in clean:
                conn.execute("INSERT INTO tags (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (t,))
                tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (t,)).fetchone()["id"]
                conn.execute(
                    "INSERT INTO file_tags (file_id, tag_id, source) VALUES (?, ?, 'user') "
                    "ON CONFLICT(file_id, tag_id) DO UPDATE SET source = 'user'", (file_id, tag_id))
            conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)")
            conn.execute("UPDATE files SET updated_at = ? WHERE id = ?", (utcnow(), file_id))
        return self.detail(file_id)

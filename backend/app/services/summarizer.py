"""Summary + tags + doc_type per file (SDD 6.4), via llama-server JSON-schema mode."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Callable

from app.db import utcnow
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("summarizer")

PROMPT_VERSION = "s2"  # s2 = s1 + doc_type definitions; older summaries are regenerated
DOC_TYPES = ["invoice", "contract", "cv", "lecture", "note", "report", "letter", "form", "photo", "other"]
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "doc_type": {"type": "string", "enum": DOC_TYPES},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 8},
    },
    "required": ["summary", "doc_type", "tags"],
    "additionalProperties": False,
}
MAX_INPUT_CHARS = 24000  # ~6,000 tokens (chars/4)
BLOCK_CHARS = 12000  # ~3,000 tokens per map step
MAX_BLOCKS = 8


def clean_tags(tags: list[Any]) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = re.sub(r"\s+", " ", str(t)).strip().strip("#").lower()
        if 1 < len(t) <= 40 and t not in out:
            out.append(t)
    return out[:8]


class Summarizer:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx

    def _prompt(self, name: str) -> str:
        return (self.ctx.cfg.prompts_dir / name).read_text(encoding="utf-8")

    def _input_budget_chars(self) -> int:
        llm = self.ctx.models.active("llm")
        ctx_cap = self.ctx.catalog.tier_ctx_cap.get(self.ctx.hardware.tier, 4096)
        if llm is not None:
            ctx_cap = min(ctx_cap, int(llm.get("ctx_cap") or ctx_cap))
        return max(2000, min(MAX_INPUT_CHARS, (ctx_cap - 700) * 4))

    def summarize_file(self, file_id: int, should_abort: Callable[[], bool]) -> bool:
        db = self.ctx.db
        f = db.query_one("SELECT id, name, status FROM files WHERE id = ?", (file_id,))
        if f is None or f["status"] != "indexed":
            return False
        rows = db.query(
            "SELECT text FROM chunks WHERE file_id = ? AND source IN ('text','ocr') ORDER BY chunk_index", (file_id,))
        if not rows:
            db.execute("UPDATE files SET summary_state='none' WHERE id=?", (file_id,))
            return False
        text = "\n\n".join(r["text"] for r in rows)
        budget = self._input_budget_chars()
        client = self.ctx.llm_client
        if len(text) > budget:
            # map-reduce: summarize ~3,000-token blocks into 3 sentences, then summarize the summaries
            block = min(BLOCK_CHARS, budget)
            blocks = [text[i : i + block] for i in range(0, len(text), block)][:MAX_BLOCKS]
            tpl = self._prompt("summarize_block.txt")
            partials = []
            for i, b in enumerate(blocks, 1):
                out, _ = client.complete(
                    [{"role": "user", "content": tpl.format(name=f["name"], part=i, parts=len(blocks), text=b)}],
                    max_tokens=160, temperature=0.1, should_abort=should_abort)
                partials.append(out.strip())
            text = "\n".join(partials)[:budget]
        prompt = self._prompt("summarize.txt").format(name=f["name"], text=text[:budget])
        data, _ = client.complete_json([{"role": "user", "content": prompt}], SUMMARY_SCHEMA, max_tokens=700,
                                       temperature=0.2, should_abort=should_abort)
        summary = re.sub(r"\s+", " ", str(data.get("summary") or "")).strip()
        if not summary:
            raise ValueError("Empty summary")
        doc_type = str(data.get("doc_type") or "other").lower()
        if doc_type not in DOC_TYPES:
            doc_type = "other"
        tags = clean_tags(data.get("tags") or [])
        model_id = self.ctx.llm.model_id or (self.ctx.models.active_id("llm") or "unknown")
        lang = db.scalar("SELECT language FROM files WHERE id = ?", (file_id,))
        self._store(file_id, summary, doc_type, tags, lang, model_id)
        log.info("summary_done", file_id=file_id)
        return True

    def _store(self, file_id: int, summary: str, doc_type: str, tags: list[str], lang: str | None, model_id: str) -> None:
        db = self.ctx.db
        embedder = self.ctx.embedders.get()
        now = utcnow()
        with db.write() as conn:
            if conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone() is None:
                return
            conn.execute(
                "INSERT INTO summaries (file_id, summary, doc_type, language, model_id, prompt_version, generated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(file_id) DO UPDATE SET summary=excluded.summary, "
                "doc_type=excluded.doc_type, language=excluded.language, model_id=excluded.model_id, "
                "prompt_version=excluded.prompt_version, generated_at=excluded.generated_at",
                (file_id, summary, doc_type, lang, model_id, PROMPT_VERSION, now),
            )
            conn.execute("DELETE FROM file_tags WHERE file_id = ? AND source = 'ai'", (file_id,))
            for t in tags:
                conn.execute("INSERT INTO tags (name) VALUES (?) ON CONFLICT(name) DO NOTHING", (t,))
                tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (t,)).fetchone()["id"]
                conn.execute("INSERT OR IGNORE INTO file_tags (file_id, tag_id, source) VALUES (?, ?, 'ai')",
                             (file_id, tag_id))
            old = conn.execute("SELECT id FROM chunks WHERE file_id = ? AND source = 'summary'", (file_id,)).fetchall()
            conn.execute("DELETE FROM chunks WHERE file_id = ? AND source = 'summary'", (file_id,))
            cur = conn.execute(
                "INSERT INTO chunks (file_id, chunk_index, source, page_start, page_end, text, char_count, "
                "embedding_model) VALUES (?, 0, 'summary', NULL, NULL, ?, ?, ?)",
                (file_id, summary, len(summary), embedder.model_id if embedder else ""),
            )
            chunk_id = int(cur.lastrowid)
            conn.execute("UPDATE files SET summary_state='done', updated_at=? WHERE id=?", (now, file_id))
            conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)")
        if embedder is not None:
            if old:
                self.ctx.vectors.delete_chunks(embedder.model_id, [r["id"] for r in old])
            vec = embedder.embed_passages([summary])
            self.ctx.vectors.add(embedder.model_id, embedder.dim, [chunk_id], [file_id], vec)

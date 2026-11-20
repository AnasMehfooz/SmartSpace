"""Summary + tags + doc_type per file (SDD 6.4), via llama-server JSON-schema mode."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Callable

from app.db import utcnow
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.context import AppContext

log = get_logger("summarizer")

PROMPT_VERSION = "s3"  # s3 = fact-first, written in the file's language; doc_type from its own short
#                      classification request (29/30 correct vs 21/30 inside the summary request, 3B
#                      model). Older summaries are regenerated.
LANGUAGE_NAMES = {"de": "German", "en": "English"}
DOC_TYPES = ["invoice", "contract", "cv", "lecture", "note", "report", "letter", "form", "photo", "other"]
SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 8},
    },
    "required": ["summary", "tags"],
    "additionalProperties": False,
}
CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"doc_type": {"type": "string", "enum": DOC_TYPES}},
    "required": ["doc_type"],
    "additionalProperties": False,
}
CLASSIFY_CHARS = 2500  # the start of a document is enough to tell its kind
# One worked example, sent as its own earlier turn, shows the fact-first style. An inline example in the
# instructions made the 3B model start fact-first but also copy the example's date into other summaries;
# with no example at all, 18/30 summaries began "This document ...". The example has no dates or amounts.
FEW_SHOT = {
    "de": ("Antrag_Bibliotheksausweis.pdf",
           "Stadtbibliothek Mitte\nAntrag auf einen Bibliotheksausweis\nName: Lisa Weber\nAusweisart: Jahresausweis\n"
           "Unterschrift: vorhanden",
           {"summary": "Antrag auf einen Jahresausweis der Stadtbibliothek Mitte, gestellt von Lisa Weber. "
                       "Der Antrag ist unterschrieben.",
            "tags": ["bibliothek", "bibliotheksausweis", "antrag", "jahresausweis"]}),
    "en": ("Library_Card_Application.pdf",
           "Riverside Library\nLibrary card application\nName: Lisa Weber\nCard type: annual card\nSigned: yes",
           {"summary": "Application for an annual library card at Riverside Library, made by Lisa Weber. "
                       "The application is signed.",
            "tags": ["library", "library card", "application", "annual card"]}),
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
        f = db.query_one("SELECT id, name, status, language FROM files WHERE id = ?", (file_id,))
        if f is None or f["status"] != "indexed":
            return False
        rows = db.query(
            "SELECT text FROM chunks WHERE file_id = ? AND source IN ('text','ocr') ORDER BY chunk_index", (file_id,))
        if not rows:
            db.execute("UPDATE files SET summary_state='none' WHERE id=?", (file_id,))
            return False
        text = "\n\n".join(r["text"] for r in rows)
        head = text[:CLASSIFY_CHARS]
        # Name the language explicitly: "same language as the document" alone is ignored by small models, which
        # then translate (and mistranslate) German terms such as "Haftpflichtversicherung".
        language = LANGUAGE_NAMES.get(f["language"] or "", "the same language as the document")
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
                    [{"role": "user", "content": tpl.format(name=f["name"], part=i, parts=len(blocks), text=b, language=language)}],
                    max_tokens=160, temperature=0.1, should_abort=should_abort)
                partials.append(out.strip())
            text = "\n".join(partials)[:budget]
        messages = self.summary_messages(f["name"], text[:budget], f["language"])
        data, _ = client.complete_json(messages, SUMMARY_SCHEMA, max_tokens=700, temperature=0.2,
                                       should_abort=should_abort)
        summary = re.sub(r"\s+", " ", str(data.get("summary") or "")).strip()
        if not summary:
            raise ValueError("Empty summary")
        doc_type = self._classify(f["name"], head, should_abort)
        tags = clean_tags(data.get("tags") or [])
        model_id = self.ctx.llm.model_id or (self.ctx.models.active_id("llm") or "unknown")
        self._store(file_id, summary, doc_type, tags, f["language"], model_id)
        log.info("summary_done", file_id=file_id)
        return True

    def summary_messages(self, name: str, text: str, lang: str | None) -> list[dict[str, str]]:
        """The worked example (in the file's language) as an earlier turn, then the real request."""
        language = LANGUAGE_NAMES.get(lang or "", "the same language as the document")
        tpl = self._prompt("summarize.txt")
        ex_name, ex_text, ex_answer = FEW_SHOT.get(lang or "", FEW_SHOT["en"])
        ex_language = LANGUAGE_NAMES["de" if (lang or "") == "de" else "en"]
        return [
            {"role": "user", "content": tpl.format(name=ex_name, text=ex_text, language=ex_language)},
            {"role": "assistant", "content": json.dumps(ex_answer, ensure_ascii=False)},
            {"role": "user", "content": tpl.format(name=name, text=text, language=language)},
        ]

    def _classify(self, name: str, head: str, should_abort: Callable[[], bool]) -> str:
        """doc_type from a short request of its own: small models label much better when that is the only task."""
        try:
            data, _ = self.ctx.llm_client.complete_json(
                [{"role": "user", "content": self._prompt("classify.txt").format(name=name, text=head)}],
                CLASSIFY_SCHEMA, max_tokens=20, temperature=0.0, should_abort=should_abort)
        except ValueError:
            return "other"
        doc_type = str(data.get("doc_type") or "other").lower()
        return doc_type if doc_type in DOC_TYPES else "other"

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

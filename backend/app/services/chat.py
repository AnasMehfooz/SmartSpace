"""Chat sessions + RAG answers with citations (SDD 8)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

from starlette.concurrency import run_in_threadpool

from app.db import utcnow
from app.errors import AppError
from app.logging_setup import get_logger
from app.services.language import detect_language

if TYPE_CHECKING:
    from app.context import AppContext
    from app.schemas import SearchFilters

log = get_logger("chat")

ANSWER_RESERVE_TOKENS = 512
CHUNK_CHARS = 900
HISTORY_TURNS = 4
FILE_CARDS = 5
NO_LLM = {
    "en": "Install a language model to get answers. Here are the most relevant files.",
    "de": "Installiere ein Sprachmodell, um Antworten zu erhalten. Hier sind die passendsten Dateien.",
}
NOT_FOUND = {
    "en": "I could not find this in the indexed files.",
    "de": "Ich konnte dazu nichts in den indexierten Dateien finden.",
}
_CITE = re.compile(r"\[(\d{1,2}(?:\s*,\s*\d{1,2})*)\]")


def est_tokens(text: str) -> int:
    return len(text) // 4 + 4  # SDD: chars/4 approximation


def parse_citations(answer: str) -> list[int]:
    nums: list[int] = []
    for m in _CITE.finditer(answer):
        for part in m.group(1).split(","):
            n = int(part.strip())
            if n not in nums:
                nums.append(n)
    return nums


def _page_label(c: dict[str, Any]) -> str:
    if c.get("page") is None:
        return ""
    if c.get("page_end") and c["page_end"] != c["page"]:
        return f", pages {c['page']}-{c['page_end']}"
    return f", page {c['page']}"


class ChatService:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.db = ctx.db
        self._active: dict[int, asyncio.Event] = {}

    # ---- sessions ---------------------------------------------------------------------------
    def list_sessions(self, limit: int, offset: int) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT s.id, s.title, s.created_at, s.updated_at, "
            "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id) AS message_count "
            "FROM chat_sessions s ORDER BY s.updated_at DESC, s.id DESC LIMIT ? OFFSET ?", (limit, offset))
        total = self.db.scalar("SELECT COUNT(*) FROM chat_sessions") or 0
        return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    def create_session(self, title: str | None) -> dict[str, Any]:
        now = utcnow()
        title = (title or "").strip()[:200] or "New chat"
        with self.db.write() as conn:
            cur = conn.execute("INSERT INTO chat_sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
                               (title, now, now))
            sid = int(cur.lastrowid)
        return {"id": sid, "title": title, "created_at": now, "updated_at": now, "message_count": 0}

    def require_session(self, session_id: int):
        row = self.db.query_one("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
        if row is None:
            raise AppError("SESSION_NOT_FOUND", f"Chat session {session_id} does not exist")
        return row

    def get_session(self, session_id: int) -> dict[str, Any]:
        s = self.require_session(session_id)
        msgs = []
        for m in self.db.query("SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id", (session_id,)):
            d = dict(m)
            d["citations"] = json.loads(d.pop("citations_json") or "[]")
            d.pop("session_id", None)
            msgs.append(d)
        return {"id": s["id"], "title": s["title"], "created_at": s["created_at"], "updated_at": s["updated_at"],
                "messages": msgs, "generating": session_id in self._active}

    def rename(self, session_id: int, title: str) -> dict[str, Any]:
        self.require_session(session_id)
        title = title.strip()[:200]
        if not title:
            raise AppError("VALIDATION_ERROR", "Title must not be empty")
        self.db.execute("UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?", (title, utcnow(), session_id))
        return self.get_session(session_id)

    def delete(self, session_id: int) -> None:
        self.require_session(session_id)
        self.stop(session_id)
        self.db.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))

    def delete_all(self) -> int:
        self.stop_all()
        return self.db.execute("DELETE FROM chat_sessions").rowcount

    def validate_new_message(self, session_id: int, content: str) -> None:
        """Checks done before the SSE stream starts, so they return a proper HTTP error."""
        self.require_session(session_id)
        if not content or not content.strip():
            raise AppError("VALIDATION_ERROR", "Message must not be empty")
        if session_id in self._active:
            raise AppError("VALIDATION_ERROR", "This chat is already generating an answer", status=409)

    def stop(self, session_id: int) -> bool:
        ev = self._active.get(session_id)
        if ev is None:
            return False
        ev.set()
        return True

    def stop_all(self) -> None:
        for ev in list(self._active.values()):
            ev.set()

    # ---- messages ---------------------------------------------------------------------------
    def _history(self, session_id: int, before_id: int) -> list[dict[str, str]]:
        rows = self.db.query(
            "SELECT role, content FROM chat_messages WHERE session_id = ? AND id < ? AND content != '' "
            "ORDER BY id DESC LIMIT ?", (session_id, before_id, HISTORY_TURNS * 2))
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def _save_user(self, session_id: int, content: str) -> tuple[int, bool]:
        now = utcnow()
        with self.db.write() as conn:
            first = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = ? AND role = 'user'",
                                 (session_id,)).fetchone()[0] == 0
            cur = conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                (session_id, content, now))
            mid = int(cur.lastrowid)
            if first:
                title = " ".join(content.split())[:40] or "New chat"
                conn.execute("UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ? AND title = 'New chat'",
                             (title, now, session_id))
            conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
        return mid, first

    def _create_assistant(self, session_id: int, model_id: str | None) -> int:
        cur = self.db.execute(
            "INSERT INTO chat_messages (session_id, role, content, model_id, created_at) VALUES (?, 'assistant', '', ?, ?)",
            (session_id, model_id, utcnow()))
        return int(cur.lastrowid)

    def _finish_assistant(self, message_id: int, content: str, citations: list[dict[str, Any]],
                          tokens_in: int | None, tokens_out: int | None) -> None:
        with self.db.write() as conn:
            conn.execute(
                "UPDATE chat_messages SET content = ?, citations_json = ?, tokens_in = ?, tokens_out = ? WHERE id = ?",
                (content, json.dumps(citations, ensure_ascii=False), tokens_in, tokens_out, message_id))
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE id = (SELECT session_id FROM chat_messages WHERE id = ?)",
                (utcnow(), message_id))

    def _prompt(self, name: str) -> str:
        return (self.ctx.cfg.prompts_dir / name).read_text(encoding="utf-8")

    def build_messages(self, history: list[dict[str, str]], question: str, chunks: list[dict[str, Any]],
                       ctx_size: int) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """System prompt with numbered passages that fit the budget:
        ctx - 512 (answer) - system prompt - history (last 4 turns, trimmed)."""
        system_tpl = self._prompt("rag_answer.txt")
        budget = ctx_size - ANSWER_RESERVE_TOKENS
        hist = [{"role": m["role"], "content": m["content"][:1500]} for m in history[-HISTORY_TURNS * 2:]]
        while hist and sum(est_tokens(m["content"]) for m in hist) > budget // 4:
            hist = hist[2:] if len(hist) > 2 else []
        used = est_tokens(system_tpl) + est_tokens(question) + sum(est_tokens(m["content"]) for m in hist)
        parts: list[str] = []
        labelled: list[dict[str, Any]] = []
        for c in chunks:
            n = len(labelled) + 1
            text = c["text"][:CHUNK_CHARS]
            part = f'[{n}] (file: {c["name"]}{_page_label(c)})\n"""\n{text}\n"""'
            if used + est_tokens(part) > budget:
                break
            used += est_tokens(part)
            parts.append(part)
            labelled.append({**c, "n": n})
        system = system_tpl.format(context="\n\n".join(parts))
        return [{"role": "system", "content": system}, *hist, {"role": "user", "content": question}], labelled

    async def _rewrite(self, history: list[dict[str, str]], question: str) -> str:
        tpl = self._prompt("query_rewrite.txt")
        hist = "\n".join(f"{m['role']}: {m['content'][:400]}" for m in history[-HISTORY_TURNS * 2:])
        text, _ = await run_in_threadpool(
            self.ctx.llm_client.complete, [{"role": "user", "content": tpl.format(history=hist, question=question)}],
            max_tokens=60, temperature=0.0)
        text = text.strip().strip('"').strip()
        return text.splitlines()[0][:500] if text else question

    async def stream_answer(self, session_id: int, content: str,
                            filters: "SearchFilters | None") -> AsyncIterator[tuple[str, dict[str, Any]]]:
        t0 = time.monotonic()
        self.require_session(session_id)
        content = content.strip()
        if not content:
            raise AppError("VALIDATION_ERROR", "Message must not be empty")
        if session_id in self._active:
            raise AppError("VALIDATION_ERROR", "This chat is already generating an answer")
        lang = detect_language(content) or "en"
        user_id, is_first = self._save_user(session_id, content)
        history = self._history(session_id, user_id)
        llm = self.ctx.models.active("llm")
        search_only = llm is None
        stop_event = asyncio.Event()
        self._active[session_id] = stop_event
        assistant_id = self._create_assistant(session_id, llm.id if llm else None)

        answer = ""
        citations: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        gate = False
        failed = False
        try:
            yield "meta", {"message_id": assistant_id, "user_message_id": user_id,
                           "model_id": llm.id if llm else None, "search_only": search_only}
            query = content
            if not search_only and not is_first and history and self.ctx.hardware.tier > 0:
                self.ctx.gate.enter()
                gate = True
                try:
                    query = await self._rewrite(history, content)
                except Exception:
                    log.warning("query_rewrite_failed")
                    query = content
            k = int(self.ctx.settings.get("rag_top_k"))
            cards, chunks = await run_in_threadpool(self.ctx.search.search_with_chunks, query, filters, FILE_CARDS, k, 2)
            yield "files", {"results": cards}
            if search_only:
                answer = NO_LLM.get(lang, NO_LLM["en"])
                yield "token", {"t": answer}
            elif not chunks:
                answer = NOT_FOUND.get(lang, NOT_FOUND["en"])
                yield "token", {"t": answer}
            else:
                if not gate:
                    self.ctx.gate.enter()
                    gate = True
                await run_in_threadpool(self.ctx.llm.ensure_ready)
                ctx_size = int(self.ctx.llm.ctx_size or 4096)
                messages, labelled = self.build_messages(history, content, chunks, ctx_size)
                async for item in self.ctx.llm_client.astream(messages, max_tokens=512, temperature=0.2, top_p=0.9,
                                                              stop_event=stop_event):
                    if "t" in item:
                        answer += item["t"]
                        yield "token", {"t": item["t"]}
                    elif "usage" in item:
                        usage = item["usage"]
                by_n = {c["n"]: c for c in labelled}
                for n in parse_citations(answer):
                    c = by_n.get(n)
                    if c:
                        citations.append({"n": n, "file_id": c["file_id"], "chunk_id": c["chunk_id"], "page": c["page"],
                                          "name": c["name"], "rel_path": c["rel_path"], "score": round(c["score"], 5)})
            yield "sources", {"citations": citations}
        except AppError as e:
            failed = True
            if not answer:
                answer = ""
            yield "error", {"code": e.code, "message": e.message, "details": e.details}
        except Exception as e:  # pragma: no cover - defensive
            failed = True
            log.exception("chat_failed", session_id=session_id)
            yield "error", {"code": "INTERNAL_ERROR", "message": f"{e.__class__.__name__}"}
        finally:
            if gate:
                self.ctx.gate.exit()
            self._active.pop(session_id, None)
            # synchronous on purpose: this also runs when the client disconnects (task cancelled)
            self._finish_assistant(assistant_id, answer, citations, usage.get("tokens_in"), usage.get("tokens_out"))
        yield "done", {"message_id": assistant_id, "tokens_in": usage.get("tokens_in"),
                       "tokens_out": usage.get("tokens_out"), "took_ms": int((time.monotonic() - t0) * 1000),
                       "stopped": stop_event.is_set(), "failed": failed}

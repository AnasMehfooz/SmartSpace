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
from app.services.files import short_summary
from app.services.language import detect_language

if TYPE_CHECKING:
    from app.context import AppContext
    from app.schemas import SearchFilters

log = get_logger("chat")

ANSWER_RESERVE_TOKENS = 512
CHUNK_CHARS = 900
HISTORY_TURNS = 4
FILE_CARDS = 5
LABEL_SUMMARY_CHARS = 140
REFUSAL_PROBE_CHARS = 160
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


# "I could not find it" style answers (English and German), checked on the first sentence of an answer.
_REFUSAL = re.compile(
    r"could(?: not|n[’']t) find|can(?:not|[’']t) find|unable to find|no (?:information|files?|mention) "
    r"(?:about|on|of|related)|not (?:in|found in|mentioned in) the (?:indexed|provided)|nicht (?:finden|gefunden)"
    r"|keine .{0,80}? (?:finden|gefunden)|nichts .{0,60}? (?:finden|gefunden)",
    re.IGNORECASE,
)
# Retry a refusal only when search clearly found something: a top file that scores like a hit in both
# keyword and vector search (or by name) and is ahead of the next file.
# Any date in a passage: 1.3.2024, 2024-03-01, "1. März 2024", "March 2024", "Q3 2024", a bare year.
_DATE = re.compile(
    r"\b\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b|\b(?:19|20)\d{2}\b|"
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|januar|februar|märz|mai|juni|juli|oktober|dezember)[a-z]*\b",
    re.IGNORECASE,
)
RETRY_MIN_SCORE = 0.03
RETRY_LEAD = 1.3


def is_refusal(text: str) -> bool:
    return bool(_REFUSAL.search(text or ""))


_NUM = re.compile(r"\d[\d.,]*\d|\d")


def canon_number(token: str) -> str | None:
    """One form for a number written in any style, so an answer and a passage can be compared:
    "2.850,00" / "2,850.00" / "2850" -> "2850", "7,90" / "7.90" -> "7.9". None for single digits and bare years,
    which are too common to tell sources apart."""
    t = token.strip(".,")
    if "." in t and "," in t:  # both separators: the last one is the decimal point
        dec, thou = (".", ",") if t.rfind(".") > t.rfind(",") else (",", ".")
        t = t.replace(thou, "").replace(dec, ".")
    elif "." in t or "," in t:
        sep = "." if "." in t else ","
        parts = t.split(sep)
        # "2.850" / "1,000,000" group thousands; "7,90" / "3.5" are decimals
        t = t.replace(sep, "") if len(parts) > 2 or len(parts[1]) == 3 else t.replace(sep, ".")
    try:
        v = float(t)
    except ValueError:
        return None
    if v == int(v) and (v < 10 or (1900 <= v <= 2099 and t.isdigit())):
        return None
    return f"{v:.2f}".rstrip("0").rstrip(".")


def number_set(text: str) -> set[str]:
    return {c for c in (canon_number(t) for t in _NUM.findall(text or "")) if c}


_TAIL_NUMBER = re.compile(r"\d[\d.,]*$")


class AmountFixer:
    """Streams answer text and puts back the source's own spelling of an amount the model garbled by commas and
    points only: a German source says 1.042,29, the small model wrote "1,042,29". An amount whose digits match no
    source number, or that already means a source number, is left as it is."""

    def __init__(self, source_texts: list[str]):
        self.known: set[str] = set()
        by_digits: dict[str, set[str]] = {}
        for text in source_texts:
            for tok in _NUM.findall(text or ""):
                c = canon_number(tok)
                if c:
                    self.known.add(c)
                    by_digits.setdefault(re.sub(r"\D", "", tok), set()).add(tok)
        # only spellings that all mean the same number can replace a garbled one
        self.by_digits = {d: next(iter(toks)) for d, toks in by_digits.items()
                          if len({canon_number(t) for t in toks}) == 1}
        self.buf = ""
        self.fixed = 0

    def _fix(self, text: str) -> str:
        def repl(m: re.Match) -> str:
            tok = m.group(0)
            c = canon_number(tok)
            if c is None or c in self.known:
                return tok
            good = self.by_digits.get(re.sub(r"\D", "", tok))
            if good is None:
                return tok
            self.fixed += 1
            return good
        return _NUM.sub(repl, text)

    def feed(self, t: str) -> str:
        """Text that is safe to send now; a number still growing at the end is held back."""
        self.buf += t
        m = _TAIL_NUMBER.search(self.buf)
        cut = m.start() if m else len(self.buf)
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return self._fix(out)

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return self._fix(out)


def _interleave(lists: list[list[dict[str, Any]]], key: str, limit: int,
                max_per_file: int | None = None) -> list[dict[str, Any]]:
    """Merge ranked lists (search for the rewritten and for the original question) by taking turns:
    #1 of each list comes before any #2, so the best hit of either question always makes the cut.
    (Rank fusion would favour items found by both, which lets a drifted rewrite push the right file down.)"""
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    per_file: dict[int, int] = {}
    for rank in range(max((len(x) for x in lists), default=0)):
        for items in lists:
            if rank >= len(items):
                continue
            item = items[rank]
            if item[key] in seen:
                continue
            if max_per_file is not None:
                fid = item["file_id"]
                if per_file.get(fid, 0) >= max_per_file:
                    continue
                per_file[fid] = per_file.get(fid, 0) + 1
            seen.add(item[key])
            out.append(item)
            if len(out) >= limit:
                return out
    return out


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

    def _file_notes(self, file_ids: list[int]) -> dict[int, tuple[str | None, str | None]]:
        """doc_type and a one-line summary per file, so the model knows what each source is."""
        if not file_ids:
            return {}
        rows = self.db.query(
            f"SELECT file_id, summary, doc_type FROM summaries WHERE file_id IN ({','.join('?' * len(file_ids))})",
            file_ids)
        return {r["file_id"]: (r["doc_type"], short_summary(r["summary"], LABEL_SUMMARY_CHARS)) for r in rows}

    def build_messages(self, history: list[dict[str, str]], question: str, chunks: list[dict[str, Any]],
                       ctx_size: int, hint: str | None = None) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        """System prompt with one numbered source per file (all its passages under one number, so a file's text
        and its AI summary never look like two files), within the budget:
        ctx - 512 (answer) - system prompt - history (last 4 turns, trimmed)."""
        system_tpl = self._prompt("rag_answer.txt")
        budget = ctx_size - ANSWER_RESERVE_TOKENS
        hist = [{"role": m["role"], "content": m["content"][:1500]} for m in history[-HISTORY_TURNS * 2:]]
        while hist and sum(est_tokens(m["content"]) for m in hist) > budget // 4:
            hist = hist[2:] if len(hist) > 2 else []
        used = (est_tokens(system_tpl) + est_tokens(question) + sum(est_tokens(m["content"]) for m in hist)
                + (est_tokens(hint) if hint else 0))
        groups: dict[int, list[dict[str, Any]]] = {}
        for c in chunks:  # chunks arrive best first, so files keep the rank of their best chunk
            groups.setdefault(c["file_id"], []).append(c)
        notes = self._file_notes(list(groups))
        parts: list[str] = []
        labelled: list[dict[str, Any]] = []
        for fid, group in groups.items():
            n = len(labelled) + 1
            first = group[0]
            doc_type, short = notes.get(fid, (None, None))
            block = f'[{n}] (file: {first["name"]}'
            block += f" · {doc_type}" if doc_type and doc_type != "other" else ""
            # A small model otherwise gives an undated receipt the date of another source (seen: 6/12 answers).
            block += " · no date" if not any(_DATE.search(c["text"]) for c in group) else ""
            block += f' · "{short}"' if short else ""
            block += ")"
            passages = 0
            for c in group:
                head = "AI summary of the file" if c.get("source") == "summary" else f"Text{_page_label(c)}"
                piece = f'\n{head}:\n"""\n{c["text"][:CHUNK_CHARS]}\n"""'
                if used + est_tokens(block + piece) > budget:
                    break
                block += piece
                passages += 1
            if not passages:
                break
            used += est_tokens(block)
            parts.append(block)
            labelled.append({**first, "n": n})
        system = system_tpl.format(context="\n\n".join(parts))
        if hint:
            system += "\n\n" + hint
        return [{"role": "system", "content": system}, *hist, {"role": "user", "content": question}], labelled

    def _retrieve(self, original: str, query: str, filters: "SearchFilters | None",
                  k: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Search for the rewritten question; if the rewrite changed it, also for the original one and fuse both,
        so a follow-up rewrite that drifts to the previous topic cannot hide the right file."""
        search = self.ctx.search.search_with_chunks
        cards, chunks = search(query, filters, FILE_CARDS, k, 2)
        if " ".join(query.lower().split()) == " ".join(original.lower().split()):
            return cards, chunks
        cards2, chunks2 = search(original, filters, FILE_CARDS, k, 2)
        return (_interleave([cards, cards2], "file_id", FILE_CARDS),
                _interleave([chunks, chunks2], "chunk_id", k, max_per_file=2))

    @staticmethod
    def _worth_retry(cards: list[dict[str, Any]]) -> bool:
        if not cards:
            return False
        top = cards[0]["score"]
        nxt = cards[1]["score"] if len(cards) > 1 else 0.0
        return top >= RETRY_MIN_SCORE and top >= nxt * RETRY_LEAD

    @staticmethod
    def _cite_by_numbers(answer: str, labelled: list[dict[str, Any]],
                         chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """For an answer with no [n] marker and no file name: credit each number it quotes (950,00, 2.850,00) to
        the best-ranked source whose passages contain it, pointing at the passage that holds most of them (page
        text before the AI summary). A number that is in no source cites nothing."""
        wanted = number_set(answer)
        if not wanted:
            return []
        n_of = {c["file_id"]: c["n"] for c in labelled}
        owner: dict[str, int] = {}
        best: dict[int, tuple[tuple[int, bool], dict[str, Any]]] = {}
        for ch in chunks:
            n = n_of.get(ch["file_id"])
            hits = number_set(ch["text"]) & wanted if n is not None else set()
            if not hits:
                continue
            for num in hits:
                owner[num] = min(owner.get(num, n), n)
            key = (len(hits), ch.get("source") != "summary")
            if n not in best or key > best[n][0]:
                best[n] = (key, ch)
        return [{**best[n][1], "n": n} for n in sorted(set(owner.values()))]

    @staticmethod
    def _retry_hint(top: dict[str, Any]) -> str:
        return (f"Note: source [{top['n']}] ({top['name']}) is the closest match in the user's files. If it is "
                "related to the question, describe what it shows and cite it. Only if it is unrelated, say you "
                "could not find it.")

    async def _stream_checked(self, messages: list[dict[str, str]], stop_event: asyncio.Event,
                              usage: dict[str, int], state: dict[str, bool], check: bool) -> AsyncIterator[str]:
        """Stream answer text. With `check`, hold back the first sentence: if it is an "I could not find it"
        refusal, stop early and set state["refused"] instead of sending it, so the caller can retry once."""
        held, holding = "", check
        agen = self.ctx.llm_client.astream(messages, max_tokens=512, temperature=0.2, top_p=0.9,
                                           stop_event=stop_event)
        try:
            async for item in agen:
                if "usage" in item:
                    usage.update(item["usage"])
                    continue
                t = item.get("t", "")
                if not holding:
                    yield t
                    continue
                held += t
                end = re.search(r"[.!?\n]", held)
                if end or len(held) >= REFUSAL_PROBE_CHARS:
                    if is_refusal(held[: end.end()] if end else held):
                        state["refused"] = True
                        return
                    holding = False
                    yield held
            if holding and held:
                if is_refusal(held):
                    state["refused"] = True
                    return
                yield held
        finally:
            await agen.aclose()

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
            cards, chunks = await run_in_threadpool(self._retrieve, content, query, filters, k)
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
                state = {"refused": False}
                retry = bool(labelled) and self._worth_retry(cards)
                fixer = AmountFixer([c["text"] for c in chunks])
                async for t in self._stream_checked(messages, stop_event, usage, state, check=retry):
                    if t := fixer.feed(t):
                        answer += t
                        yield "token", {"t": t}
                if t := fixer.flush():
                    answer += t
                    yield "token", {"t": t}
                if state["refused"] and not stop_event.is_set():
                    # The small model said "not found" although search clearly found a file: ask once more,
                    # naming the best source. The first attempt was held back, so the user only sees this one.
                    log.info("chat_refusal_retry")
                    messages, labelled = self.build_messages(history, content, chunks, ctx_size,
                                                             hint=self._retry_hint(labelled[0]))
                    async for t in self._stream_checked(messages, stop_event, usage, state, check=False):
                        if t := fixer.feed(t):
                            answer += t
                            yield "token", {"t": t}
                    if t := fixer.flush():
                        answer += t
                        yield "token", {"t": t}
                if fixer.fixed:
                    log.info("chat_amounts_fixed", count=fixer.fixed)
                # An answer that says it found nothing must not cite files; clients show the files event instead.
                if not is_refusal(answer):
                    by_n = {c["n"]: c for c in labelled}
                    picked = [by_n[n] for n in parse_citations(answer) if n in by_n]
                    if not picked:  # the model sometimes names the file instead of writing [n]
                        low = answer.lower()
                        picked = [c for c in labelled if c["name"].lower() in low]
                    if not picked:  # ... or writes no marker at all: credit the source that holds its numbers
                        picked = self._cite_by_numbers(answer, labelled, chunks)
                        if picked:
                            log.info("chat_cited_by_numbers", sources=[c["n"] for c in picked])
                    for c in picked:
                        citations.append({"n": c["n"], "file_id": c["file_id"], "chunk_id": c["chunk_id"],
                                          "page": c["page"], "name": c["name"], "rel_path": c["rel_path"],
                                          "score": round(c["score"], 5)})
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

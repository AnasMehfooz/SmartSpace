"""Hybrid search (SDD 7): vector (LanceDB) + keyword (FTS5 bm25) fused with Reciprocal Rank Fusion,
plus a filename bonus. Queries are never stored or logged."""

from __future__ import annotations

import html
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, time as dtime, timezone
from typing import TYPE_CHECKING, Any

from app.db.database import ns_to_iso
from app.errors import AppError
from app.services.paths import clean_rel

if TYPE_CHECKING:
    from app.context import AppContext
    from app.schemas import SearchFilters

RRF_K = 60
CANDIDATES = 40
FILENAME_BONUS = 0.02
QUICK_LIMIT = 6
_TERM = re.compile(r"\w+", re.UNICODE)

STOPWORDS = {
    # en
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "was", "be", "my", "me",
    "i", "you", "it", "this", "that", "where", "what", "which", "who", "how", "when", "do", "does", "did", "find",
    "show", "please", "can", "from", "about", "at", "by", "as", "all", "any", "there", "have", "has",
    # de
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "und", "oder", "ist",
    "sind", "war", "mein", "meine", "meinen", "meinem", "ich", "du", "es", "wo", "was", "wie", "wann", "welche",
    "welcher", "zu", "im", "in", "auf", "für", "mit", "von", "vom", "zum", "zur", "bitte", "finde", "zeige", "über",
}


# ---- pure helpers (unit tested) -------------------------------------------------------------

def normalize_query(q: str) -> str:
    return unicodedata.normalize("NFC", " ".join((q or "").split()))[:500]


def query_terms(q: str) -> list[str]:
    out: list[str] = []
    for t in _TERM.findall(q.lower()):
        if t not in out:
            out.append(t)
    return out[:32]


def keyword_terms(terms: list[str]) -> list[str]:
    content = [t for t in terms if t not in STOPWORDS]
    return content or terms


def fts_query(terms: list[str]) -> str | None:
    """Each term quoted (escapes FTS syntax); prefix match for the last term only; OR for recall."""
    if not terms:
        return None
    parts = [f'"{t}"' for t in terms[:-1]] + [f'"{terms[-1]}"*']
    return " OR ".join(parts)


def rrf_fuse(*ranked_lists: list[int], k: int = RRF_K) -> dict[int, float]:
    """score = sum(1 / (k + rank_i)) over every list the item appears in (rank starts at 1)."""
    scores: dict[int, float] = defaultdict(float)
    for lst in ranked_lists:
        for rank, item in enumerate(lst, start=1):
            scores[item] += 1.0 / (k + rank)
    return dict(scores)


def make_snippet(text: str, terms: list[str], max_len: int = 280) -> str:
    """Window around the first matched term, HTML-escaped, matches wrapped in <mark>."""
    flat = " ".join(text.split())
    pattern = None
    useful = [t for t in terms if t not in STOPWORDS] or terms
    if useful:
        alts = "|".join(re.escape(t) for t in sorted(useful, key=len, reverse=True))
        pattern = re.compile(rf"(?<!\w)({alts})", re.IGNORECASE)
    start = 0
    m = pattern.search(flat) if pattern else None
    if m and m.start() > max_len // 3:
        start = m.start() - max_len // 3
        sp = flat.find(" ", start)
        start = sp + 1 if 0 <= sp < m.start() else start
    end = min(len(flat), start + max_len)
    if end < len(flat):
        sp = flat.rfind(" ", start, end)
        end = sp if sp > start else end
    window = html.escape(flat[start:end])
    if pattern:
        window = pattern.sub(r"<mark>\1</mark>", window)
    return ("… " if start > 0 else "") + window + (" …" if end < len(flat) else "")


def to_ns(value: str, end: bool = False) -> int:
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            dt = datetime.combine(d, dtime.max if end else dtime.min, tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        raise AppError("VALIDATION_ERROR", f"Invalid date: {value}") from None
    return int(dt.timestamp() * 1e9)


def _ints(ids) -> str:
    return ",".join(str(int(i)) for i in ids)


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SearchService:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.db = ctx.db

    # ---- filters ----------------------------------------------------------------------------
    def _filter_clause(self, f: "SearchFilters | None") -> tuple[str, list[Any]]:
        if f is None:
            return "", []
        clauses: list[str] = []
        params: list[Any] = []
        if f.ext:
            exts = [e.lower().lstrip(".") for e in f.ext if e and e.strip()]
            if exts:
                clauses.append(f"f.ext IN ({','.join('?' * len(exts))})")
                params += exts
        if f.kind:
            clauses.append("f.kind = ?")
            params.append(f.kind)
        if f.folder:
            folder = clean_rel(f.folder)
            if folder:
                clauses.append("f.rel_path LIKE ? ESCAPE '\\'")
                params.append(_like_escape(folder) + "/%")
        if f.tags:
            tags = [t.strip() for t in f.tags if t and t.strip()]
            if tags:
                clauses.append(
                    "EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id = ft.tag_id WHERE ft.file_id = f.id "
                    f"AND t.name IN ({','.join('?' * len(tags))}))")
                params += tags
        if f.doc_type:
            clauses.append("EXISTS (SELECT 1 FROM summaries s WHERE s.file_id = f.id AND s.doc_type = ?)")
            params.append(f.doc_type)
        if f.date_from:
            clauses.append("f.mtime_ns >= ?")
            params.append(to_ns(f.date_from))
        if f.date_to:
            clauses.append("f.mtime_ns <= ?")
            params.append(to_ns(f.date_to, end=True))
        return " AND ".join(clauses), params

    def _allowed_ids(self, f: "SearchFilters | None") -> list[int] | None:
        where, params = self._filter_clause(f)
        if not where:
            return None
        return [r[0] for r in self.db.query(f"SELECT f.id FROM files f WHERE {where}", params)]

    # ---- retrieval --------------------------------------------------------------------------
    def _keyword(self, terms: list[str], allowed: list[int] | None) -> list[tuple[int, int]]:
        q = fts_query(keyword_terms(terms))
        if not q or (allowed is not None and not allowed):
            return []
        sql = ("SELECT c.id, c.file_id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
               "WHERE chunks_fts MATCH ?")
        if allowed is not None:
            sql += f" AND c.file_id IN ({_ints(allowed)})"
        sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
        try:
            rows = self.db.query(sql, (q, CANDIDATES))
        except sqlite3.OperationalError:
            return []
        return [(r[0], r[1]) for r in rows]

    def _vector(self, query: str, allowed: list[int] | None) -> list[tuple[int, int]]:
        if not query:
            return []
        emb = self.ctx.embedders.get()
        if emb is None:
            return []
        vec = emb.embed_query(query)
        return [(cid, fid) for cid, fid, _ in self.ctx.vectors.search(emb.model_id, vec, CANDIDATES, allowed)]

    def _name_matches(self, terms: list[str], allowed: list[int] | None) -> set[int]:
        cand = [t for t in terms if len(t) >= 3 and t not in STOPWORDS]
        if not cand or (allowed is not None and not allowed):
            return set()
        # Only files Smart Space reads: removed ("excluded"), skipped and failed files never come back by name.
        sql = ("SELECT f.id FROM files f WHERE f.status IN ('indexed', 'pending', 'processing') AND ("
               + " OR ".join("f.name LIKE ? ESCAPE '\\'" for _ in cand) + ")")
        if allowed is not None:
            sql += f" AND f.id IN ({_ints(allowed)})"
        sql += " LIMIT 200"
        return {r[0] for r in self.db.query(sql, [f"%{_like_escape(t)}%" for t in cand])}

    def _retrieve(self, query: str, f: "SearchFilters | None"):
        terms = query_terms(query)
        allowed = self._allowed_ids(f)
        kw = self._keyword(terms, allowed)
        vec = self._vector(query, allowed)
        chunk_file = {cid: fid for cid, fid in kw + vec}
        if chunk_file:  # drop stale vector hits (chunks replaced by a re-index)
            alive = {r[0] for r in self.db.query(f"SELECT id FROM chunks WHERE id IN ({_ints(chunk_file)})")}
            kw = [x for x in kw if x[0] in alive]
            vec = [x for x in vec if x[0] in alive]
            chunk_file = {c: fid for c, fid in chunk_file.items() if c in alive}
        scores = rrf_fuse([c for c, _ in vec], [c for c, _ in kw])
        names = self._name_matches(terms, allowed)
        return terms, scores, chunk_file, names

    @staticmethod
    def _rank_files(scores: dict[int, float], chunk_file: dict[int, int], names: set[int],
                    limit: int) -> list[tuple[int, float, int | None]]:
        best: dict[int, tuple[float, int]] = {}
        for cid, s in scores.items():
            fid = chunk_file[cid]
            if fid not in best or s > best[fid][0]:
                best[fid] = (s, cid)
        out: dict[int, tuple[float, int | None]] = {
            fid: (s + (FILENAME_BONUS if fid in names else 0.0), cid) for fid, (s, cid) in best.items()
        }
        for fid in names:
            out.setdefault(fid, (FILENAME_BONUS, None))
        ranked = sorted(out.items(), key=lambda kv: (-kv[1][0], kv[0]))[:limit]
        return [(fid, s, cid) for fid, (s, cid) in ranked]

    def _cards(self, ranked: list[tuple[int, float, int | None]], terms: list[str], mode: str) -> list[dict[str, Any]]:
        if not ranked:
            return []
        quick = mode == "quick"
        fids = [r[0] for r in ranked]
        cids = [r[2] for r in ranked if r[2] is not None]
        files = {r["id"]: r for r in self.db.query(
            f"SELECT id, rel_path, name, ext, kind, size_bytes, mtime_ns, status FROM files WHERE id IN ({_ints(fids)})")}
        chunks = {r["id"]: r for r in self.db.query(
            f"SELECT id, page_start, text, source FROM chunks WHERE id IN ({_ints(cids)})")} if cids else {}
        summaries: dict[int, Any] = {}
        tags: dict[int, list[str]] = defaultdict(list)
        if not quick:
            summaries = {r["file_id"]: r for r in self.db.query(
                f"SELECT file_id, summary, doc_type FROM summaries WHERE file_id IN ({_ints(fids)})")}
            for r in self.db.query(
                    f"SELECT ft.file_id, t.name FROM file_tags ft JOIN tags t ON t.id = ft.tag_id "
                    f"WHERE ft.file_id IN ({_ints(fids)}) ORDER BY ft.source DESC, t.name"):
                tags[r["file_id"]].append(r["name"])
        out = []
        for fid, score, cid in ranked:
            f = files.get(fid)
            if f is None:
                continue
            snippet = None
            if cid is not None and cid in chunks:
                c = chunks[cid]
                snippet = {"chunk_id": cid, "page": c["page_start"], "source": c["source"],
                           "text": make_snippet(c["text"], terms, 140 if quick else 280)}
            card: dict[str, Any] = {
                "file_id": fid, "rel_path": f["rel_path"], "name": f["name"], "ext": f["ext"], "kind": f["kind"],
                "score": round(score, 5), "snippet": snippet,
            }
            if not quick:
                s = summaries.get(fid)
                card.update({
                    "size_bytes": f["size_bytes"], "mtime": ns_to_iso(f["mtime_ns"]), "status": f["status"],
                    "summary": s["summary"] if s else None, "doc_type": s["doc_type"] if s else None,
                    "tags": tags.get(fid, []),
                })
            out.append(card)
        return out

    # ---- public -----------------------------------------------------------------------------
    def search(self, query: str, filters: "SearchFilters | None" = None, limit: int | None = None,
               mode: str = "full") -> dict[str, Any]:
        t0 = time.perf_counter()
        q = normalize_query(query)
        limit = QUICK_LIMIT if mode == "quick" else int(limit or self.ctx.settings.get("search_top_k"))
        terms, scores, chunk_file, names = self._retrieve(q, filters)
        ranked = self._rank_files(scores, chunk_file, names, limit)
        results = self._cards(ranked, terms, mode)
        return {"query": q, "mode": mode, "took_ms": int((time.perf_counter() - t0) * 1000), "results": results}

    def search_with_chunks(self, query: str, filters: "SearchFilters | None", file_limit: int, k: int,
                           max_per_file: int = 2) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """For RAG: file cards plus the top-k chunks (max `max_per_file` per file)."""
        q = normalize_query(query)
        terms, scores, chunk_file, names = self._retrieve(q, filters)
        cards = self._cards(self._rank_files(scores, chunk_file, names, file_limit), terms, "full")
        chunk_scores = {cid: s + (FILENAME_BONUS if chunk_file[cid] in names else 0.0) for cid, s in scores.items()}
        picked: list[tuple[int, int, float]] = []
        per_file: dict[int, int] = defaultdict(int)
        for cid, s in sorted(chunk_scores.items(), key=lambda kv: (-kv[1], kv[0])):
            fid = chunk_file[cid]
            if per_file[fid] >= max_per_file:
                continue
            per_file[fid] += 1
            picked.append((cid, fid, s))
            if len(picked) >= k:
                break
        for fid in names:  # files matched only by name: use their first chunk
            if len(picked) >= k:
                break
            if fid in per_file:
                continue
            row = self.db.query_one(
                "SELECT id FROM chunks WHERE file_id = ? ORDER BY CASE source WHEN 'summary' THEN 0 ELSE 1 END, "
                "chunk_index LIMIT 1", (fid,))
            if row:
                picked.append((row["id"], fid, FILENAME_BONUS))
                per_file[fid] += 1
        if not picked:
            return cards, []
        rows = {r["id"]: r for r in self.db.query(
            "SELECT c.id, c.file_id, c.page_start, c.page_end, c.text, c.source, f.name, f.rel_path "
            f"FROM chunks c JOIN files f ON f.id = c.file_id WHERE c.id IN ({_ints(c for c, _, _ in picked)})")}
        chunks = []
        for cid, fid, s in picked:
            r = rows.get(cid)
            if r is None:
                continue
            chunks.append({"chunk_id": cid, "file_id": fid, "name": r["name"], "rel_path": r["rel_path"],
                           "page": r["page_start"], "page_end": r["page_end"], "text": r["text"],
                           "source": r["source"], "score": s})
        return cards, chunks

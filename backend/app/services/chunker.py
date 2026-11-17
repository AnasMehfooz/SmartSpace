"""Chunker (SDD 6.3 step 4): paragraph, then sentence, then word boundaries; ~1600 chars with
240 overlap; never cuts mid-word; keeps page_start/page_end."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.extractors import Segment

_PARA_SPLIT = re.compile(r"\n[ \t]*\n+")
_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=\S)")
_TRAILING_WS = re.compile(r"[ \t]+\n")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


@dataclass
class Chunk:
    text: str
    page_start: int | None
    page_end: int | None
    source: str

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class _Unit:
    text: str
    page: int | None
    source: str


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = _TRAILING_WS.sub("\n", text)
    return _MULTI_SPACE.sub(" ", text)


def _split_words(text: str, max_len: int) -> list[str]:
    pieces: list[str] = []
    cur = ""
    for word in text.split():
        while len(word) > max_len:  # a single "word" longer than a chunk: unavoidable hard cut
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(word[:max_len])
            word = word[max_len:]
        candidate = f"{cur} {word}" if cur else word
        if len(candidate) > max_len:
            pieces.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        pieces.append(cur)
    return pieces


def _split_long(text: str, max_len: int) -> list[str]:
    """Split an over-long paragraph by sentences, then by words."""
    out: list[str] = []
    cur = ""
    for sent in _SENT_SPLIT.split(text):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) > max_len:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(_split_words(sent, max_len))
            continue
        candidate = f"{cur} {sent}" if cur else sent
        if len(candidate) > max_len:
            out.append(cur)
            cur = sent
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out


def _overlap_tail(text: str, overlap: int) -> str:
    """Last <= `overlap` chars of `text`, starting at a sentence or word boundary."""
    if overlap <= 0 or not text:
        return ""
    if len(text) <= overlap:
        return text
    window = text[-overlap:]
    m = _SENT_SPLIT.search(window)
    if m and m.end() < len(window) - 20:
        return window[m.end():].strip()
    sp = window.find(" ")
    if sp == -1:
        return ""
    return window[sp + 1 :].strip()


def chunk_segments(segments: list[Segment], target: int = 1600, overlap: int = 240) -> list[Chunk]:
    overlap = max(0, min(overlap, target // 2))
    max_unit = max(100, target - overlap)
    units: list[_Unit] = []
    for seg in segments:
        for para in _PARA_SPLIT.split(_clean(seg.text)):
            para = para.strip()
            if not para:
                continue
            if len(para) <= max_unit:
                units.append(_Unit(para, seg.page, seg.source))
            else:
                units.extend(_Unit(p, seg.page, seg.source) for p in _split_long(para, max_unit))

    chunks: list[Chunk] = []
    cur: list[_Unit] = []
    cur_len = 0
    has_new = False  # current chunk contains more than the carried-over overlap

    def emit() -> None:
        text = "\n\n".join(u.text for u in cur).strip()
        if not text:
            return
        pages = [u.page for u in cur if u.page is not None]
        sources = {u.source for u in cur}
        source = "ocr" if sources == {"ocr"} else "text"
        chunks.append(Chunk(text, min(pages) if pages else None, max(pages) if pages else None, source))

    for u in units:
        add = len(u.text) + (2 if cur else 0)
        if cur and has_new and cur_len + add > target:
            emit()
            last = cur[-1]
            tail = _overlap_tail(last.text, overlap)
            cur = [_Unit(tail, last.page, last.source)] if tail else []
            cur_len = len(tail)
            has_new = False
            add = len(u.text) + (2 if cur else 0)
            if cur_len + add > target:  # overlap would overflow the chunk: drop it
                cur, cur_len = [], 0
                add = len(u.text)
        cur.append(u)
        cur_len += add
        has_new = True
    if cur and has_new:
        emit()
    return chunks

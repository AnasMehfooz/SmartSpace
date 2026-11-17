"""Plain text formats: UTF-8 fast path, charset-normalizer for everything else."""

from __future__ import annotations

from pathlib import Path

from app.services.extractors import ExtractResult, Segment


def decode_bytes(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    from charset_normalizer import from_bytes

    best = from_bytes(data).best()
    if best is not None:
        return str(best)
    return data.decode("latin-1", errors="replace")


def extract_text(path: Path) -> ExtractResult:
    data = path.read_bytes()
    if not data.strip():
        return ExtractResult(skip_reason="empty")
    text = decode_bytes(data).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ExtractResult(skip_reason="empty")
    return ExtractResult(segments=[Segment(None, text, "text")], page_count=None)

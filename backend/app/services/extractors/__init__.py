"""Text extraction per file type (SDD 6.1). Every extractor opens files read-only."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.extractors.image_ocr import OcrEngine

DOCUMENT_EXTS = {"pdf", "docx", "pptx", "txt", "md", "markdown", "csv", "json", "log"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff"}


@dataclass
class Segment:
    page: int | None
    text: str
    source: str = "text"  # 'text' | 'ocr'


@dataclass
class ExtractResult:
    segments: list[Segment] = field(default_factory=list)
    page_count: int | None = None
    skip_reason: str | None = None  # encrypted | no_text | empty

    @property
    def text_chars(self) -> int:
        return sum(len(s.text.strip()) for s in self.segments)


class ExtractError(Exception):
    pass


def kind_for_ext(ext: str) -> str | None:
    ext = ext.lower()
    if ext in DOCUMENT_EXTS:
        return "document"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def extract(path: Path, ext: str, *, max_pdf_pages: int, ocr: "OcrEngine | None") -> ExtractResult:
    ext = ext.lower()
    if ext == "pdf":
        from app.services.extractors.pdf import extract_pdf

        return extract_pdf(path, max_pdf_pages, ocr)
    if ext == "docx":
        from app.services.extractors.docx import extract_docx

        return extract_docx(path)
    if ext == "pptx":
        from app.services.extractors.pptx import extract_pptx

        return extract_pptx(path)
    if ext in {"txt", "md", "markdown", "csv", "json", "log"}:
        from app.services.extractors.text import extract_text

        return extract_text(path)
    if ext in IMAGE_EXTS:
        from app.services.extractors.image_ocr import extract_image

        if ocr is None:
            raise ExtractError("OCR engine unavailable")
        return extract_image(path, ocr)
    raise ExtractError(f"Unsupported file type: .{ext}")

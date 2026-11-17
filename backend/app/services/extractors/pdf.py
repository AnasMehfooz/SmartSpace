"""PDF: PyMuPDF text per page; pages with < 20 chars are rasterized at 200 DPI and OCR'd."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from app.services.extractors import ExtractError, ExtractResult, Segment

if TYPE_CHECKING:
    from app.services.extractors.image_ocr import OcrEngine

MIN_PAGE_CHARS = 20
OCR_DPI = 200


def extract_pdf(path: Path, max_pages: int, ocr: "OcrEngine | None") -> ExtractResult:
    import pymupdf
    from PIL import Image

    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        raise ExtractError(f"Cannot open PDF ({e.__class__.__name__})") from None
    with doc:
        if doc.needs_pass:
            return ExtractResult(skip_reason="encrypted")
        page_count = doc.page_count
        segments: list[Segment] = []
        for i in range(min(page_count, max_pages)):
            page = doc.load_page(i)
            text = page.get_text("text", sort=True) or ""
            if len(text.strip()) < MIN_PAGE_CHARS and ocr is not None:
                pix = page.get_pixmap(dpi=OCR_DPI, alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                ocr_text = ocr.ocr(img)
                if len(ocr_text.strip()) > len(text.strip()):
                    segments.append(Segment(i + 1, ocr_text, "ocr"))
                    continue
            if text.strip():
                segments.append(Segment(i + 1, text, "text"))
        return ExtractResult(segments=segments, page_count=page_count)

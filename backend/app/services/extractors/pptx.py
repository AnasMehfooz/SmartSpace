"""PPTX: python-pptx slide text (shapes, tables, groups) and notes; the slide number is the page."""

from __future__ import annotations

from pathlib import Path

from app.services.extractors import ExtractError, ExtractResult, Segment


def _shape_texts(shape) -> list[str]:
    out: list[str] = []
    try:
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for s in shape.shapes:
                out.extend(_shape_texts(s))
            return out
    except Exception:
        pass
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        t = shape.text_frame.text
        if t.strip():
            out.append(t)
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            cells = [" ".join(c.text.split()) for c in row.cells]
            line = " | ".join(c for c in cells if c)
            if line:
                out.append(line)
    return out


def extract_pptx(path: Path) -> ExtractResult:
    try:
        from pptx import Presentation

        prs = Presentation(str(path))
    except Exception as e:
        raise ExtractError(f"Cannot open PPTX ({e.__class__.__name__})") from None
    segments: list[Segment] = []
    slides = list(prs.slides)
    for i, slide in enumerate(slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            texts.extend(_shape_texts(shape))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text if slide.notes_slide.notes_text_frame else ""
            if notes.strip():
                texts.append(notes)
        if texts:
            segments.append(Segment(i, "\n\n".join(texts), "text"))
    return ExtractResult(segments=segments, page_count=len(slides))

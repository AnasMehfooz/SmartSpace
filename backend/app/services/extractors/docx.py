"""DOCX: python-docx paragraphs, tables (flattened row by row), headers and footers."""

from __future__ import annotations

from pathlib import Path

from app.services.extractors import ExtractError, ExtractResult, Segment

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # encrypted OOXML lives in an OLE container


def _table_rows(table) -> list[str]:
    rows = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            t = " ".join(cell.text.split())
            if not cells or cells[-1] != t:  # merged cells repeat their text
                cells.append(t)
        line = " | ".join(c for c in cells if c)
        if line:
            rows.append(line)
    return rows


def extract_docx(path: Path) -> ExtractResult:
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head == OLE_MAGIC:
        return ExtractResult(skip_reason="encrypted")
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        d = docx.Document(str(path))
    except Exception as e:
        raise ExtractError(f"Cannot open DOCX ({e.__class__.__name__})") from None

    blocks: list[str] = []
    seen_headers: set[str] = set()
    for section in d.sections:
        for part in (section.header, section.first_page_header):
            try:
                t = "\n".join(p.text for p in part.paragraphs if p.text.strip())
            except Exception:
                t = ""
            if t and t not in seen_headers:
                seen_headers.add(t)
                blocks.append(t)

    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = Paragraph(child, d).text
            if t.strip():
                blocks.append(t)
        elif tag == "tbl":
            rows = _table_rows(Table(child, d))
            if rows:
                blocks.append("\n".join(rows))

    for section in d.sections:
        try:
            t = "\n".join(p.text for p in section.footer.paragraphs if p.text.strip())
        except Exception:
            t = ""
        if t and t not in seen_headers:
            seen_headers.add(t)
            blocks.append(t)

    text = "\n\n".join(blocks)
    return ExtractResult(segments=[Segment(None, text, "text")] if text.strip() else [], page_count=None)

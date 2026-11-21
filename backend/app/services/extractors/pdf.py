"""PDF: PyMuPDF text per page; pages with < 20 chars are rasterized at 200 DPI and OCR'd."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.services.extractors import ExtractError, ExtractResult, Segment

if TYPE_CHECKING:
    from app.services.extractors.image_ocr import OcrEngine

MIN_PAGE_CHARS = 20
OCR_DPI = 200

# Some payroll software (seen: German "Abrechnung der Brutto/Netto-Bezüge") prints amounts in a monospace font and
# draws the decimal comma as a tiny line between two digits, so the text layer reads "11.95814" for 11.958,14 and
# the chat repeated the wrong amount. A mark is put back as a comma when it is the only one inside a number, sits on
# the baseline and has exactly two digits (the cents) after it. Date boxes with a tick between every pair of
# digits ("10|05|05") have more than one mark and are left alone.
MARK_MAX_WIDTH = 1.2
MARK_HEIGHT = (1.0, 4.0)
_HAS_NUMBER = re.compile(r"\d{3,}")


def _mark_after(marks: list[Any], ch: dict[str, Any]) -> bool:
    edge, base = ch["bbox"][2], ch["origin"][1]
    return any(abs(m.x0 - edge) <= 1.5 and base - 3 <= m.y0 <= base + 0.5 and m.y1 >= base + 0.8 for m in marks)


def drawn_comma_fixes(page: Any) -> dict[str, str]:
    """{"11.95814": "11.958,14", ...} for numbers on this page whose decimal comma is drawn, not written."""
    try:
        marks = [d["rect"] for d in page.get_drawings()
                 if d["rect"].width <= MARK_MAX_WIDTH and MARK_HEIGHT[0] <= d["rect"].height <= MARK_HEIGHT[1]]
    except Exception:  # pragma: no cover - a damaged drawing list must not stop the text
        return {}
    if not marks:
        return {}
    fixes: dict[str, str] = {}
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                chars = span["chars"]
                i = 0
                while i < len(chars):
                    if not chars[i]["c"].isdigit():
                        i += 1
                        continue
                    j = i
                    while j < len(chars) and (chars[j]["c"].isdigit() or chars[j]["c"] in ".,"):
                        j += 1
                    token = chars[i:j]
                    word = "".join(c["c"] for c in token)
                    i = j
                    if "," in word:
                        continue
                    cuts = [k for k in range(len(token) - 1)
                            if token[k]["c"].isdigit() and token[k + 1]["c"].isdigit() and _mark_after(marks, token[k])]
                    if len(cuts) == 1 and len(word) - cuts[0] - 1 == 2:
                        k = cuts[0]
                        fixes[word] = f"{word[:k + 1]},{word[k + 1:]}"
    return fixes


def restore_drawn_commas(page: Any, text: str, fixes: dict[str, str] | None = None) -> str:
    if not _HAS_NUMBER.search(text):
        return text
    for bad, good in (drawn_comma_fixes(page) if fixes is None else fixes).items():
        text = re.sub(rf"(?<![\d.,]){re.escape(bad)}(?![\d.,])", good, text)
    return text


# Forms such as payslips and invoices print a row of column labels and the amounts in rows below them, so the plain
# text has "... Gesamtkosten Auszahlungsbetrag" on one line and "... 104,12 982,90" on the next: a small model then
# cannot tell which amount is the payout (seen: it answered the net pay). Each amount without a label on its own
# line gets the nearest label above it in the same column, written right before it: "Auszahlungsbetrag 982,90".
# (Not as a list after the page: that became a passage of its own without the month, and the chat took another
# month's amounts.)
_AMOUNT = re.compile(r"^-?\d{1,3}(?:[.,]\d{3})*[.,]\d{2}-?$")
_HAS_AMOUNT = re.compile(r"\d[.,]\d{2}\b")
_LABEL = re.compile(r"[A-Za-zÄÖÜäöüß]{3,}")
LABEL_REACH = 60  # points: how far above an amount its column label may be, and how far left an inline label


def column_labels(page: Any, fixes: dict[str, str] | None = None) -> list[tuple[str | None, str]]:
    """(label, amount) for every amount in reading order; label None when it is labelled on its own line or no
    column label is found (those only keep label_amounts in step with the text)."""
    fixes = fixes or {}
    words = [(w[0], w[1], w[2], w[3], fixes.get(w[4], w[4])) for w in page.get_text("words")]
    rows: list[tuple[float, list[tuple[float, float, float, float, str]]]] = []
    for w in sorted(words, key=lambda w: (w[1] + w[3]) / 2):  # words in rows by their vertical centre
        centre = (w[1] + w[3]) / 2
        if rows and abs(rows[-1][0] - centre) <= 3:
            rows[-1][1].append(w)
        else:
            rows.append((centre, [w]))
    pairs: list[tuple[str | None, str]] = []  # reading order: top to bottom, left to right
    for i, (centre, row) in enumerate(rows):
        for v in sorted(row, key=lambda w: w[0]):
            if not _AMOUNT.match(v[4]):
                continue
            label = None
            if not any(_LABEL.search(w[4]) and w[2] <= v[0] and v[0] - w[2] < LABEL_REACH for w in row):
                for above_centre, above in reversed(rows[:i]):
                    if centre - above_centre > LABEL_REACH:
                        break
                    hit = sorted((w for w in above
                                  if _LABEL.search(w[4]) and min(v[2], w[2]) - max(v[0], w[0]) > 0), key=lambda w: w[0])
                    if hit:
                        label = " ".join(w[4] for w in hit).rstrip(";:,.")
                        break
            pairs.append((label, v[4]))
    return pairs


def label_amounts(text: str, pairs: list[tuple[str | None, str]]) -> str:
    """Write each column label right before its amount ("Auszahlungsbetrag 982,90", like the labelled amounts on
    the rest of such a page), walking the text in reading order."""
    pos = 0
    for label, value in pairs:
        pattern = re.compile(rf"(?<![\d.,]){re.escape(value)}(?![\d.,])")
        m = pattern.search(text, pos) or (pattern.search(text) if label else None)
        if not m:
            continue
        if label is None:  # labelled on its own line: only move past it
            pos = m.end()
            continue
        note = f"{label} "
        text = text[: m.start()] + note + text[m.start():]
        pos = m.end() + len(note)
    return text


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
            raw = page.get_text("text", sort=True) or ""
            fixes = drawn_comma_fixes(page) if _HAS_NUMBER.search(raw) else {}
            text = restore_drawn_commas(page, raw, fixes)
            if _HAS_AMOUNT.search(text):
                text = label_amounts(text, column_labels(page, fixes))
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

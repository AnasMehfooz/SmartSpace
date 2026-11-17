import pytest

from app.services.extractors import ExtractError, extract, kind_for_ext
from app.services.extractors.image_ocr import OcrEngine
from tests.fixtures.make_fixtures import _docx, _pdf, _pptx, _scanned_pdf, _text_image, DOCX, PPTX


def test_kind_for_ext():
    assert kind_for_ext("PDF") == "document" and kind_for_ext("png") == "image"
    assert kind_for_ext("mp3") is None and kind_for_ext("exe") is None


def test_pdf_text_with_pages(tmp_path):
    p = tmp_path / "a.pdf"
    _pdf(p, "Seite eins: Mietvertrag über 950 Euro.\n\n" + "Absatz. " * 600)
    r = extract(p, "pdf", max_pdf_pages=500, ocr=None)
    assert r.page_count >= 2 and r.segments[0].page == 1
    assert "Mietvertrag über 950 Euro" in r.segments[0].text


def test_pdf_page_limit(tmp_path):
    p = tmp_path / "a.pdf"
    _pdf(p, "\n\n".join("Absatz " * 100 for _ in range(20)))
    r = extract(p, "pdf", max_pdf_pages=1, ocr=None)
    assert len(r.segments) == 1 and r.page_count > 1


def test_encrypted_pdf_is_skipped(tmp_path):
    import pymupdf

    p = tmp_path / "secret.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "top secret")
    doc.save(str(p), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    doc.close()
    assert extract(p, "pdf", max_pdf_pages=10, ocr=None).skip_reason == "encrypted"


def test_corrupt_pdf_raises(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(ExtractError):
        extract(p, "pdf", max_pdf_pages=10, ocr=None)


def test_docx_paragraphs_and_table_rows(tmp_path):
    p = tmp_path / "m.docx"
    _docx(p, DOCX["Work/Meeting_Notes_Kickoff.docx"])
    text = extract(p, "docx", max_pdf_pages=10, ocr=None).segments[0].text
    assert "Kickoff Meeting Notes" in text
    assert "Implement the search API | Developer B | Week 4" in text


def test_pptx_slides_are_pages_and_notes_included(tmp_path):
    p = tmp_path / "s.pptx"
    _pptx(p, PPTX["University/Machine_Learning_Intro.pptx"])
    r = extract(p, "pptx", max_pdf_pages=10, ocr=None)
    assert r.page_count == 3 and [s.page for s in r.segments] == [1, 2, 3]
    assert "bias-variance" in r.segments[2].text  # speaker notes


def test_text_encodings(tmp_path):
    p = tmp_path / "latin1.txt"
    p.write_bytes("Größe: 58 m² – Kündigung".encode("cp1252"))
    assert "Größe" in extract(p, "txt", max_pdf_pages=1, ocr=None).segments[0].text
    e = tmp_path / "empty.md"
    e.write_bytes(b"  \n ")
    assert extract(e, "md", max_pdf_pages=1, ocr=None).skip_reason == "empty"


@pytest.fixture(scope="module")
def ocr():
    return OcrEngine()


def test_image_ocr_and_no_text(tmp_path, ocr):
    img = tmp_path / "receipt.png"
    _text_image(["COFFEE CORNER", "TOTAL 7.90 EUR"]).save(img)
    r = extract(img, "png", max_pdf_pages=1, ocr=ocr)
    assert r.segments[0].source == "ocr" and "COFFEE" in r.segments[0].text
    blank = tmp_path / "blank.png"
    _text_image([]).save(blank)
    assert extract(blank, "png", max_pdf_pages=1, ocr=ocr).skip_reason == "no_text"


def test_scanned_pdf_uses_ocr(tmp_path, ocr):
    p = tmp_path / "scan.pdf"
    _scanned_pdf(p, ["Haftpflichtversicherung", "Versicherungsnummer 4711-2024"])
    r = extract(p, "pdf", max_pdf_pages=5, ocr=ocr)
    assert r.segments[0].source == "ocr" and "4711-2024" in r.segments[0].text

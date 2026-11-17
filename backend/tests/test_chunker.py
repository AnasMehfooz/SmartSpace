from app.services.chunker import chunk_segments
from app.services.extractors import Segment


def _words(n: int, word: str = "lorem") -> str:
    return " ".join(f"{word}{i}" for i in range(n))


def test_short_text_is_one_chunk():
    chunks = chunk_segments([Segment(1, "Hallo Welt. Kurzer Text.")])
    assert len(chunks) == 1
    assert chunks[0].text == "Hallo Welt. Kurzer Text."
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 1)


def test_chunks_respect_target_and_never_cut_words():
    text = "\n\n".join(_words(60) + "." for _ in range(20))  # ~20 paragraphs of ~500 chars
    chunks = chunk_segments([Segment(None, text)], target=1600, overlap=240)
    assert len(chunks) > 3
    vocab = set(text.split())
    for c in chunks:
        assert len(c.text) <= 1600 + 2
        for w in c.text.split():
            assert w in vocab, f"cut word: {w}"


def test_overlap_repeats_tail_of_previous_chunk():
    sentences = " ".join(f"Satz Nummer {i} endet hier." for i in range(400))
    chunks = chunk_segments([Segment(None, sentences)], target=600, overlap=120)
    assert len(chunks) > 2
    for a, b in zip(chunks, chunks[1:]):
        head = b.text[:40]
        assert head in a.text, "next chunk should start with text from the end of the previous one"


def test_german_umlauts_and_eszett_survive():
    text = "Die Kündigungsfrist beträgt drei Monate. Größe der Wohnung: 58 m². Straße: Lindenstraße 12."
    chunks = chunk_segments([Segment(2, text)])
    assert "Kündigungsfrist" in chunks[0].text and "Lindenstraße" in chunks[0].text


def test_pages_are_tracked_across_segments():
    segs = [Segment(p, _words(150, f"p{p}w")) for p in range(1, 6)]  # ~900 chars per page
    chunks = chunk_segments(segs, target=1600, overlap=200)
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 5
    assert all(c.page_start <= c.page_end for c in chunks)
    assert any(c.page_start != c.page_end for c in chunks), "a chunk should span a page break"


def test_ocr_source_is_kept_and_empty_input_gives_nothing():
    chunks = chunk_segments([Segment(None, "COFFEE CORNER\nTOTAL 7.90 EUR", "ocr")])
    assert chunks[0].source == "ocr"
    assert chunk_segments([Segment(1, "   \n\n  ")]) == []


def test_giant_word_is_hard_split():
    chunks = chunk_segments([Segment(None, "x" * 5000)], target=1600, overlap=240)
    assert all(len(c.text) <= 1600 for c in chunks)
    assert sum(len(c.text) for c in chunks) >= 5000

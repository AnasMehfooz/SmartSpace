"""German/English language detection on the first 2,000 chars (lingua, de/en only)."""

from __future__ import annotations

import re
import threading

_detector = None
_lock = threading.Lock()

_DE = {"der", "die", "das", "und", "ist", "nicht", "mit", "ein", "eine", "für", "auf", "den", "dem", "von",
       "zu", "sie", "wir", "ich", "sich", "des", "im", "auch", "wird", "werden", "bei", "oder", "über"}
_EN = {"the", "and", "is", "not", "with", "a", "an", "for", "on", "of", "to", "you", "we", "i", "it",
       "this", "that", "are", "was", "be", "by", "or", "from", "at", "as", "have", "will"}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _heuristic(text: str) -> str | None:
    words = [w.lower() for w in _WORD.findall(text)]
    de = sum(w in _DE for w in words)
    en = sum(w in _EN for w in words)
    if de == en == 0:
        return None
    return "de" if de > en else "en"


def detect_language(text: str) -> str | None:
    """Returns 'de', 'en' or None (unknown / too little text)."""
    sample = (text or "")[:2000]
    if len(sample.strip()) < 20:
        return None
    global _detector
    try:
        from lingua import Language, LanguageDetectorBuilder

        with _lock:
            if _detector is None:
                _detector = (
                    LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.GERMAN)
                    .with_minimum_relative_distance(0.05)
                    .build()
                )
        lang = _detector.detect_language_of(sample)
        if lang is None:
            return _heuristic(sample)
        return {"ENGLISH": "en", "GERMAN": "de"}.get(lang.name)
    except Exception:
        return _heuristic(sample)

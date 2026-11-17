"""Images: Pillow load (max side 3000 px) then RapidOCR (ONNX, models bundled in the wheel)."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from app.logging_setup import get_logger
from app.services.extractors import ExtractResult, Segment

log = get_logger("ocr")

MAX_SIDE = 3000
MIN_OCR_CHARS = 15


class OcrEngine:
    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()
        self.error: str | None = None

    def _get(self):
        if self._engine is None:
            import psutil
            from rapidocr_onnxruntime import RapidOCR

            # cores/2 threads: ~2x faster than the default (all logical cores incl. E-cores) and keeps the UI responsive
            threads = max(1, (psutil.cpu_count(logical=False) or 2) // 2)
            self._engine = RapidOCR(intra_op_num_threads=threads, inter_op_num_threads=1)
        return self._engine

    def ocr(self, img) -> str:
        """OCR a PIL image; returns lines top-to-bottom."""
        rgb = img.convert("RGB")
        if max(rgb.size) > MAX_SIDE:
            rgb.thumbnail((MAX_SIDE, MAX_SIDE))
        arr = np.asarray(rgb)[:, :, ::-1].copy()  # RGB -> BGR (OpenCV order)
        with self._lock:
            try:
                result, _ = self._get()(arr)
            except Exception as e:
                self.error = f"{e.__class__.__name__}"
                log.warning("ocr_failed", error=self.error)
                return ""
        if not result:
            return ""
        lines = []
        for item in result:
            if len(item) >= 3 and item[1] and (item[2] is None or float(item[2]) >= 0.5):
                lines.append(str(item[1]))
        return "\n".join(lines)


def extract_image(path: Path, ocr: OcrEngine) -> ExtractResult:
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if max(im.size) > MAX_SIDE:
            im.thumbnail((MAX_SIDE, MAX_SIDE))
        text = ocr.ocr(im)
    if len(text.strip()) < MIN_OCR_CHARS:
        return ExtractResult(skip_reason="no_text")
    return ExtractResult(segments=[Segment(None, text, "ocr")], page_count=None)

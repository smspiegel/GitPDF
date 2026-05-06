"""OCR dispatch: PaddleOCR for handwriting/poor scans, Tesseract for typed scans.

Both backends return the same shape: list[(text, x0, y0, x1, y1)] in pixel
coordinates of the input PIL image, with top-left origin.

PaddleOCR is the default when installed (better on degraded text). Tesseract
is the fallback. The choice is dynamic so the app still runs if PaddleOCR
isn't installed.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Protocol

from PIL import Image

log = logging.getLogger(__name__)

OcrWord = tuple[str, float, float, float, float]


class OcrBackend(Protocol):
    def recognize(self, image: Image.Image) -> list[OcrWord]: ...


_FORCE_BACKEND = os.environ.get("GITPDF_OCR")  # "paddle" | "tesseract" | None


@lru_cache(maxsize=1)
def _get_backend() -> OcrBackend:
    if _FORCE_BACKEND == "tesseract":
        return _TesseractBackend()
    if _FORCE_BACKEND == "paddle":
        return _PaddleBackend()
    try:
        return _PaddleBackend()
    except Exception as exc:  # noqa: BLE001
        log.info("PaddleOCR unavailable (%s); using Tesseract.", exc)
        return _TesseractBackend()


def ocr_page_image(image: Image.Image) -> list[OcrWord]:
    return _get_backend().recognize(image)


class _PaddleBackend:
    def __init__(self) -> None:
        from paddleocr import PaddleOCR  # type: ignore

        self._ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)

    def recognize(self, image: Image.Image) -> list[OcrWord]:
        import numpy as np

        arr = np.array(image.convert("RGB"))
        result = self._ocr.ocr(arr, cls=True)
        # result: [[ [box, (text, conf)], ... ]] for one page input
        words: list[OcrWord] = []
        if not result or not result[0]:
            return words
        for line in result[0]:
            box, (text, _conf) = line
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            # PaddleOCR returns line-level boxes. Split on whitespace so each
            # word gets its own bbox by linear interpolation along the box width.
            tokens = text.split()
            if not tokens:
                continue
            x0, x1 = float(min(xs)), float(max(xs))
            y0, y1 = float(min(ys)), float(max(ys))
            total_chars = sum(len(t) for t in tokens) + max(len(tokens) - 1, 0)
            cursor = 0
            for tok in tokens:
                start_frac = cursor / total_chars if total_chars else 0
                end_frac = (cursor + len(tok)) / total_chars if total_chars else 1
                wx0 = x0 + (x1 - x0) * start_frac
                wx1 = x0 + (x1 - x0) * end_frac
                words.append((tok, wx0, y0, wx1, y1))
                cursor += len(tok) + 1
        return words


class _TesseractBackend:
    def __init__(self) -> None:
        import pytesseract  # noqa: F401  -- import-time check

    def recognize(self, image: Image.Image) -> list[OcrWord]:
        import pytesseract
        from pytesseract import Output

        data = pytesseract.image_to_data(image, output_type=Output.DICT)
        words: list[OcrWord] = []
        n = len(data["text"])
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf = float(data["conf"][i])
            except (TypeError, ValueError):
                conf = -1.0
            if conf >= 0 and conf < 30:
                continue
            x = float(data["left"][i])
            y = float(data["top"][i])
            w = float(data["width"][i])
            h = float(data["height"][i])
            words.append((text, x, y, x + w, y + h))
        return words

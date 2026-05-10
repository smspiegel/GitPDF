"""PDF -> ExtractedDoc.

Coordinate convention: top-left origin, units in PDF points (1/72 inch).
This matches what PDF.js's `viewport.convertToViewportPoint` expects after
the standard top-down transform.
"""
from __future__ import annotations

from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium

from .models import BBox, ExtractedDoc, Token
from .ocr import ocr_page_image


# A page is treated as scanned if the embedded text layer holds fewer than
# this many word-like tokens. Tuned conservatively: many real-world PDFs
# include a few stamp characters even when the body is a scan.
TEXT_LAYER_MIN_WORDS = 8


def extract_document(pdf_path: str | Path, ocr_dpi: int = 300) -> ExtractedDoc:
    pdf_path = Path(pdf_path)
    tokens: list[Token] = []
    page_sizes: list[tuple[float, float]] = []
    used_ocr: list[bool] = []
    next_index = 0

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            page_sizes.append((float(page.width), float(page.height)))
            # Two settings worth explaining:
            #
            # 1. `use_text_flow` is left at the default (False). Setting it
            #    True honors the PDF's internal text-stream verbatim, which
            #    for PDFs whose text streams omit space characters and rely
            #    on glyph positioning for visual spacing (some LaTeX/Word/
            #    Office exporters do this) produces one giant run-on token
            #    per line. The default uses spatial inference instead.
            #
            # 2. `x_tolerance` is dropped from the default 3.0 to 1.5. PDFs
            #    that don't emit space chars typically draw words ~2.5-3.0
            #    PDF points apart (right at the default threshold), so the
            #    inter-word gaps get rounded down into "same word." Real
            #    intra-word kerning is < 0.2 pt, so 1.5 is comfortably
            #    above kerning noise and well below any reasonable visual
            #    word boundary. PDFs that DO emit space chars are
            #    unaffected because the space glyph itself creates a gap
            #    much larger than 1.5.
            words = page.extract_words(
                extra_attrs=[],
                keep_blank_chars=False,
                x_tolerance=1.5,
            )
            if len(words) >= TEXT_LAYER_MIN_WORDS:
                used_ocr.append(False)
                for w in words:
                    tokens.append(
                        Token(
                            page=page_num,
                            bbox=BBox(
                                x0=float(w["x0"]),
                                y0=float(w["top"]),
                                x1=float(w["x1"]),
                                y1=float(w["bottom"]),
                            ),
                            text=str(w["text"]),
                            index=next_index,
                        )
                    )
                    next_index += 1
            else:
                used_ocr.append(True)
                next_index = _ocr_page(
                    pdf_path, page_num, ocr_dpi, page.width, page.height,
                    tokens, next_index,
                )

    return ExtractedDoc(
        page_count=page_count,
        page_sizes=page_sizes,
        tokens=tokens,
        used_ocr=used_ocr,
    )


def _ocr_page(
    pdf_path: Path,
    page_num: int,
    dpi: int,
    pdf_width_pt: float,
    pdf_height_pt: float,
    tokens_out: list[Token],
    next_index: int,
) -> int:
    """Rasterize one page with pypdfium2 and OCR it; append tokens."""
    doc = pdfium.PdfDocument(pdf_path)
    try:
        page = doc[page_num - 1]
        scale = dpi / 72.0
        pil_image = page.render(scale=scale).to_pil()
        page.close()
        words = ocr_page_image(pil_image)
        # words: list[(text, x0_px, y0_px, x1_px, y1_px)]
        for text, x0, y0, x1, y1 in words:
            tokens_out.append(
                Token(
                    page=page_num,
                    bbox=BBox(
                        x0=x0 / scale,
                        y0=y0 / scale,
                        x1=x1 / scale,
                        y1=y1 / scale,
                    ),
                    text=text,
                    index=next_index,
                )
            )
            next_index += 1
        return next_index
    finally:
        doc.close()

"""Regression tests for the PDF text extractor.

These don't open real PDFs (would need binary fixtures and slow
extraction). Instead they call pdfplumber's pure word-extraction routine
directly on synthetic char dicts that mimic the layouts seen in real
PDFs we know broke historically. That's enough to pin the
`x_tolerance` choice in `extract.py` without bundling fixture binaries.
"""
from __future__ import annotations

import pdfplumber.utils as pdfu


def _char(text: str, x0: float, x1: float, y_top: float = 100.0, h: float = 12.0) -> dict:
    """Build a pdfplumber-shaped char dict.

    pdfplumber's `extract_words` reads x0/x1/top/bottom/text and a few
    metadata fields; missing optional fields default sanely.
    """
    return {
        "text": text,
        "x0": x0, "x1": x1,
        "top": y_top, "bottom": y_top + h,
        "y0": y_top, "y1": y_top + h,
        "doctop": y_top, "matrix": (1, 0, 0, 1, 0, 0),
        "fontname": "F1", "size": h,
        "upright": True, "height": h, "width": x1 - x0,
        "object_type": "char", "page_number": 1,
    }


def _line_no_spaces(words: list[str], char_w: float = 6.0, gap: float = 2.7) -> list[dict]:
    """Lay out a line of words with NO space characters between them.

    Word-boundary glyphs are separated by a small horizontal gap (default
    2.7 PDF points -- the value seen in the real PDF that triggered the
    bug). Within a word, consecutive glyphs touch (gap = 0). This is the
    exact layout pdfplumber's default `x_tolerance=3` glues into one
    run-on token.
    """
    chars: list[dict] = []
    x = 50.0
    for w_idx, word in enumerate(words):
        if w_idx > 0:
            x += gap  # inter-word visual gap, no space char emitted
        for c in word:
            chars.append(_char(c, x, x + char_w))
            x += char_w
    return chars


# Settings used by `gitpdf.extract.extract_document`. Keep in sync.
EXTRACT_KWARGS = dict(extra_attrs=[], keep_blank_chars=False, x_tolerance=1.5)


def test_extract_words_splits_no_space_pdf_layout():
    """Words separated by ~2.7-pt visual gaps (no space chars) must split.

    Regression for the resume PDF where pdfplumber's default tolerance
    glued every line into a single 'Fouryearsofexperience...' token.
    """
    chars = _line_no_spaces(["Four", "years", "of", "experience"])
    words = pdfu.extract_words(chars, **EXTRACT_KWARGS)
    texts = [w["text"] for w in words]
    assert texts == ["Four", "years", "of", "experience"], (
        f"expected per-word split, got {texts}"
    )


def test_extract_words_default_tolerance_demonstrates_bug():
    """Confirms the bug exists at the pdfplumber default. If pdfplumber's
    default ever changes such that this no longer reproduces, our explicit
    x_tolerance=1.5 in extract.py becomes unnecessary -- worth knowing."""
    chars = _line_no_spaces(["Four", "years", "of", "experience"])
    words = pdfu.extract_words(chars, extra_attrs=[], keep_blank_chars=False)
    texts = [w["text"] for w in words]
    assert texts == ["Fouryearsofexperience"], (
        f"pdfplumber default no longer glues no-space words; "
        f"got {texts}. Consider whether the explicit x_tolerance=1.5 in "
        f"extract.py is still needed."
    )


def test_extract_words_preserves_intra_word_kerning():
    """Tight kerning within a word (sub-pt gaps) must NOT split words.

    Real PDFs have 0.0-0.2 pt kerning between intra-word glyphs. Our
    1.5 pt tolerance is well above that, but pin the behavior so a
    future tighter tolerance doesn't shred normal text into letters.
    """
    chars: list[dict] = []
    x = 50.0
    char_w = 6.0
    for i, c in enumerate("Spiegelman"):
        # tiny ±0.1 jitter, like real kerning
        if i > 0:
            x += 0.1 if i % 2 == 0 else -0.05
        chars.append(_char(c, x, x + char_w))
        x += char_w
    words = pdfu.extract_words(chars, **EXTRACT_KWARGS)
    texts = [w["text"] for w in words]
    assert texts == ["Spiegelman"], f"intra-word kerning was over-split: {texts}"


def test_extract_words_with_real_space_chars_still_works():
    """PDFs that DO emit space chars must keep working at our tighter
    tolerance -- the space glyph itself creates a wide gap, so x_tolerance
    has no effect on these."""
    chars: list[dict] = []
    x = 50.0
    char_w = 6.0
    for word_idx, w in enumerate(["hello", "world"]):
        for c in w:
            chars.append(_char(c, x, x + char_w))
            x += char_w
        if word_idx == 0:
            chars.append(_char(" ", x, x + char_w))
            x += char_w
    words = pdfu.extract_words(chars, **EXTRACT_KWARGS)
    texts = [w["text"] for w in words]
    assert texts == ["hello", "world"], f"space-separated layout broken: {texts}"

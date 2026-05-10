"""Diagnostic: print extracted tokens from two PDFs and report mismatches.

Used when the diff engine flags two visually-similar PDFs as wildly
different. If the token texts on each side don't actually match (because
of font/ligature/encoding differences in the PDF), no diff algorithm
can paper over that -- the problem is upstream in extraction.

Usage:
    python scripts/diag_extract.py path/to/A.pdf path/to/B.pdf
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

# Add src/ to path so we can import the package without an install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gitpdf.extract import extract_document  # noqa: E402


def dump(label: str, pdf_path: str, n_preview: int = 30) -> Counter:
    print(f"\n=== {label}: {pdf_path} ===")
    doc = extract_document(pdf_path)
    print(f"pages: {doc.page_count}  tokens: {len(doc.tokens)}  "
          f"used_ocr: {doc.used_ocr}")
    print(f"first {n_preview} tokens:")
    for i, t in enumerate(doc.tokens[:n_preview]):
        # repr() shows hidden chars (­ soft hyphen, ﻿ BOM, etc.)
        print(f"  [{i:3d}] page={t.page} {repr(t.text)}")
    return Counter(t.text for t in doc.tokens)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a_words = dump("A", sys.argv[1])
    b_words = dump("B", sys.argv[2])

    only_a = a_words - b_words
    only_b = b_words - a_words
    common = a_words & b_words

    print("\n=== token-set comparison ===")
    print(f"unique tokens A: {len(a_words)}")
    print(f"unique tokens B: {len(b_words)}")
    print(f"shared (intersection): {len(common)}  "
          f"  (= {100 * len(common) / max(len(a_words | b_words), 1):.1f}% of union)")
    print(f"\ntop 20 tokens present ONLY in A:")
    for tok, n in only_a.most_common(20):
        print(f"  {n:4d} x {repr(tok)}")
    print(f"\ntop 20 tokens present ONLY in B:")
    for tok, n in only_b.most_common(20):
        print(f"  {n:4d} x {repr(tok)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

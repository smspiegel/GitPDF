"""Pure-Python tests for the diff engine.

Tokens are synthesized directly so we don't depend on PDF rendering or OCR.
Each token has a unique index, a 1-indexed page, and a bbox we fabricate
to mimic single-line layout (one line per word, monotonically increasing y).
"""
from __future__ import annotations

import pytest

from gitpdf.diff_engine import (
    align_blocks,
    choose_mode,
    compute_diff,
    detect_moves,
    score_similarity,
    segment_blocks,
)
from gitpdf.models import BBox, DiffKind, Token


def make_tokens(words: list[str], page: int = 1, line_h: float = 12.0) -> list[Token]:
    out: list[Token] = []
    y = 10.0
    for i, w in enumerate(words):
        out.append(
            Token(
                page=page,
                bbox=BBox(x0=10, y0=y, x1=10 + 6 * len(w), y1=y + line_h - 2),
                text=w,
                index=i,
            )
        )
        y += line_h  # one word per line so segmentation is straightforward
    return out


def make_paragraph_tokens(paragraphs: list[list[str]], page: int = 1) -> list[Token]:
    """Lay paragraphs out as rows of words, with a big vertical gap between paragraphs."""
    out: list[Token] = []
    idx = 0
    y = 10.0
    line_h = 12.0
    para_gap = line_h * 3  # > 1.6x median triggers a block break
    for para in paragraphs:
        x = 10.0
        for w in para:
            out.append(
                Token(
                    page=page,
                    bbox=BBox(x0=x, y0=y, x1=x + 6 * len(w), y1=y + line_h - 2),
                    text=w,
                    index=idx,
                )
            )
            x += 6 * len(w) + 4
            idx += 1
        y += para_gap
    return out


# ------ segmentation ------

def test_segment_empty():
    assert segment_blocks([]) == []


def test_segment_groups_paragraphs():
    tokens = make_paragraph_tokens(
        [["hello", "world"], ["foo", "bar", "baz"]]
    )
    blocks = segment_blocks(tokens)
    assert len(blocks) == 2
    assert "hello world" in blocks[0].text
    assert "foo bar baz" in blocks[1].text


# ------ similarity ------

def test_similarity_identical():
    a = make_paragraph_tokens([["the", "quick", "brown", "fox"]])
    b = make_paragraph_tokens([["the", "quick", "brown", "fox"]])
    assert score_similarity(segment_blocks(a), segment_blocks(b)) == pytest.approx(1.0)


def test_similarity_disjoint():
    a = make_paragraph_tokens([["alpha", "beta", "gamma"]])
    b = make_paragraph_tokens([["red", "green", "blue"]])
    assert score_similarity(segment_blocks(a), segment_blocks(b)) < 0.5


def test_choose_mode_threshold():
    assert choose_mode("auto", 0.71) == "git-style"
    assert choose_mode("auto", 0.69) == "diff-only"
    assert choose_mode("git-style", 0.0) == "git-style"
    assert choose_mode("diff-only", 1.0) == "diff-only"


# ------ alignment ------

def test_align_one_to_one():
    a = segment_blocks(make_paragraph_tokens([["hello", "world"], ["foo", "bar"]]))
    b = segment_blocks(make_paragraph_tokens([["foo", "bar"], ["hello", "world"]]))
    pairs = align_blocks(a, b)
    matched = [(p.a, p.b) for p in pairs if p.a is not None and p.b is not None]
    # Each block should be paired exactly once.
    assert len(matched) == 2
    assert {p[0] for p in matched} == {0, 1}
    assert {p[1] for p in matched} == {0, 1}


def test_align_addition():
    a = segment_blocks(make_paragraph_tokens([["hello", "world"]]))
    b = segment_blocks(make_paragraph_tokens([["hello", "world"], ["new", "paragraph"]]))
    pairs = align_blocks(a, b)
    unmatched_b = [p for p in pairs if p.a is None and p.b is not None]
    assert len(unmatched_b) == 1


def test_align_removal():
    a = segment_blocks(make_paragraph_tokens([["hello", "world"], ["gone", "now"]]))
    b = segment_blocks(make_paragraph_tokens([["hello", "world"]]))
    pairs = align_blocks(a, b)
    unmatched_a = [p for p in pairs if p.a is not None and p.b is None]
    assert len(unmatched_a) == 1


# ------ move detection ------

def test_detect_moves_swap():
    # A: [b0=foo, b1=bar]; B: [b0=bar, b1=foo]
    # Matched pairs: (0,1) and (1,0). LIS length = 1, so one of them is moved.
    from gitpdf.diff_engine import Pair
    pairs = [Pair(a=0, b=1, similarity=1.0), Pair(a=1, b=0, similarity=1.0)]
    moved = detect_moves(pairs)
    assert len(moved) == 1


def test_detect_moves_no_move():
    from gitpdf.diff_engine import Pair
    pairs = [Pair(a=0, b=0, similarity=1.0), Pair(a=1, b=1, similarity=1.0)]
    assert detect_moves(pairs) == set()


# ------ end-to-end compute_diff ------

def test_compute_diff_identical_no_overlays():
    tokens_a = make_paragraph_tokens([["one", "two", "three"]])
    tokens_b = make_paragraph_tokens([["one", "two", "three"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="auto")
    assert result.similarity == pytest.approx(1.0)
    assert result.mode_used == "git-style"
    assert result.overlays == []


def test_compute_diff_word_replacement():
    tokens_a = make_paragraph_tokens([["the", "quick", "brown", "fox"]])
    tokens_b = make_paragraph_tokens([["the", "slow", "brown", "fox"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="git-style")
    kinds = {o.kind for o in result.overlays}
    assert DiffKind.REMOVED in kinds
    assert DiffKind.ADDED in kinds


def test_compute_diff_reorder_emits_moved():
    tokens_a = make_paragraph_tokens(
        [["alpha", "alpha", "alpha"], ["beta", "beta", "beta"]]
    )
    tokens_b = make_paragraph_tokens(
        [["beta", "beta", "beta"], ["alpha", "alpha", "alpha"]]
    )
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="auto")
    moved = [o for o in result.overlays if o.kind == DiffKind.MOVED]
    sides = {o.side for o in moved}
    assert sides == {"A", "B"}, f"expected moved overlays on both sides, got {sides}"


def test_compute_diff_diff_only_mode_collapses_kinds():
    tokens_a = make_paragraph_tokens([["the", "quick", "brown", "fox"]])
    tokens_b = make_paragraph_tokens([["the", "slow", "brown", "fox"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="diff-only")
    kinds = {o.kind for o in result.overlays}
    assert DiffKind.REMOVED not in kinds
    assert DiffKind.ADDED not in kinds
    assert kinds == {DiffKind.MOVED}

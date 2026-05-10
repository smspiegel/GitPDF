"""Pure-Python tests for the diff engine.

Tokens are synthesized directly so we don't depend on PDF rendering or OCR.
Each token has a unique index, a 1-indexed page, and a bbox we fabricate
to mimic single-line layout (one line per word, monotonically increasing y).
"""
from __future__ import annotations

import pytest

from gitpdf.diff_engine import (
    choose_mode,
    compute_diff,
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


def test_compute_diff_replace_emits_single_replaced_row():
    """A `replace` opcode must produce ONE summary entry with kind=REPLACED
    that carries both the before (text_a) and after (text_b) text.

    The frontend renders this kind as a horizontally-split row: the red
    half on the left shows text_a (what was deleted), the green half on
    the right shows text_b (what replaced it). Earlier code emitted only
    REMOVED for replaces, so the green side was invisible in the panel.
    """
    tokens_a = make_paragraph_tokens([["the", "quick", "brown", "fox"]])
    tokens_b = make_paragraph_tokens([["the", "slow", "brown", "fox"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="git-style")

    replaced = [s for s in result.summary if s.kind == DiffKind.REPLACED]
    assert len(replaced) == 1, f"expected 1 REPLACED row, got {result.summary}"
    assert not [s for s in result.summary if s.kind == DiffKind.REMOVED]
    assert not [s for s in result.summary if s.kind == DiffKind.ADDED]
    # Both sides populated so the frontend can render the split snippet.
    assert replaced[0].text_a == "quick"
    assert replaced[0].text_b == "slow"


def test_compute_diff_replace_in_diff_only_mode_collapses_to_moved():
    """In diff-only mode there's no red/green distinction, so a REPLACED
    summary row collapses to MOVED (the neutral marker)."""
    tokens_a = make_paragraph_tokens([["the", "quick", "brown", "fox"]])
    tokens_b = make_paragraph_tokens([["the", "slow", "brown", "fox"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="diff-only")
    kinds = {s.kind for s in result.summary}
    assert DiffKind.REPLACED not in kinds
    assert kinds == {DiffKind.MOVED}


def test_compute_diff_pure_delete_one_removed_row():
    """A pure delete (no insert at the same position) yields exactly one
    REMOVED summary row with no ADDED partner."""
    tokens_a = make_paragraph_tokens([["alpha", "beta", "gamma"]])
    tokens_b = make_paragraph_tokens([["alpha", "gamma"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="git-style")
    removed = [s for s in result.summary if s.kind == DiffKind.REMOVED]
    added = [s for s in result.summary if s.kind == DiffKind.ADDED]
    assert len(removed) == 1 and len(added) == 0
    assert removed[0].text_a == "beta"


def test_compute_diff_pure_insert_one_added_row():
    """A pure insert yields exactly one ADDED summary row, no REMOVED."""
    tokens_a = make_paragraph_tokens([["alpha", "gamma"]])
    tokens_b = make_paragraph_tokens([["alpha", "beta", "gamma"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="git-style")
    removed = [s for s in result.summary if s.kind == DiffKind.REMOVED]
    added = [s for s in result.summary if s.kind == DiffKind.ADDED]
    assert len(removed) == 0 and len(added) == 1
    assert added[0].text_b == "beta"


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


# ------ per-line segmentation regression (resume/dense-spacing case) ------


def make_lines_tokens(
    lines: list[list[str]], page: int = 1, line_h: float = 12.0
) -> list[Token]:
    """Lay out one row of words per line with TIGHT spacing (no paragraph gap).

    This mirrors a resume or single-column report where every line is just
    one line-height apart from the next -- the layout that used to collapse
    into a single block under the old paragraph-gap heuristic.
    """
    out: list[Token] = []
    idx = 0
    y = 10.0
    for row in lines:
        x = 10.0
        for w in row:
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
        y += line_h  # tight: one line height between rows
    return out


def test_segment_one_block_per_visual_line_under_tight_spacing():
    # Five tightly-spaced lines must NOT collapse into one block.
    tokens = make_lines_tokens(
        [
            ["SPENCER", "SPIEGELMAN"],
            ["email|phone|loc"],
            ["SUMMARY", "OF", "QUALIFICATIONS"],
            ["Four", "years", "of", "experience"],
            ["Languages:", "Python"],
        ]
    )
    blocks = segment_blocks(tokens)
    assert len(blocks) == 5, f"expected per-line segmentation, got {len(blocks)} blocks"
    assert blocks[0].text == "SPENCER SPIEGELMAN"
    assert blocks[2].text == "SUMMARY OF QUALIFICATIONS"


def test_segment_groups_same_line_words_into_one_block():
    # All words on the same y are one block; a y advance starts a new one.
    tokens = make_lines_tokens([["alpha", "beta", "gamma"], ["delta"]])
    blocks = segment_blocks(tokens)
    assert len(blocks) == 2
    assert blocks[0].text == "alpha beta gamma"
    assert blocks[1].text == "delta"


def test_compute_diff_resume_like_only_changed_lines_highlighted():
    """The bug from the original report: near-identical resumes used to flag
    every line as added/removed because everything collapsed to one block.
    With per-line segmentation, only the diverging lines should be flagged.
    """
    shared = [
        ["SPENCER", "SPIEGELMAN"],
        ["SUMMARY", "OF", "QUALIFICATIONS"],
        ["Four", "years", "of", "experience"],
        ["EDUCATION"],
        ["University", "of", "Waterloo"],
    ]
    tokens_a = make_lines_tokens(shared)
    tokens_b = make_lines_tokens(shared + [["Dear", "Hitachi", "Energy", "Research", "Team"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="git-style")
    # Only the new line on B should produce overlays; nothing on A.
    sides = {o.side for o in result.overlays}
    assert sides == {"B"}, f"unchanged lines should not be flagged, got sides={sides}"
    added_text = " ".join(
        o.kind.value for o in result.overlays if o.side == "B"
    )
    assert "added" in added_text


# ------ similarity-cap regression ------


def test_similarity_capped_when_overlays_present():
    """Any overlay must prevent the reported similarity from rounding to 100%.

    The UI displays similarity via `(value * 100).toFixed(0)`, so anything
    >= 0.995 would round up to "100%" and lie to the user. Whenever
    compute_diff produces at least one overlay we cap at 0.99.
    """
    # Single-word change in an otherwise long shared body: token_set_ratio
    # comes back very high (often 100), but there is a real diff to show.
    shared = ["the", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog"] * 3
    tokens_a = make_paragraph_tokens([shared + ["one"]])
    tokens_b = make_paragraph_tokens([shared + ["two"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="git-style")
    assert result.overlays, "test setup invalid: expected at least one overlay"
    assert result.similarity <= 0.99, (
        f"similarity must be capped at 0.99 when overlays exist, "
        f"got {result.similarity}"
    )
    # And the displayed (rounded) percent must therefore be < 100.
    assert round(result.similarity * 100) < 100


def test_similarity_uncapped_when_no_overlays():
    """No overlays = nothing changed = honest 1.0 is allowed."""
    tokens_a = make_paragraph_tokens([["one", "two", "three"]])
    tokens_b = make_paragraph_tokens([["one", "two", "three"]])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="auto")
    assert result.overlays == []
    assert result.similarity == pytest.approx(1.0)


# ------ layout-blindness regressions (page-break / reflow) ------


def _stream_tokens(
    lines: list[tuple[int, list[str]]], line_h: float = 12.0
) -> list[Token]:
    """Build tokens where each tuple is (page_number, words_on_that_line).

    Lines on the same page advance y by `line_h`. Crossing to a new page
    resets y. Lets us simulate identical content laid out differently --
    e.g. all on one page on side B, but split across two pages on side A.
    """
    out: list[Token] = []
    idx = 0
    last_page: int | None = None
    y = 10.0
    for page, words in lines:
        if page != last_page:
            y = 10.0
            last_page = page
        x = 10.0
        for w in words:
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
        y += line_h
    return out


def test_compute_diff_page_break_same_content_no_overlays():
    """Regression: identical content laid out across different page breaks
    must NOT produce any add/remove flags. Earlier block-level alignment
    flagged the post-break tail of A as 'removed' because Hungarian could
    only pair one of A's two lines with B's single line.
    """
    # Side A: text split across a page break (same words, just wrapped).
    tokens_a = _stream_tokens([
        (1, ["The", "quick", "brown", "fox", "jumps"]),
        (2, ["over", "the", "lazy", "dog"]),
    ])
    # Side B: same words, all on page 1, broken into two visual lines.
    tokens_b = _stream_tokens([
        (1, ["The", "quick", "brown", "fox", "jumps"]),
        (1, ["over", "the", "lazy", "dog"]),
    ])
    result = compute_diff(tokens_a, tokens_b, 2, 1, mode="git-style")
    assert result.overlays == [], (
        f"identical content across different page layouts must produce no "
        f"diffs; got {len(result.overlays)} overlays: "
        f"{[(o.side, o.kind.value) for o in result.overlays]}"
    )


def test_compute_diff_line_wrap_difference_no_overlays():
    """Same paragraph, different line wrapping -- must not produce false diffs."""
    # Side A: wrapped at 5 words.
    tokens_a = _stream_tokens([
        (1, ["alpha", "beta", "gamma", "delta", "epsilon"]),
        (1, ["zeta", "eta", "theta"]),
    ])
    # Side B: same words on a single line.
    tokens_b = _stream_tokens([
        (1, ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]),
    ])
    result = compute_diff(tokens_a, tokens_b, 1, 1, mode="git-style")
    assert result.overlays == [], (
        f"identical content with different line-wrapping must produce no "
        f"diffs; got {[(o.side, o.kind.value) for o in result.overlays]}"
    )


def test_compute_diff_page_break_with_a_real_change_only_change_flagged():
    """Across-page reflow plus one real edit: only the real edit is flagged."""
    tokens_a = _stream_tokens([
        (1, ["The", "quick", "brown", "fox", "jumps"]),
        (2, ["over", "the", "lazy", "dog"]),
    ])
    tokens_b = _stream_tokens([
        (1, ["The", "quick", "brown", "fox", "jumps", "over", "the", "sleeping", "dog"]),
    ])
    result = compute_diff(tokens_a, tokens_b, 2, 1, mode="git-style")
    # The single word "lazy" -> "sleeping" should be the only flagged change.
    a_overlays = [o for o in result.overlays if o.side == "A"]
    b_overlays = [o for o in result.overlays if o.side == "B"]
    assert a_overlays, "expected the one real change to be flagged on A"
    assert b_overlays, "expected the one real change to be flagged on B"
    # The flagged A text should be just "lazy" (and similarly "sleeping" on B).
    a_texts = [s.text_a for s in result.summary if s.text_a]
    b_texts = [s.text_b for s in result.summary if s.text_b]
    assert any("lazy" in t for t in a_texts), f"A flagged text: {a_texts}"
    assert any("sleeping" in t for t in b_texts), f"B flagged text: {b_texts}"

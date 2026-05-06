"""Pure-Python diff engine: tokens in, overlays out. No I/O.

The engine is split into deterministic phases so each is unit-testable:
    segment_blocks  -> Block[]
    score_similarity -> float in [0,1]
    align_blocks    -> list[(a_idx|None, b_idx|None)]
    diff_pair       -> word-level spans within an aligned pair
    assemble        -> Overlay[] + SummaryEntry[]
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

import numpy as np
from rapidfuzz import fuzz
from scipy.optimize import linear_sum_assignment

from .models import (
    BBox,
    Block,
    DiffKind,
    DiffResult,
    Mode,
    Overlay,
    SummaryEntry,
    Token,
)


GIT_STYLE_THRESHOLD = 0.70
PARAGRAPH_GAP_FACTOR = 1.6  # vertical gap > median line height * this -> new block
MIN_BLOCK_CHARS = 2
CONTEXT_WORDS = 12  # words on each side of a diff span included in summary context


# -------- block segmentation --------

def segment_blocks(tokens: list[Token]) -> list[Block]:
    """Group tokens into paragraph-like blocks using a vertical-gap heuristic."""
    if not tokens:
        return []

    blocks: list[Block] = []
    current: list[Token] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(t.text for t in current).strip()
        if len(text) < MIN_BLOCK_CHARS:
            current.clear()
            return
        x0 = min(t.bbox.x0 for t in current)
        y0 = min(t.bbox.y0 for t in current)
        x1 = max(t.bbox.x1 for t in current)
        y1 = max(t.bbox.y1 for t in current)
        blocks.append(
            Block(
                id=len(blocks),
                page=current[0].page,
                bbox=BBox(x0=x0, y0=y0, x1=x1, y1=y1),
                text=text,
                token_indices=[t.index for t in current],
            )
        )
        current.clear()

    line_heights = [t.bbox.height for t in tokens if t.bbox.height > 0]
    median_h = float(np.median(line_heights)) if line_heights else 12.0
    gap_threshold = median_h * PARAGRAPH_GAP_FACTOR

    prev: Token | None = None
    for t in tokens:
        if prev is not None:
            page_change = t.page != prev.page
            # Tokens are in reading order; a "new line" begins when y advances.
            # Use vertical gap between bottoms; negative/zero means same line.
            vgap = t.bbox.y0 - prev.bbox.y1
            if page_change or vgap > gap_threshold:
                flush()
        current.append(t)
        prev = t
    flush()
    return blocks


# -------- similarity --------

def score_similarity(blocks_a: list[Block], blocks_b: list[Block]) -> float:
    """Global similarity in [0,1] over all block text."""
    text_a = " ".join(b.text for b in blocks_a)
    text_b = " ".join(b.text for b in blocks_b)
    if not text_a and not text_b:
        return 1.0
    if not text_a or not text_b:
        return 0.0
    return fuzz.token_set_ratio(text_a, text_b) / 100.0


def choose_mode(mode: Mode, similarity: float) -> str:
    if mode == "git-style":
        return "git-style"
    if mode == "diff-only":
        return "diff-only"
    return "git-style" if similarity >= GIT_STYLE_THRESHOLD else "diff-only"


# -------- block alignment --------

@dataclass
class Pair:
    a: int | None
    b: int | None
    similarity: float


def align_blocks(
    blocks_a: list[Block],
    blocks_b: list[Block],
    min_match: float = 0.50,
) -> list[Pair]:
    """Pair blocks across A and B using the Hungarian algorithm.

    Returns a list of pairs covering every block exactly once. Unpaired
    blocks have a=None or b=None. Pairs with similarity below `min_match`
    are split into separate unpaired entries (treated as removed/added).
    """
    if not blocks_a and not blocks_b:
        return []
    if not blocks_a:
        return [Pair(a=None, b=i, similarity=0.0) for i, _ in enumerate(blocks_b)]
    if not blocks_b:
        return [Pair(a=i, b=None, similarity=0.0) for i, _ in enumerate(blocks_a)]

    n_a, n_b = len(blocks_a), len(blocks_b)
    sim = np.zeros((n_a, n_b), dtype=float)
    for i, ba in enumerate(blocks_a):
        for j, bb in enumerate(blocks_b):
            sim[i, j] = fuzz.token_set_ratio(ba.text, bb.text) / 100.0

    # Hungarian on a square matrix; pad with zero-similarity dummies.
    n = max(n_a, n_b)
    cost = np.ones((n, n), dtype=float)  # cost = 1 - sim; dummies stay at 1.0
    cost[:n_a, :n_b] = 1.0 - sim
    row_idx, col_idx = linear_sum_assignment(cost)

    pairs: list[Pair] = []
    used_a, used_b = set(), set()
    for r, c in zip(row_idx, col_idx):
        if r >= n_a or c >= n_b:
            continue
        s = sim[r, c]
        if s < min_match:
            continue
        pairs.append(Pair(a=int(r), b=int(c), similarity=float(s)))
        used_a.add(int(r))
        used_b.add(int(c))

    for i in range(n_a):
        if i not in used_a:
            pairs.append(Pair(a=i, b=None, similarity=0.0))
    for j in range(n_b):
        if j not in used_b:
            pairs.append(Pair(a=None, b=j, similarity=0.0))
    return pairs


def detect_moves(pairs: list[Pair]) -> set[tuple[int, int]]:
    """Identify which (a_idx, b_idx) matched pairs are out-of-order.

    Uses LIS over b-indices in order of a-indices: pairs on the LIS keep
    their original order; pairs not on the LIS are flagged as moved.
    """
    matched = sorted(
        [(p.a, p.b) for p in pairs if p.a is not None and p.b is not None]
    )
    if len(matched) < 2:
        return set()
    b_seq = [b for _, b in matched]
    # LIS indices via patience sorting.
    n = len(b_seq)
    parents = [-1] * n
    tails: list[int] = []  # indexes into b_seq
    for i, val in enumerate(b_seq):
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if b_seq[tails[mid]] < val:
                lo = mid + 1
            else:
                hi = mid
        if lo > 0:
            parents[i] = tails[lo - 1]
        if lo == len(tails):
            tails.append(i)
        else:
            tails[lo] = i
    # Reconstruct LIS membership.
    lis = set()
    if tails:
        k = tails[-1]
        while k != -1:
            lis.add(k)
            k = parents[k]
    moved: set[tuple[int, int]] = set()
    for i, (a, b) in enumerate(matched):
        if i not in lis:
            moved.add((a, b))
    return moved


# -------- word-level diff inside an aligned pair --------

@dataclass
class WordSpan:
    side: str  # "A" or "B"
    token_indices: list[int]
    kind: DiffKind


def diff_pair(
    block_a: Block,
    block_b: Block,
    tokens_a: list[Token],
    tokens_b: list[Token],
) -> list[WordSpan]:
    """Word-level diff. Returns spans of token indices to highlight."""
    a_words = [tokens_a[i].text for i in block_a.token_indices]
    b_words = [tokens_b[i].text for i in block_b.token_indices]
    sm = difflib.SequenceMatcher(a=a_words, b=b_words, autojunk=False)
    spans: list[WordSpan] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete") and i1 < i2:
            spans.append(
                WordSpan(
                    side="A",
                    token_indices=block_a.token_indices[i1:i2],
                    kind=DiffKind.REMOVED,
                )
            )
        if tag in ("replace", "insert") and j1 < j2:
            spans.append(
                WordSpan(
                    side="B",
                    token_indices=block_b.token_indices[j1:j2],
                    kind=DiffKind.ADDED,
                )
            )
    return spans


# -------- assembly --------

def _context_around(
    tokens: list[Token], indices: list[int], radius: int = CONTEXT_WORDS
) -> str:
    """Return a snippet showing the diff span with `radius` words on each side.

    The diff text itself is wrapped in ⟦ ⟧ so an agent can see exactly which
    span is the change vs. its surrounding context.
    """
    if not indices:
        return ""
    lo, hi = min(indices), max(indices)
    start = max(0, lo - radius)
    end = min(len(tokens), hi + 1 + radius)
    selected_set = set(indices)
    parts: list[str] = []
    for i in range(start, end):
        word = tokens[i].text
        if i in selected_set:
            parts.append(f"⟦{word}⟧" if len(selected_set) == 1 else word)
        else:
            parts.append(word)
    snippet = " ".join(parts)
    # Wrap the contiguous diff span as a whole if multi-token.
    if len(selected_set) > 1:
        first_word = tokens[lo].text
        last_word = tokens[hi].text
        # Replace the first/last occurrences inside the span with markers.
        # Simpler: rebuild marking the span boundaries.
        parts = []
        for i in range(start, end):
            if i == lo:
                parts.append(f"⟦{tokens[i].text}")
            elif i == hi:
                parts.append(f"{tokens[i].text}⟧")
            else:
                parts.append(tokens[i].text)
        snippet = " ".join(parts)
    return snippet


def _line_rects_by_page(
    tokens: list[Token], indices: list[int]
) -> list[tuple[int, BBox]]:
    if not indices:
        return []
    sel = [tokens[i] for i in indices]
    sel.sort(key=lambda t: (t.page, t.bbox.y0, t.bbox.x0))
    out: list[tuple[int, BBox]] = []
    cur: list[Token] = []
    cur_page: int | None = None
    cur_y_mid: float | None = None

    def flush() -> None:
        if not cur:
            return
        out.append(
            (
                cur[0].page,
                BBox(
                    x0=min(t.bbox.x0 for t in cur),
                    y0=min(t.bbox.y0 for t in cur),
                    x1=max(t.bbox.x1 for t in cur),
                    y1=max(t.bbox.y1 for t in cur),
                ),
            )
        )
        cur.clear()

    for t in sel:
        y_mid = (t.bbox.y0 + t.bbox.y1) / 2
        same_line = (
            cur_page == t.page
            and cur_y_mid is not None
            and abs(y_mid - cur_y_mid) < max(t.bbox.height, 1.0) * 0.6
        )
        if not same_line:
            flush()
            cur_page = t.page
        cur.append(t)
        cur_y_mid = y_mid
    flush()
    return out


def compute_diff(
    tokens_a: list[Token],
    tokens_b: list[Token],
    page_count_a: int,
    page_count_b: int,
    mode: Mode = "auto",
) -> DiffResult:
    blocks_a = segment_blocks(tokens_a)
    blocks_b = segment_blocks(tokens_b)
    similarity = score_similarity(blocks_a, blocks_b)
    chosen = choose_mode(mode, similarity)

    pairs = align_blocks(blocks_a, blocks_b)
    moved_set = detect_moves(pairs)

    overlays: list[Overlay] = []
    summary: list[SummaryEntry] = []
    next_id = 0

    for p in pairs:
        if p.a is not None and p.b is not None:
            ba, bb = blocks_a[p.a], blocks_b[p.b]
            if (p.a, p.b) in moved_set:
                # Whole-block yellow on both sides; emit one diff_id for nav.
                did = next_id
                next_id += 1
                for page, rect in _line_rects_by_page(tokens_a, ba.token_indices):
                    overlays.append(
                        Overlay(diff_id=did, side="A", page=page, bbox=rect, kind=DiffKind.MOVED)
                    )
                for page, rect in _line_rects_by_page(tokens_b, bb.token_indices):
                    overlays.append(
                        Overlay(diff_id=did, side="B", page=page, bbox=rect, kind=DiffKind.MOVED)
                    )
                summary.append(
                    SummaryEntry(
                        diff_id=did,
                        kind=DiffKind.MOVED,
                        page_a=ba.page,
                        page_b=bb.page,
                        text_a=ba.text[:200],
                        text_b=bb.text[:200],
                        context_a=_context_around(tokens_a, ba.token_indices),
                        context_b=_context_around(tokens_b, bb.token_indices),
                    )
                )
            # Word-level edits inside the pair (always run, even for moved-with-edits).
            for span in diff_pair(ba, bb, tokens_a, tokens_b):
                did = next_id
                next_id += 1
                tok_list = tokens_a if span.side == "A" else tokens_b
                # diff-only mode collapses red/green into the neutral "moved" marker.
                kind = span.kind if chosen == "git-style" else DiffKind.MOVED
                for page, rect in _line_rects_by_page(tok_list, span.token_indices):
                    overlays.append(
                        Overlay(
                            diff_id=did,
                            side=span.side,  # type: ignore[arg-type]
                            page=page,
                            bbox=rect,
                            kind=kind,
                        )
                    )
                snippet = " ".join(tok_list[i].text for i in span.token_indices)[:200]
                ctx = _context_around(tok_list, span.token_indices)
                summary.append(
                    SummaryEntry(
                        diff_id=did,
                        kind=kind,
                        page_a=ba.page if span.side == "A" else None,
                        page_b=bb.page if span.side == "B" else None,
                        text_a=snippet if span.side == "A" else "",
                        text_b=snippet if span.side == "B" else "",
                        context_a=ctx if span.side == "A" else "",
                        context_b=ctx if span.side == "B" else "",
                    )
                )
        elif p.a is not None:
            ba = blocks_a[p.a]
            did = next_id
            next_id += 1
            kind = DiffKind.REMOVED if chosen == "git-style" else DiffKind.MOVED
            for page, rect in _line_rects_by_page(tokens_a, ba.token_indices):
                overlays.append(
                    Overlay(diff_id=did, side="A", page=page, bbox=rect, kind=kind)
                )
            summary.append(
                SummaryEntry(
                    diff_id=did,
                    kind=kind,
                    page_a=ba.page,
                    text_a=ba.text[:200],
                    context_a=_context_around(tokens_a, ba.token_indices),
                )
            )
        elif p.b is not None:
            bb = blocks_b[p.b]
            did = next_id
            next_id += 1
            kind = DiffKind.ADDED if chosen == "git-style" else DiffKind.MOVED
            for page, rect in _line_rects_by_page(tokens_b, bb.token_indices):
                overlays.append(
                    Overlay(diff_id=did, side="B", page=page, bbox=rect, kind=kind)
                )
            summary.append(
                SummaryEntry(
                    diff_id=did,
                    kind=kind,
                    page_b=bb.page,
                    text_b=bb.text[:200],
                    context_b=_context_around(tokens_b, bb.token_indices),
                )
            )

    return DiffResult(
        mode_used=chosen,  # type: ignore[arg-type]
        similarity=similarity,
        overlays=overlays,
        summary=summary,
        page_count_a=page_count_a,
        page_count_b=page_count_b,
    )

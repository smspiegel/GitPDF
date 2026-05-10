"""Pure-Python diff engine: tokens in, overlays out. No I/O.

The engine is split into deterministic phases so each is unit-testable:
    segment_blocks   -> Block[]                  (per visual line, used for similarity score only)
    score_similarity -> float in [0,1]
    compute_diff     -> Overlay[] + SummaryEntry[]

`compute_diff` runs a word-level diff over the *full* token stream of each
side, intentionally ignoring layout (page breaks, line wrapping, column
reflow). Once the diff identifies which token ranges differ, the matching
ranges are mapped back to per-line bboxes for highlighting. Earlier
versions aligned at the block (per-line) level using the Hungarian
algorithm; that approach broke whenever the same content occupied a
different number of lines on each side -- e.g. a paragraph that fit on
one line in B but split across a page break in A produced spurious
add/remove flags. Diffing the word stream directly removes that whole
class of false positives.
"""
from __future__ import annotations

import difflib

import numpy as np
from rapidfuzz import fuzz

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
LINE_TOLERANCE_FACTOR = 0.6  # tokens within median_h * this share a visual line
MIN_BLOCK_CHARS = 2
CONTEXT_WORDS = 12  # words on each side of a diff span included in summary context


# -------- block segmentation --------

def segment_blocks(tokens: list[Token]) -> list[Block]:
    """One block per visual line.

    Used for the global similarity score and for any consumer that wants a
    layout-aware view of the document. The actual diff is computed at the
    word-stream level (see `compute_diff`) and does not depend on this.
    """
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
    line_tolerance = median_h * LINE_TOLERANCE_FACTOR

    prev_page: int | None = None
    prev_y_mid: float | None = None
    for t in tokens:
        y_mid = (t.bbox.y0 + t.bbox.y1) / 2.0
        if prev_page is not None and (
            t.page != prev_page or abs(y_mid - prev_y_mid) > line_tolerance
        ):
            flush()
        current.append(t)
        prev_page = t.page
        prev_y_mid = y_mid
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


# -------- assembly helpers --------

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


# -------- move detection on opcode stream --------

def _detect_moved_pairs(
    opcodes: list[tuple[str, int, int, int, int]],
    a_words: list[str],
    b_words: list[str],
) -> dict[int, int]:
    """Find delete↔insert opcode pairs whose content is identical.

    Returns a mapping `op_idx -> partner_op_idx` covering both directions.
    Only pure delete/insert opcodes participate; a `replace` is treated as
    an in-place edit, never a move. Identical content is matched word-for-
    word (case-sensitive); if a sequence appears twice on each side, the
    first delete pairs with the first available insert.
    """
    inserts_by_content: dict[tuple[str, ...], list[int]] = {}
    for idx, op in enumerate(opcodes):
        if op[0] == "insert":
            content = tuple(b_words[op[3]:op[4]])
            inserts_by_content.setdefault(content, []).append(idx)

    pairs: dict[int, int] = {}
    for idx, op in enumerate(opcodes):
        if op[0] != "delete":
            continue
        content = tuple(a_words[op[1]:op[2]])
        candidates = inserts_by_content.get(content)
        if candidates:
            partner = candidates.pop(0)
            pairs[idx] = partner
            pairs[partner] = idx
    return pairs


# -------- main entry point --------

def compute_diff(
    tokens_a: list[Token],
    tokens_b: list[Token],
    page_count_a: int,
    page_count_b: int,
    mode: Mode = "auto",
) -> DiffResult:
    # Similarity is layout-aware (block-level) so mode selection thresholds
    # behave like the user expects from the visible UI.
    blocks_a = segment_blocks(tokens_a)
    blocks_b = segment_blocks(tokens_b)
    similarity = score_similarity(blocks_a, blocks_b)
    chosen = choose_mode(mode, similarity)

    # The diff itself is layout-blind: we compare the raw word streams, so
    # identical content matches as `equal` regardless of where it sits on
    # the page. This is what stops a paragraph that wraps differently
    # between A and B from being flagged as remove+add.
    a_words = [t.text for t in tokens_a]
    b_words = [t.text for t in tokens_b]
    sm = difflib.SequenceMatcher(a=a_words, b=b_words, autojunk=False)
    opcodes = sm.get_opcodes()
    moved_partners = _detect_moved_pairs(opcodes, a_words, b_words)

    # Pre-assign a diff_id per change. A move pair shares one id so the
    # frontend can highlight both sides on a single click.
    op_diff_id: dict[int, int] = {}
    next_id = 0
    for op_idx, op in enumerate(opcodes):
        if op[0] == "equal" or op_idx in op_diff_id:
            continue
        op_diff_id[op_idx] = next_id
        partner = moved_partners.get(op_idx)
        if partner is not None:
            op_diff_id[partner] = next_id
        next_id += 1

    overlays: list[Overlay] = []
    summary: list[SummaryEntry] = []
    move_pair_emitted: set[int] = set()  # dedup the second visit of a move pair

    for op_idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            continue
        did = op_diff_id[op_idx]
        is_moved = op_idx in moved_partners

        if chosen == "git-style":
            a_kind = DiffKind.MOVED if is_moved else DiffKind.REMOVED
            b_kind = DiffKind.MOVED if is_moved else DiffKind.ADDED
        else:
            # diff-only collapses red/green into the neutral marker.
            a_kind = b_kind = DiffKind.MOVED

        a_indices = list(range(i1, i2)) if (tag in ("delete", "replace") and i1 < i2) else []
        b_indices = list(range(j1, j2)) if (tag in ("insert", "replace") and j1 < j2) else []

        for page, rect in _line_rects_by_page(tokens_a, a_indices):
            overlays.append(
                Overlay(diff_id=did, side="A", page=page, bbox=rect, kind=a_kind)
            )
        for page, rect in _line_rects_by_page(tokens_b, b_indices):
            overlays.append(
                Overlay(diff_id=did, side="B", page=page, bbox=rect, kind=b_kind)
            )

        # ---- summary entry ----
        # One row per change. Kind drives how the frontend renders the row:
        #   * pure delete -> REMOVED (red row, text_a only)
        #   * pure insert -> ADDED   (green row, text_b only)
        #   * replace     -> REPLACED (split row: red text_a | green text_b)
        #   * move pair   -> MOVED  (yellow row, both sides; one entry per pair)
        # In diff-only mode every change collapses to MOVED (single colour).
        if is_moved:
            if did in move_pair_emitted:
                continue
            move_pair_emitted.add(did)
            partner_idx = moved_partners[op_idx]
            ptag, pi1, pi2, pj1, pj2 = opcodes[partner_idx]
            if tag == "delete":
                a_lo, a_hi, b_lo, b_hi = i1, i2, pj1, pj2
            else:  # tag == "insert"
                a_lo, a_hi, b_lo, b_hi = pi1, pi2, j1, j2
            move_a = list(range(a_lo, a_hi))
            move_b = list(range(b_lo, b_hi))
            summary.append(
                SummaryEntry(
                    diff_id=did,
                    kind=DiffKind.MOVED,
                    page_a=tokens_a[a_lo].page,
                    page_b=tokens_b[b_lo].page,
                    text_a=" ".join(a_words[a_lo:a_hi])[:200],
                    text_b=" ".join(b_words[b_lo:b_hi])[:200],
                    context_a=_context_around(tokens_a, move_a),
                    context_b=_context_around(tokens_b, move_b),
                )
            )
            continue

        if a_indices and b_indices:
            # `replace`: emit one split-row entry showing before/after.
            summary_kind = (
                DiffKind.REPLACED if chosen == "git-style" else DiffKind.MOVED
            )
            summary.append(
                SummaryEntry(
                    diff_id=did,
                    kind=summary_kind,
                    page_a=tokens_a[i1].page,
                    page_b=tokens_b[j1].page,
                    text_a=" ".join(a_words[i1:i2])[:200],
                    text_b=" ".join(b_words[j1:j2])[:200],
                    context_a=_context_around(tokens_a, a_indices),
                    context_b=_context_around(tokens_b, b_indices),
                )
            )
        elif a_indices:
            summary.append(
                SummaryEntry(
                    diff_id=did,
                    kind=a_kind,
                    page_a=tokens_a[i1].page,
                    page_b=None,
                    text_a=" ".join(a_words[i1:i2])[:200],
                    text_b="",
                    context_a=_context_around(tokens_a, a_indices),
                    context_b="",
                )
            )
        elif b_indices:
            summary.append(
                SummaryEntry(
                    diff_id=did,
                    kind=b_kind,
                    page_a=None,
                    page_b=tokens_b[j1].page,
                    text_a="",
                    text_b=" ".join(b_words[j1:j2])[:200],
                    context_a="",
                    context_b=_context_around(tokens_b, b_indices),
                )
            )

    # If we found any real diff, never report a similarity that would round
    # up to "100%" in the UI -- claiming perfect similarity when overlays
    # exist is misleading. The cap is purely cosmetic: mode selection above
    # already used the raw value.
    reported_similarity = min(similarity, 0.99) if overlays else similarity

    return DiffResult(
        mode_used=chosen,  # type: ignore[arg-type]
        similarity=reported_similarity,
        overlays=overlays,
        summary=summary,
        page_count_a=page_count_a,
        page_count_b=page_count_b,
    )

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


Side = Literal["A", "B"]


class BBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


class Token(BaseModel):
    """A single word/glyph cluster with its position on a page."""
    page: int
    bbox: BBox
    text: str
    index: int


class Block(BaseModel):
    """A paragraph-level grouping of tokens used for alignment."""
    id: int
    page: int
    bbox: BBox
    text: str
    token_indices: list[int]


class DiffKind(str, Enum):
    REMOVED = "removed"
    ADDED = "added"
    MOVED = "moved"
    EQUAL = "equal"


class Overlay(BaseModel):
    """A highlight rectangle to render on top of a rendered PDF page."""
    diff_id: int
    side: Side
    page: int
    bbox: BBox
    kind: DiffKind


class SummaryEntry(BaseModel):
    diff_id: int
    kind: DiffKind
    page_a: int | None = None
    page_b: int | None = None
    text_a: str = ""
    text_b: str = ""
    # Surrounding text on each side, useful for downstream consumers
    # that need to judge a change in context.
    context_a: str = ""
    context_b: str = ""


Mode = Literal["auto", "git-style", "diff-only"]


class DiffResult(BaseModel):
    mode_used: Literal["git-style", "diff-only"]
    similarity: float = Field(ge=0.0, le=1.0)
    overlays: list[Overlay]
    summary: list[SummaryEntry]
    page_count_a: int
    page_count_b: int


class ExtractedDoc(BaseModel):
    """Output of the extract step. Pages are 1-indexed."""
    page_count: int
    page_sizes: list[tuple[float, float]]
    tokens: list[Token]
    used_ocr: list[bool]

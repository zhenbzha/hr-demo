"""Data types for extracted text, citation geometry, and source pages."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TextLine:
    """An OCR line with a page-coordinate polygon and analysis-text span.

    Span offsets and lengths use Unicode code points.
    """

    text: str
    polygon: list[float] = field(default_factory=list)
    span_offset: int = 0
    span_length: int = 0


@dataclass(frozen=True)
class ContentBlock:
    """A text or table fragment with zero-based source page numbers.

    heading is the Document Intelligence section path, or None when
    structural section metadata is unavailable.
    """

    text: str
    page_numbers: tuple[int, ...]
    kind: Literal["text", "table"] = "text"
    table_header_rows: int | None = None
    heading: str | None = None


@dataclass
class Page:
    """An analyzed physical page with zero-based numbering and OCR geometry.

    The offset locates this page's text in the concatenated parsed pages.
    """

    page_num: int
    offset: int
    text: str
    blocks: list[ContentBlock] = field(default_factory=list)
    width: float | None = None
    height: float | None = None
    unit: str | None = None
    angle: float | None = None
    lines: list[TextLine] = field(default_factory=list)


@dataclass(frozen=True)
class Chunk:
    """A searchable passage with all contributing zero-based page numbers."""

    text: str
    page_numbers: tuple[int, ...]
    heading: str = ""

    @property
    def page_num(self) -> int:
        return min(self.page_numbers)

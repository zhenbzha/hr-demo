"""Represent PDF pages, extracted text, and text positions."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TextLine:
    """
    A line of extracted text and its position on a PDF page.

    Attributes
    ----------
    text : str
        Extracted text used to locate citations.
    polygon : list of float
        Alternating x/y coordinates outlining the text on the page,
        measured in the page's ``unit``.
    span_offset : int
        Character offset in the complete Document Intelligence analysis
        text, counted in Unicode code points from zero.
    span_length : int
        Number of Unicode code points covered by the line span.
    """

    text: str
    polygon: list[float] = field(default_factory=list)
    span_offset: int = 0
    span_length: int = 0


@dataclass(frozen=True)
class ContentBlock:
    """
    A text passage or table, with the PDF pages it came from.

    Attributes
    ----------
    text : str
        Extracted Markdown text or native table HTML.
    page_numbers : tuple of int
        All contributing physical PDF pages, numbered from zero.
    kind : {'text', 'table'}
        Type of content, used to choose how to split it into passages.
    table_header_rows : int or None
        Number of column-header rows to repeat when splitting a table.
        None means Document Intelligence did not identify header rows.
    heading : str or None
        Section heading, including parent sections, reported by Document
        Intelligence. None when unavailable.
    """

    text: str
    page_numbers: tuple[int, ...]
    kind: Literal["text", "table"] = "text"
    table_header_rows: int | None = None
    heading: str | None = None


@dataclass
class Page:
    """
    A PDF page with its extracted content and text coordinates.

    Attributes
    ----------
    page_num : int
        Physical PDF page index, starting at zero.
    offset : int
        Character offset of this page in the combined text of all
        parsed pages.
    text : str
        Concatenated text of this page's parsed blocks.
    blocks : list of ContentBlock
        Text passages and tables in reading order. A table spanning
        several pages appears once and records all its source pages.
    width : float or None
        Page width in ``unit``, or None if unavailable.
    height : float or None
        Page height in ``unit``, or None if unavailable.
    unit : str or None
        Measurement unit for page dimensions and text coordinates.
    angle : float or None
        Reported page orientation in degrees, or None if unavailable.
    lines : list of TextLine
        Text lines and their positions, used to highlight citations.
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
    """
    A text passage with its source pages and section heading.

    Attributes
    ----------
    text : str
        Extracted policy text/table HTML, including any chunk overlap.
    page_numbers : tuple of int
        Contributing physical PDF page indices, starting at zero.
        Search documents and citation annotations convert these to one-based
        page numbers for readers.
    heading : str
        Section heading included with the text sent to the embedding model.
    """

    text: str
    page_numbers: tuple[int, ...]
    heading: str = ""

    @property
    def page_num(self) -> int:
        return min(self.page_numbers)

"""Parse Document Intelligence Markdown into text and table blocks."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import (
    AnalyzeDocumentRequest,
    AnalyzeResult,
    DocumentContentFormat,
    DocumentPage,
    DocumentSpan,
    DocumentTable,
)

from .page import ContentBlock, Page, TextLine

ANALYSIS_API_VERSION = "2024-11-30"
MODEL_ID = "prebuilt-layout"
PARSER_VERSION = 5
STRING_INDEX_TYPE = "unicodeCodePoint"

type _SectionSpan = tuple[int, int, tuple[str, ...]]


def _section_spans(result: AnalyzeResult) -> list[_SectionSpan]:
    """
    Map section spans to their inherited document and section headings.

    Parameters
    ----------
    result : AnalyzeResult
        Layout result containing section references and paragraph roles.

    Returns
    -------
    list of tuple
        Start offset, exclusive end offset, and heading path for each section
        span. Offsets count Unicode code points in ``result.content``. Paths
        contain ancestor headings followed by titles and section headings
        from the current section, in their reported order.
    """
    sections = getattr(result, "sections", None) or []
    paragraphs = getattr(result, "paragraphs", None) or []
    children = {
        int(element.removeprefix("/sections/"))
        for section in sections
        for element in section.elements
        if element.startswith("/sections/")
    }
    spans: list[_SectionSpan] = []

    def visit(index: int, parents: tuple[str, ...]) -> None:
        section = sections[index]
        headings = tuple(
            paragraph.content
            for element in section.elements
            if element.startswith("/paragraphs/")
            for paragraph in [paragraphs[int(element.removeprefix("/paragraphs/"))]]
            if paragraph.role in {"title", "sectionHeading"}
        )
        path = parents + headings
        spans.extend(
            (span.offset, span.offset + span.length, path) for span in section.spans
        )
        for element in section.elements:
            if element.startswith("/sections/"):
                visit(int(element.removeprefix("/sections/")), path)

    for index in range(len(sections)):
        if index not in children:
            visit(index, ())
    return spans


def _heading_at(spans: list[_SectionSpan], offset: int) -> str | None:
    """
    Return the deepest heading path among sections covering the offset.

    Depth is the number of inherited headings; the first path wins ties.
    Return None when section metadata is absent, or an empty string when
    spans exist but no matching headings apply.
    """
    if not spans:
        return None
    paths = (path for start, end, path in spans if start <= offset < end)
    return " > ".join(max(paths, key=len, default=()))


def _text_blocks(
    content: str,
    start: int,
    end: int,
    page_num: int,
    sections: list[_SectionSpan],
) -> list[ContentBlock]:
    """Split text at the section boundaries reported by Document Intelligence."""
    boundaries = {start, end}
    for section_start, section_end, _ in sections:
        boundaries.update(
            boundary
            for boundary in (section_start, section_end)
            if start < boundary < end
        )
    return [
        ContentBlock(
            content[block_start:block_end],
            (page_num,),
            heading=_heading_at(sections, block_start),
        )
        for block_start, block_end in pairwise(sorted(boundaries))
    ]


def _native_table_markup(content: str, table: DocumentTable) -> str:
    """Return native table HTML using Unicode-code-point spans in content."""
    return "".join(
        content[span.offset : span.offset + span.length]
        for span in sorted(table.spans, key=lambda span: span.offset)
    )


def _column_header_rows(table: DocumentTable) -> int:
    """
    Count consecutive header-only rows at the start of a table.

    Treat columnHeader and stubHead cells as headers. Stop at the first
    row that contains a body cell or has no header cells.
    """
    header_rows: set[int] = set()
    body_rows: set[int] = set()
    for cell in table.cells:
        target = header_rows if cell.kind in {"columnHeader", "stubHead"} else body_rows
        target.update(range(cell.row_index, cell.row_index + (cell.row_span or 1)))
    count = 0
    while count in header_rows and count not in body_rows:
        count += 1
    return count


def extract_page_lines(page: DocumentPage) -> list[TextLine]:
    """
    Extract text lines and their positions from an analyzed PDF page.

    Parameters
    ----------
    page : DocumentPage
        Page from a Document Intelligence layout result using
        Unicode-code-point offsets.

    Returns
    -------
    list of TextLine
        Text lines with their page coordinates and character offsets.
        Coordinates use the page's reported unit. Offsets refer to
        the complete analysis text.
    """
    extracted = []
    for line in page.lines or []:
        if not line.content:
            continue
        spans = line.spans or []
        offset = min((span.offset for span in spans), default=0)
        end = max(
            (span.offset + span.length for span in spans),
            default=offset + len(line.content),
        )
        extracted.append(
            TextLine(
                text=line.content,
                polygon=[float(coordinate) for coordinate in line.polygon or []],
                span_offset=offset,
                span_length=end - offset,
            )
        )
    return extracted


def _spans_overlap(first: DocumentSpan, second: DocumentSpan) -> bool:
    """Test strict span overlap; touching end and start offsets do not overlap."""
    return (
        first.offset < second.offset + second.length
        and first.offset + first.length > second.offset
    )


def _table_intersections(
    page_span: DocumentSpan, tables: list[DocumentTable]
) -> list[tuple[int, int, int]]:
    """
    Clip table spans to a page span and order them by source position.

    Parameters
    ----------
    page_span : DocumentSpan
        Page interval with Unicode-code-point offsets in the analysis text.
    tables : list of DocumentTable
        Tables in their original analysis-result order.

    Returns
    -------
    list of tuple of int
        Intersections as start offset, exclusive end offset, and table index.
        Each table span contributes separately; tuples are sorted by start,
        then end, then table index.
    """
    start, end = page_span.offset, page_span.offset + page_span.length
    return sorted(
        (max(start, span.offset), min(end, span.offset + span.length), index)
        for index, table in enumerate(tables)
        for span in table.spans
        if _spans_overlap(span, page_span)
    )


def _table_blocks(
    result: AnalyzeResult, sections: list[_SectionSpan]
) -> dict[int, ContentBlock]:
    """
    Return table blocks keyed by their index in the analysis result.

    Record all source pages with zero-based numbering.
    """
    blocks = {}
    for index, table in enumerate(result.tables or []):
        page_numbers = tuple(
            sorted(
                {
                    page.page_number - 1
                    for page in result.pages
                    if any(
                        _spans_overlap(span, page_span)
                        for span in table.spans
                        for page_span in page.spans
                    )
                }
            )
        )
        blocks[index] = ContentBlock(
            _native_table_markup(result.content, table),
            page_numbers,
            "table",
            table_header_rows=_column_header_rows(table),
            heading=_heading_at(sections, min(span.offset for span in table.spans)),
        )
    return blocks


def _page_blocks(
    result: AnalyzeResult,
    page: DocumentPage,
    tables: dict[int, ContentBlock],
    inserted_tables: set[int],
    sections: list[_SectionSpan],
) -> list[ContentBlock]:
    """
    Build a page's text and table blocks in reading order.

    Add each table once across all pages and update inserted_tables.
    """
    blocks = []
    for page_span in sorted(page.spans, key=lambda span: span.offset):
        start, end = page_span.offset, page_span.offset + page_span.length
        cursor = start
        for table_start, table_end, table_index in _table_intersections(
            page_span, result.tables or []
        ):
            if table_start > cursor:
                blocks.extend(
                    _text_blocks(
                        result.content,
                        cursor,
                        table_start,
                        page.page_number - 1,
                        sections,
                    )
                )
            if table_index not in inserted_tables:
                blocks.append(tables[table_index])
                inserted_tables.add(table_index)
            cursor = table_end
        if cursor < end:
            blocks.extend(
                _text_blocks(
                    result.content, cursor, end, page.page_number - 1, sections
                )
            )
    return blocks


def parse_layout_result(result: AnalyzeResult) -> list[Page]:
    """
    Return pages containing text, tables, and citation coordinates.

    Parameters
    ----------
    result : AnalyzeResult
        Completed ``prebuilt-layout`` analysis with Markdown content,
        native HTML tables, and Unicode-code-point spans.

    Returns
    -------
    list of Page
        All extracted pages in reading order, numbered from zero, with
        section headings and text coordinates. A table spanning several
        pages appears once and records every page it came from.
    """
    pages = []
    offset = 0
    sections = _section_spans(result)
    tables = _table_blocks(result, sections)
    inserted_tables: set[int] = set()
    for page in result.pages:
        blocks = _page_blocks(result, page, tables, inserted_tables, sections)
        text = "".join(block.text for block in blocks)
        pages.append(
            Page(
                page_num=page.page_number - 1,
                offset=offset,
                text=text,
                blocks=blocks,
                width=float(page.width) if page.width is not None else None,
                height=float(page.height) if page.height is not None else None,
                unit=getattr(page.unit, "value", page.unit),
                angle=float(page.angle) if page.angle is not None else None,
                lines=extract_page_lines(page),
            )
        )
        offset += len(text)
    return pages


def analyze_local_pdf(
    *,
    client: DocumentIntelligenceClient,
    path: Path,
    timeout: float = 600,
) -> list[Page]:
    """
    Analyze a local PDF and return its parsed layout pages.

    Parameters
    ----------
    client : DocumentIntelligenceClient
        Authenticated Document Intelligence client.
    path : pathlib.Path
        PDF file to send to the ``prebuilt-layout`` model.
    timeout : float, default=600
        Maximum seconds to wait for the analysis job to complete.

    Returns
    -------
    list of Page
        Pages containing extracted text, tables, page dimensions,
        and text positions.

    Raises
    ------
    TimeoutError
        Analysis is still incomplete after the poller wait.
    """
    poller = client.begin_analyze_document(
        model_id=MODEL_ID,
        body=AnalyzeDocumentRequest(bytes_source=path.read_bytes()),
        output_content_format=DocumentContentFormat.MARKDOWN,
        string_index_type=STRING_INDEX_TYPE,
        connection_timeout=30,
        read_timeout=120,
    )
    result = poller.result(timeout=timeout)
    if not poller.done():
        raise TimeoutError(
            f"Document Intelligence did not finish analyzing {path.name}"
        )
    return parse_layout_result(result)

"""Document Intelligence parser following Azure-Samples reading-order handling."""

from __future__ import annotations

import html
import re
from pathlib import Path

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import (
    AnalyzeDocumentRequest,
    DocumentContentFormat,
    DocumentTable,
)

from .page import Page, TextLine


CONTROL_SECTION_HEADINGS = {
    "employee acknowledgement",
    "revision history",
    "työntekijän kuittaus",
    "muutoshistoria",
    "僱員確認",
    "修訂紀錄",
    "official reference",
    "official references",
    "官方參考",
}
FOOTER_PATTERN = re.compile(
    r"^(?:HR-(?:POL|IN|FI|HK)-\d{3}\s*\||Page \d+|Sivu \d+|第 \d+ 頁)"
)


def table_to_html(table: DocumentTable) -> str:
    table_html = "<figure><table>"
    rows = [
        sorted(
            [
                cell
                for cell in table.cells
                if cell.row_index == row_index
            ],
            key=lambda cell: cell.column_index,
        )
        for row_index in range(table.row_count)
    ]
    for row_cells in rows:
        table_html += "<tr>"
        for cell in row_cells:
            tag = (
                "th"
                if cell.kind in {"columnHeader", "rowHeader"}
                else "td"
            )
            spans = ""
            if cell.column_span is not None and cell.column_span > 1:
                spans += f' colspan="{cell.column_span}"'
            if cell.row_span is not None and cell.row_span > 1:
                spans += f' rowspan="{cell.row_span}"'
            table_html += (
                f"<{tag}{spans}>{html.escape(cell.content)}</{tag}>"
            )
        table_html += "</tr>"
    return table_html + "</table></figure>"


def remove_document_control(text: str) -> str:
    kept_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        normalized = stripped.lstrip("#").strip()
        normalized = re.sub(r"^\d+\s*", "", normalized).casefold()
        if normalized in CONTROL_SECTION_HEADINGS:
            break
        if FOOTER_PATTERN.match(stripped):
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip()


def extract_page_lines(page: object) -> list[TextLine]:
    extracted: list[TextLine] = []
    for line in getattr(page, "lines", None) or []:
        content = (line.content or "").strip()
        normalized = content.lstrip("#").strip()
        normalized = re.sub(r"^\d+\s*", "", normalized).casefold()
        if normalized in CONTROL_SECTION_HEADINGS:
            break
        if not content or FOOTER_PATTERN.match(content):
            continue
        polygon_values = line.polygon or []
        polygon = (
            [float(coordinate) for coordinate in polygon_values]
            if not polygon_values or isinstance(polygon_values[0], (int, float))
            else [
                coordinate
                for point in polygon_values
                for coordinate in (float(point.x), float(point.y))
            ]
        )
        span = (line.spans or [None])[0]
        extracted.append(
            TextLine(
                text=content,
                polygon=polygon,
                span_offset=int(span.offset) if span else 0,
                span_length=int(span.length) if span else len(content),
            )
        )
    return extracted


def analyze_local_pdf(
    *,
    client: DocumentIntelligenceClient,
    path: Path,
) -> list[Page]:
    content_bytes = path.read_bytes()
    poller = client.begin_analyze_document(
        model_id="prebuilt-layout",
        body=AnalyzeDocumentRequest(bytes_source=content_bytes),
        output_content_format=DocumentContentFormat.MARKDOWN,
    )
    result = poller.result()
    pages: list[Page] = []
    output_offset = 0

    for page in result.pages:
        if page.page_number == 1:
            continue
        page_offset = page.spans[0].offset
        page_length = page.spans[0].length
        tables_on_page = [
            table
            for table in (result.tables or [])
            if table.bounding_regions
            and table.bounding_regions[0].page_number == page.page_number
        ]
        table_mask: list[int | None] = [None] * page_length
        for table_index, table in enumerate(tables_on_page):
            for span in table.spans:
                for relative_index in range(span.length):
                    index = span.offset - page_offset + relative_index
                    if 0 <= index < page_length:
                        table_mask[index] = table_index

        page_text_parts: list[str] = []
        inserted_tables: set[int] = set()
        for index, table_index in enumerate(table_mask):
            if table_index is None:
                page_text_parts.append(result.content[page_offset + index])
            elif table_index not in inserted_tables:
                page_text_parts.append(
                    table_to_html(tables_on_page[table_index])
                )
                inserted_tables.add(table_index)

        page_text = "".join(page_text_parts)
        page_text = page_text.replace("<!-- PageBreak -->", "")
        page_text = remove_document_control(page_text)
        if not page_text:
            continue
        pages.append(
            Page(
                page_num=page.page_number - 1,
                offset=output_offset,
                text=page_text,
                tables=[
                    table_to_html(table) for table in tables_on_page
                ],
                width=float(page.width) if page.width is not None else None,
                height=float(page.height) if page.height is not None else None,
                unit=(
                    getattr(page.unit, "value", str(page.unit))
                    if page.unit is not None
                    else None
                ),
                angle=float(page.angle) if page.angle is not None else None,
                lines=extract_page_lines(page),
            )
        )
        output_offset += len(page_text)

    return pages

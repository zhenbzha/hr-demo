"""Offline regression tests for extraction, chunking, and Azure contracts."""

import io
import json
import tempfile
import unittest
import urllib.error
from functools import partial
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from azure.ai.documentintelligence.models import AnalyzeResult
from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchIndexingBufferedSender
from configure_ask_hr_knowledge import knowledge_source_definition
from ingestion import azure_http, document_parser, search_index
from ingestion.page import Page
from ingestion.search_schema import index_schema
from ingestion.semantic import (
    build_search_documents,
    build_semantic_text,
    normalized_content,
)
from ingestion.textsplitter import (
    EMBEDDING_TOKEN_LIMIT,
    SentenceTextSplitter,
    token_count,
)
from urllib3 import HTTPResponse


def policy_metadata() -> dict:
    return {
        "documentId": "HR-TEST-001",
        "name": "Example_Policy.pdf",
        "region": "global",
        "language": "en",
        "version": "1.0",
        "policyTopic": "leave",
        "sha256": "a" * 64,
        "documentPath": "documents/HR-TEST-001/1.0.pdf",
        "annotationPath": "annotations/HR-TEST-001/1.0.json",
    }


def embedding_client(payload: dict) -> Mock:
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.embeddings.create.return_value.model_dump.return_value = payload
    return client


def buffered_sender_factory():
    return partial(
        SearchIndexingBufferedSender,
        "https://example.search.windows.net",
        "hr",
        AzureKeyCredential("unit-test"),
        api_version=azure_http.SEARCH_API_VERSION,
    )


def http_response(
    request: requests.PreparedRequest,
    payload: dict | bytes,
    status: int = 200,
    *,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.request = request
    response.url = request.url
    response.headers["Content-Type"] = (
        "application/octet-stream" if isinstance(payload, bytes) else "application/json"
    )
    response._content = (
        payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    )
    response.headers.update(headers or {})
    response._content_consumed = True
    response.raw = HTTPResponse(
        body=io.BytesIO(response._content),
        status=status,
        headers=dict(response.headers),
        preload_content=False,
    )
    return response


def search_key_schema() -> dict:
    return {
        "name": "hr",
        "fields": [{"name": "chunk_id", "type": "Edm.String", "key": True}],
    }


def native_layout(
    content: str,
    *,
    tables: list[dict] | None = None,
    page_break: int | None = None,
    sections: list[dict] | None = None,
    paragraphs: list[dict] | None = None,
) -> AnalyzeResult:
    boundaries = (
        [0, len(content)] if page_break is None else [0, page_break, len(content)]
    )
    return AnalyzeResult(
        {
            "content": content,
            "contentFormat": "markdown",
            "pages": [
                {
                    "pageNumber": number,
                    "spans": [{"offset": start, "length": end - start}],
                    "lines": [],
                    "width": 8.5,
                    "height": 11,
                    "unit": "inch",
                    "angle": 0,
                }
                for number, (start, end) in enumerate(pairwise(boundaries), start=1)
            ],
            "tables": tables or [],
            "sections": sections or [],
            "paragraphs": paragraphs or [],
        }
    )


class ExtractionTests(unittest.TestCase):
    def test_layout_hierarchy_and_table_are_packed_without_losing_source_text(self):
        header = '<!-- PageHeader="Example employer" -->\n'
        title = "# Example policy\n\n"
        heading = "## Working arrangements\n\n"
        table = "<table><tr><td>Working week</td><td>Thirty hours</td></tr></table>\n\n"
        child = "### Additional hours\n\n"
        rule = "Additional hours require advance agreement.\n"
        footer = '<!-- PageFooter="Retain a copy." -->'
        content = header + title + heading + table + child + rule + footer
        title_start = len(header)
        heading_start = title_start + len(title)
        table_start = heading_start + len(heading)
        child_start = table_start + len(table)
        rule_start = child_start + len(child)
        result = native_layout(
            content,
            paragraphs=[
                {"role": "title", "content": "Example policy", "spans": []},
                {
                    "role": "sectionHeading",
                    "content": "Working arrangements",
                    "spans": [],
                },
                {"role": "sectionHeading", "content": "Additional hours", "spans": []},
                {"content": rule, "spans": []},
            ],
            sections=[
                {
                    "spans": [
                        {"offset": title_start, "length": len(content) - title_start}
                    ],
                    "elements": ["/paragraphs/0", "/sections/1"],
                },
                {
                    "spans": [
                        {
                            "offset": heading_start,
                            "length": len(content) - heading_start,
                        }
                    ],
                    "elements": ["/paragraphs/1", "/tables/0", "/sections/2"],
                },
                {
                    "spans": [
                        {
                            "offset": child_start,
                            "length": rule_start + len(rule) - child_start,
                        }
                    ],
                    "elements": ["/paragraphs/2", "/paragraphs/3"],
                },
            ],
            tables=[
                {
                    "rowCount": 1,
                    "columnCount": 2,
                    "cells": [],
                    "spans": [{"offset": table_start, "length": len(table)}],
                }
            ],
        )
        pages = document_parser.parse_layout_result(result)
        self.assertEqual(pages[0].text, content)
        table_block = next(block for block in pages[0].blocks if block.kind == "table")
        self.assertEqual(table_block.heading, "Example policy > Working arrangements")
        child_block = next(block for block in pages[0].blocks if rule in block.text)
        self.assertEqual(
            child_block.heading,
            "Example policy > Working arrangements > Additional hours",
        )
        chunks = list(SentenceTextSplitter().split_pages(pages))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(
            normalized_content(chunks[0].text), normalized_content(content)
        )
        self.assertIn(table.strip(), chunks[0].text)
        self.assertIn("Additional hours", chunks[0].heading)

    def test_packed_sections_budget_includes_all_heading_metadata(self):
        content = "\n\n".join(
            f"## Section {number} with descriptive context\n"
            f"Rule {number} requires a written approval."
            for number in range(12)
        )

        def measure(text: str, heading: str) -> int:
            return token_count(text + "\n" + heading)

        chunks = list(
            SentenceTextSplitter(90, measure_input=measure).split_pages(
                [Page(0, 0, content)]
            )
        )
        self.assertTrue(
            all(measure(chunk.text, chunk.heading) <= 90 for chunk in chunks)
        )
        for number in range(12):
            self.assertIn(
                f"Rule {number} requires a written approval.",
                "".join(chunk.text for chunk in chunks),
            )

    def test_nested_sections_and_their_page_continuations_are_retained(self):
        first_page = (
            "## Revision history\nEffective from October.\n"
            "### Official references\nRetain supporting evidence.\n"
        )
        second_page = (
            "Evidence must cover the full claim period.\n"
            "### Earlier revision\nEarlier claims follow the previous rules.\n"
            "## Leave\nActive policy."
        )
        pages = document_parser.parse_layout_result(
            native_layout(first_page + second_page, page_break=len(first_page))
        )
        self.assertEqual([page.text for page in pages], [first_page, second_page])
        documents = build_search_documents(pages=pages, source=policy_metadata())
        semantic_text = "\n".join(document["semantic_text"] for document in documents)
        for passage in (
            "Effective from October.",
            "Retain supporting evidence.",
            "Evidence must cover the full claim period.",
            "Earlier claims follow the previous rules.",
            "Active policy.",
        ):
            self.assertIn(passage, semantic_text)
        self.assertEqual(
            {
                page
                for document in documents
                for page in range(document["page_start"], document["page_end"] + 1)
            },
            {1, 2},
        )

    def test_native_html_keeps_caption_entities_and_rowspan_covered_rows(self):
        markup = (
            "<table>\n<caption>Policy &amp; exceptions</caption>\n"
            '<tr><td rowspan="2">Merged &lt;rule&gt;</td></tr>\n'
            "<tr></tr>\n</table>"
        )
        result = native_layout(
            markup,
            tables=[
                {
                    "rowCount": 2,
                    "columnCount": 1,
                    "cells": [
                        {
                            "rowIndex": 0,
                            "columnIndex": 0,
                            "rowSpan": 2,
                            "content": "Not the HTML source",
                        }
                    ],
                    "spans": [{"offset": 0, "length": len(markup)}],
                }
            ],
        )
        parsed = document_parser.parse_layout_result(result)
        self.assertEqual(parsed[0].blocks[0].text, markup)
        self.assertEqual(parsed[0].blocks[0].text.count("<tr>"), 2)
        self.assertEqual(
            normalized_content(markup), "Policy & exceptions\nMerged <rule>"
        )

    def test_native_table_spans_can_be_discontiguous(self):
        first = "<table><tr><td>First page "
        second = "and second page</td></tr></table>"
        content = first + "intervening page metadata" + second
        result = native_layout(
            content,
            tables=[
                {
                    "rowCount": 1,
                    "columnCount": 1,
                    "cells": [],
                    "spans": [
                        {"offset": 0, "length": len(first)},
                        {"offset": len(content) - len(second), "length": len(second)},
                    ],
                }
            ],
        )
        parsed = document_parser.parse_layout_result(result)
        self.assertEqual(parsed[0].blocks[0].text, first + second)
        self.assertEqual(parsed[0].text.count("<table>"), 1)
        self.assertIn("intervening page metadata", parsed[0].text)

    def test_identical_tables_at_distinct_offsets_are_both_retained(self):
        prefix = "Policy \U0001f4c4\n"
        markup = "<table><tr><td>Retain the receipt.</td></tr></table>"
        suffix = "\nEnd of policy."
        for separator in ("", "\nSupporting text.\n"):
            with self.subTest(separator=separator):
                offsets = [len(prefix), len(prefix) + len(markup) + len(separator)]
                content = prefix + markup + separator + markup + suffix
                result = native_layout(
                    content,
                    tables=[
                        {
                            "rowCount": 1,
                            "columnCount": 1,
                            "cells": [],
                            "spans": [{"offset": offset, "length": len(markup)}],
                        }
                        for offset in offsets
                    ],
                )
                page = document_parser.parse_layout_result(result)[0]
                self.assertEqual(page.text, content)
                tables = [block for block in page.blocks if block.kind == "table"]
                self.assertEqual([block.text for block in tables], [markup, markup])
                self.assertEqual([block.page_numbers for block in tables], [(0,), (0,)])

    def test_structured_column_headers_are_repeated_when_splitting(self):
        header = "<tr><th>Region</th><th>Requirement</th></tr>"
        rows = [
            f"<tr><td>Region {number}</td><td>Requirement {number}</td></tr>"
            for number in range(40)
        ]
        markup = "<table>" + header + "".join(rows) + "</table>"
        result = native_layout(
            markup,
            tables=[
                {
                    "rowCount": len(rows) + 1,
                    "columnCount": 2,
                    "cells": [
                        {
                            "rowIndex": row,
                            "columnIndex": column,
                            "kind": "columnHeader" if row == 0 else "content",
                            "content": label if row == 0 else f"{label} {row - 1}",
                        }
                        for row in range(len(rows) + 1)
                        for column, label in enumerate(("Region", "Requirement"))
                    ],
                    "spans": [{"offset": 0, "length": len(markup)}],
                }
            ],
        )
        pages = document_parser.parse_layout_result(result)
        self.assertEqual(pages[0].blocks[0].table_header_rows, 1)
        chunks = list(SentenceTextSplitter(100).split_pages(pages))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(header in chunk.text for chunk in chunks))
        combined = "".join(chunk.text for chunk in chunks)
        for row in rows:
            self.assertEqual(combined.count(row), 1)

    def test_section_names_do_not_determine_what_is_indexed(self):
        for heading in (
            "Employee acknowledgement",
            "Revision history",
            "Official reference",
            "Official references",
            "Työntekijän kuittaus",
            "Muutoshistoria",
            "僱員確認",
            "修訂紀錄",
            "官方參考",
        ):
            with self.subTest(heading=heading):
                content = f"## {heading}\nThe employee must retain the receipt."
                pages = document_parser.parse_layout_result(native_layout(content))
                self.assertEqual(pages[0].text, content)
                documents = build_search_documents(
                    pages=pages, source=policy_metadata()
                )
                self.assertEqual(documents[0]["heading_text"], heading)
                self.assertIn(
                    "The employee must retain the receipt.",
                    documents[0]["semantic_text"],
                )

    def test_unmarked_section_text_is_retained(self):
        text = "Official references\nThe employee must retain the receipt."
        self.assertEqual(
            document_parser.parse_layout_result(native_layout(text))[0].text, text
        )

    def test_page_comments_reach_raw_and_semantic_search_content(self):
        content = (
            '<!-- PageHeader="Eligibility &amp; coverage" -->\n'
            "## Employee acknowledgement\nAcknowledge within 5 days.\n"
            '<!-- PageFooter="Review within 30 days." -->\n'
            '<!-- PageNumber="1" -->\n<!-- PageBreak -->'
        )
        pages = document_parser.parse_layout_result(native_layout(content))
        self.assertEqual(pages[0].text, content)
        documents = build_search_documents(pages=pages, source=policy_metadata())
        raw_text = "\n".join(document["chunk_text"] for document in documents)
        semantic_text = "\n".join(document["semantic_text"] for document in documents)
        self.assertIn('<!-- PageHeader="Eligibility &amp; coverage" -->', raw_text)
        for text in (
            "Eligibility & coverage",
            "Acknowledge within 5 days.",
            "Review within 30 days.",
            'PageNumber="1"',
            "PageBreak",
        ):
            self.assertIn(text, semantic_text)
        self.assertNotIn("<!--", semantic_text)

    def test_long_page_comment_contents_survive_chunk_boundaries(self):
        sentences = [f"Condition {number} requires HR review." for number in range(120)]
        content = '<!-- PageHeader="' + " ".join(sentences) + '" -->'
        pages = document_parser.parse_layout_result(native_layout(content))
        documents = build_search_documents(pages=pages, source=policy_metadata())
        self.assertGreater(len(documents), 1)
        semantic_text = "\n".join(document["semantic_text"] for document in documents)
        for sentence in sentences:
            self.assertIn(sentence, semantic_text)
        for document in documents:
            self.assertLessEqual(token_count(document["semantic_text"]), 500)

    def test_multiple_page_spans_are_retained(self):
        page = SimpleNamespace(
            page_number=1,
            spans=[
                SimpleNamespace(offset=0, length=2),
                SimpleNamespace(offset=4, length=2),
            ],
            lines=[],
            width=8,
            height=11,
            unit="inch",
            angle=0,
        )
        result = SimpleNamespace(pages=[page], tables=[], content="abXXcd")
        pages = document_parser.parse_layout_result(result)
        self.assertEqual(pages[0].text, "abcd")

    def test_cross_page_table_keeps_both_page_numbers(self):
        markup = "<table>\n<tr><td>cross-page policy</td></tr>\n</table>"
        result = native_layout(
            "A" + markup + "F",
            page_break=20,
            tables=[
                {
                    "rowCount": 1,
                    "columnCount": 1,
                    "cells": [],
                    "spans": [{"offset": 1, "length": len(markup)}],
                }
            ],
        )
        parsed = document_parser.parse_layout_result(result)
        documents = build_search_documents(
            pages=parsed, source=policy_metadata()
        )
        table_chunks = [
            document
            for document in documents
            if "cross-page policy" in document["chunk_text"]
        ]
        self.assertEqual(len(table_chunks), 1)
        self.assertEqual(
            (table_chunks[0]["page_start"], table_chunks[0]["page_end"]), (1, 2)
        )
        self.assertIn("A", parsed[0].text)
        self.assertEqual(parsed[1].text, "F")

    def test_native_row_headers_are_not_repeated_as_column_headers(self):
        markup = (
            "<table>"
            + "".join(f"<tr><th>Policy {number}</th></tr>" for number in range(30))
            + "</table>"
        )
        result = native_layout(
            markup,
            tables=[
                {
                    "rowCount": 30,
                    "columnCount": 1,
                    "cells": [
                        {
                            "rowIndex": number,
                            "columnIndex": 0,
                            "kind": "rowHeader",
                            "content": f"Policy {number}",
                        }
                        for number in range(30)
                    ],
                    "spans": [{"offset": 0, "length": len(markup)}],
                }
            ],
        )
        pages = document_parser.parse_layout_result(result)
        self.assertEqual(pages[0].blocks[0].table_header_rows, 0)
        chunks = list(SentenceTextSplitter(40).split_pages(pages))
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunk.text for chunk in chunks).count("Policy 0<"), 1)

    def test_acknowledgement_cross_page_table_is_retained(self):
        heading = "## Employee acknowledgement\n"
        markup = "<table><tr><td>Signature deadline</td><td>Five days</td></tr></table>"
        content = heading + markup + "\nKeep a signed copy."
        pages = document_parser.parse_layout_result(
            native_layout(
                content,
                page_break=content.index("Five") + 2,
                tables=[
                    {
                        "rowCount": 1,
                        "columnCount": 2,
                        "cells": [],
                        "spans": [{"offset": len(heading), "length": len(markup)}],
                    }
                ],
            )
        )
        self.assertEqual([page.page_num for page in pages], [0, 1])
        documents = build_search_documents(pages=pages, source=policy_metadata())
        table_document = next(
            document for document in documents if markup in document["chunk_text"]
        )
        self.assertEqual(table_document["heading_text"], "Employee acknowledgement")
        self.assertEqual(
            (table_document["page_start"], table_document["page_end"]), (1, 2)
        )
        self.assertIn("Signature deadline | Five days", table_document["semantic_text"])

    def test_first_page_is_not_implicitly_discarded(self):
        content = "The policy applies from the first page."
        page = SimpleNamespace(
            page_number=1,
            spans=[SimpleNamespace(offset=0, length=len(content))],
            lines=[],
            width=8.5,
            height=11,
            unit="inch",
            angle=0,
        )
        client = Mock()
        client.begin_analyze_document.return_value.result.return_value = (
            SimpleNamespace(pages=[page], tables=[], content=content)
        )
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "policy.pdf"
            pdf.write_bytes(b"%PDF-test")
            pages = document_parser.analyze_local_pdf(client=client, path=pdf)
        self.assertEqual([entry.text for entry in pages], [content])

    def test_document_intelligence_uses_python_string_offsets(self):
        client = Mock()
        client.begin_analyze_document.return_value.result.return_value = (
            SimpleNamespace(pages=[], tables=[], content="")
        )
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "policy.pdf"
            pdf.write_bytes(b"%PDF-test")
            document_parser.analyze_local_pdf(client=client, path=pdf)
        self.assertEqual(
            client.begin_analyze_document.call_args.kwargs.get("string_index_type"),
            "unicodeCodePoint",
        )

    def test_html_table_is_flattened(self):
        actual = normalized_content(
            "<figure><table><tr><th>Stage</th><th>Timeline</th></tr>"
            "<tr><td>Inquiry</td><td>90 days</td></tr></table></figure>"
        )
        self.assertNotIn("<", actual)
        for value in ("Stage", "Timeline", "Inquiry", "90 days"):
            self.assertIn(value, actual)

    def test_semantic_text_includes_context(self):
        actual = build_semantic_text(
            document_title="India HR POSH",
            heading_text="Complaint process",
            policy_topic="Workplace conduct",
            chunk_text="<p>Inquiry target completion within 90 days.</p>",
        )
        self.assertEqual(
            actual,
            "Document: India HR POSH\n"
            "Section: Complaint process\n"
            "Policy topic: Workplace conduct\n"
            "Inquiry target completion within 90 days.",
        )

    def test_escaped_angle_brackets_are_policy_text(self):
        self.assertEqual(
            normalized_content("Use &lt;employee-id&gt; &amp; retain the receipt."),
            "Use <employee-id> & retain the receipt.",
        )

    def test_native_table_formatting_does_not_change_semantic_rows(self):
        compact = "<table><tr><th>Stage</th><th>Timing</th></tr><tr><td>Inquiry</td><td>From the source</td></tr></table>"
        formatted = compact.replace("><", ">\n  <")
        self.assertEqual(normalized_content(formatted), normalized_content(compact))
        self.assertEqual(
            normalized_content(formatted), "Stage | Timing\nInquiry | From the source"
        )

    def test_table_captions_and_inline_spacing_remain_readable(self):
        markup = (
            "<table><caption><b>Annual</b> <i>leave</i></caption>"
            "<tr><td><b>First</b> <i>rule</i></td></tr></table>"
        )
        self.assertEqual(normalized_content(markup), "Annual leave\nFirst rule")

    def test_native_table_sections_split_with_balanced_markup_and_caption(self):
        header = "<tr><th>Region</th><th>Rule</th></tr>"
        rows = [
            f"<tr><td>Region {number}</td><td>Rule {number}</td></tr>"
            for number in range(40)
        ]
        markup = (
            "<table><caption>Policy scope</caption><thead>"
            + header
            + "</thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
        chunks = list(SentenceTextSplitter(100).split_pages([Page(0, 0, markup)]))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertIn("<caption>Policy scope</caption>", chunk.text)
            self.assertIn(header, chunk.text)
            self.assertNotRegex(chunk.text, r"</?(?:thead|tbody|tfoot)")
            self.assertNotIn("<caption>", normalized_content(chunk.text))
        for row in rows:
            self.assertEqual("".join(chunk.text for chunk in chunks).count(row), 1)

    def test_cross_page_chunk_reports_all_source_pages(self):
        documents = build_search_documents(
            pages=[
                Page(page_num=0, offset=0, text="An employee may request"),
                Page(page_num=1, offset=23, text="additional leave."),
            ],
            source=policy_metadata(),
        )
        combined = next(
            entry for entry in documents if "additional leave" in entry["chunk_text"]
        )
        self.assertEqual((combined["page_start"], combined["page_end"]), (1, 2))

    def test_large_table_is_split_without_losing_rows(self):
        rows = [
            f"<tr><td>Entry {number}</td><td>Policy requirement {number}</td></tr>"
            for number in range(800)
        ]
        table = "<figure><table>" + "".join(rows) + "</table></figure>"
        chunks = list(
            SentenceTextSplitter().split_pages([Page(page_num=0, offset=0, text=table)])
        )
        self.assertGreater(len(chunks), 1)
        for row in rows:
            self.assertIn(row, "".join(chunk.text for chunk in chunks))

    def test_table_headers_and_rowspan_groups_survive_splitting(self):
        header = '<tr><th scope="col">Region</th><th scope="col">Rule</th></tr>'
        grouped = '<tr><td rowspan="2">Europe</td><td>First rule</td></tr><tr><td>Second rule</td></tr>'
        other_rows = "".join(
            f"<tr><td>Region {number}</td><td>Rule {number}</td></tr>"
            for number in range(100)
        )
        chunks = list(
            SentenceTextSplitter(100).split_pages(
                [
                    Page(
                        0,
                        0,
                        "<figure><table>"
                        + header
                        + grouped
                        + other_rows
                        + "</table></figure>",
                    ),
                ]
            )
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(header in chunk.text for chunk in chunks))
        self.assertTrue(any(grouped in chunk.text for chunk in chunks))

    def test_uppercase_table_markup_can_be_split(self):
        table = (
            "<TABLE>"
            + "".join(f"<TR><TD>Rule {number}</TD></TR>" for number in range(30))
            + "</TABLE>"
        )
        chunks = list(SentenceTextSplitter(40).split_pages([Page(0, 0, table)]))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.text.startswith("<TABLE>") for chunk in chunks))

    def test_header_rowspan_into_body_is_kept_as_one_table(self):
        table = '<table><tr><th rowspan="2">Policy</th><th>Rule</th></tr><tr><td>First</td></tr>'
        table += "".join(
            f"<tr><td>Row {number}</td><td>Rule</td></tr>" for number in range(10)
        )
        table += "</table>"
        chunks = list(SentenceTextSplitter(40).split_pages([Page(0, 0, table)]))
        self.assertEqual([chunk.text for chunk in chunks], [table])

    def test_indivisible_oversized_table_fails_before_embedding(self):
        table = (
            "<table><tr><td>"
            + "policy " * (EMBEDDING_TOKEN_LIMIT + 10)
            + "</td></tr></table>"
        )
        with self.assertRaisesRegex(ValueError, "indivisible table row"):
            list(SentenceTextSplitter().split_pages([Page(0, 0, table)]))

    def test_enriched_text_obeys_token_budget(self):
        pages = [
            Page(0, 0, "## Leave\n" + "A policy sentence applies to employees. " * 500)
        ]
        documents = build_search_documents(pages=pages, source=policy_metadata())
        self.assertGreater(len(documents), 1)
        self.assertTrue(
            all(token_count(document["semantic_text"]) <= 500 for document in documents)
        )
        self.assertTrue(
            all(document["heading_text"] == "Leave" for document in documents)
        )

    def test_literal_special_tokens_and_cjk_are_not_dropped(self):
        text = "Policy <|endoftext|>. 香港的政策。\u00e4\u00f6\u00e5"
        chunks = list(SentenceTextSplitter(20).split_pages([Page(0, 0, text)]))
        self.assertIn("<|endoftext|>", "".join(chunk.text for chunk in chunks))
        self.assertIn("香港的政策", "".join(chunk.text for chunk in chunks))
        self.assertTrue(all(token_count(chunk.text) <= 20 for chunk in chunks))

    def test_heading_and_page_changes_create_different_keys(self):
        first = build_search_documents(
            pages=[Page(0, 0, "Policy applies.")], source=policy_metadata()
        )
        moved = build_search_documents(
            pages=[Page(1, 0, "Policy applies.")], source=policy_metadata()
        )
        renamed = build_search_documents(
            pages=[Page(0, 0, "Policy applies.")],
            source={**policy_metadata(), "name": "Renamed_Policy.pdf"},
        )
        self.assertNotEqual(first[0]["chunk_id"], moved[0]["chunk_id"])
        self.assertNotEqual(first[0]["chunk_id"], renamed[0]["chunk_id"])

    def test_small_chunk_budget_fails_instead_of_looping(self):
        splitter = SentenceTextSplitter(
            1, measure_input=lambda text, heading: 2 + token_count(text)
        )
        with self.assertRaisesRegex(ValueError, "no token budget"):
            list(splitter.split_pages([Page(0, 0, "Policy text")]))


class AzureContractTests(unittest.TestCase):
    def test_knowledge_source_searches_text_and_vector_fields(self):
        schema = index_schema(
            index_name="policies",
            openai_endpoint="https://example.openai.azure.com",
            embedding_deployment="embedding",
            dimensions=1024,
        )
        source = knowledge_source_definition(name="policies-ks", index_name="policies")
        self.assertEqual(
            {
                field["name"]
                for field in source["searchIndexParameters"]["searchFields"]
            },
            {field["name"] for field in schema["fields"] if field.get("searchable")},
        )

    def test_embedding_indices_must_match_inputs(self):
        response = {
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 0, "embedding": [0.3, 0.4]},
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "index|indices"):
            search_index.embed_documents(
                client=embedding_client(response),
                documents=[{"semantic_text": "one"}, {"semantic_text": "two"}],
                deployment="embedding",
                dimensions=2,
            )

    def test_missing_index_acknowledgements_are_not_success(self):
        def send(request, **kwargs):
            return http_response(
                request, search_key_schema() if request.method == "GET" else {}
            )

        with (
            patch("requests.Session.send", side_effect=send),
            self.assertRaisesRegex(RuntimeError, "acknowledg|response"),
        ):
            search_index.upload_documents(
                sender_factory=buffered_sender_factory(),
                documents=[{"chunk_id": "one", "chunk_text": "policy"}],
            )

    def test_retry_after_http_date_is_supported(self):
        failure = urllib.error.HTTPError(
            "https://example.test",
            429,
            "throttled",
            {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
            io.BytesIO(b"{}"),
        )
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status, response.headers = 200, {}
        response.read.return_value = json.dumps({"ok": True}).encode()
        with (
            patch("urllib.request.urlopen", side_effect=[failure, response]),
            patch.object(azure_http.time, "sleep"),
        ):
            self.assertEqual(
                azure_http.request_json("GET", "https://example.test")[2], {"ok": True}
            )


if __name__ == "__main__":
    unittest.main()

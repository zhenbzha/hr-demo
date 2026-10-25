"""Build Search documents and normalize their text for semantic retrieval."""

from __future__ import annotations

import hashlib
import json
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar

from .page import Page
from .textsplitter import EMBEDDING_TOKEN_LIMIT, SentenceTextSplitter, token_count


def clean_text(value: str) -> str:
    """Collapse whitespace runs to single spaces and strip outer whitespace."""
    return re.sub(r"\s+", " ", value).strip()


class SemanticTextParser(HTMLParser):
    """Extract searchable text from Document Intelligence HTML.

    Separate table cells with pipes and retain HTML comment contents,
    including page headers and footers.
    """

    BLOCK_TAGS: ClassVar[set[str]] = {
        "br",
        "caption",
        "div",
        "figcaption",
        "figure",
        "li",
        "p",
        "table",
        "tbody",
        "tfoot",
        "thead",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.cells_in_row = 0
        self.table_depth = 0
        self.in_cell = False
        self.in_caption = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag == "table":
            self.table_depth += 1
        elif tag == "caption":
            self.in_caption = True
        if tag in {"td", "th"}:
            self.in_cell = True
            if self.cells_in_row:
                self.parts.append(" | ")
            self.cells_in_row += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "tr":
                self.cells_in_row = 0
        elif tag not in {"b", "strong", "em", "i", "span", "a"}:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self.table_depth = max(0, self.table_depth - 1)
        elif tag in {"td", "th"}:
            self.in_cell = False
        elif tag == "caption":
            self.in_caption = False
        if tag in self.BLOCK_TAGS or tag in {"td", "th"}:
            self.parts.append("\n" if tag in self.BLOCK_TAGS else " ")
        elif tag not in {"b", "strong", "em", "i", "span", "a"}:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if (
            self.table_depth
            and not self.in_cell
            and not self.in_caption
            and not data.strip()
        ):
            return
        self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        """Append decoded HTML comment text to the extracted content."""
        self.parts.extend(("\n", unescape(data), "\n"))

    def text(self) -> str:
        """Return normalized lines without Markdown list or heading markers."""
        lines: list[str] = []
        for line in "".join(self.parts).splitlines():
            normalized = clean_text(line)
            normalized = re.sub(r"^#{1,6}\s*", "", normalized)
            normalized = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", normalized)
            if normalized:
                lines.append(normalized)
        return "\n".join(lines)


def normalized_content(value: str) -> str:
    """Convert Document Intelligence Markdown/HTML into semantic plain text."""
    parser = SemanticTextParser()
    parser.feed(value)
    parser.close()
    return parser.text()


def build_semantic_text(
    *,
    document_title: str,
    heading_text: str,
    policy_topic: str,
    chunk_text: str,
) -> str:
    """Return ranking text with title, section, topic, and normalized content."""
    content = normalized_content(chunk_text)
    return "\n".join(
        [
            f"Document: {clean_text(document_title)}",
            f"Section: {clean_text(heading_text)}",
            f"Policy topic: {clean_text(policy_topic)}",
            content,
        ]
    ).strip()


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def base_document(
    *,
    manifest_item: dict[str, Any],
    ordinal: int,
    text: str,
    heading_text: str,
    pages: list[int],
) -> dict[str, Any]:
    """Build a Search document with retrieval and citation metadata.

    The pages argument uses one-based physical page numbers. Derive the
    chunk ID from the source hash, ordinal, text, and metadata.
    """
    document_title = Path(manifest_item["name"]).stem.replace("_", " ")
    document = {
        "document_id": manifest_item["documentId"],
        "document_title": document_title,
        "region": manifest_item["region"],
        "language": manifest_item["language"],
        "policy_topic": manifest_item["policyTopic"],
        "version": manifest_item["version"],
        "source_uri": manifest_item["sourceUrl"],
        "document_path": manifest_item["documentPath"],
        "annotation_path": manifest_item["annotationPath"],
        "heading_text": heading_text,
        "chunk_text": text,
        "page_start": min(pages) if pages else None,
        "page_end": max(pages) if pages else None,
    }
    document["semantic_text"] = build_semantic_text(
        document_title=document_title,
        heading_text=heading_text,
        policy_topic=manifest_item["policyTopic"],
        chunk_text=text,
    )
    if token_count(document["semantic_text"]) > EMBEDDING_TOKEN_LIMIT:
        raise ValueError("Enriched semantic text exceeds the embedding input limit")
    identity = json.dumps(
        {"source_sha256": manifest_item["sha256"], "ordinal": ordinal, **document},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    document["chunk_id"] = content_hash(identity)[:40]
    return document


def build_search_documents(
    *,
    pages: list[Page],
    manifest_item: dict[str, Any],
) -> list[dict[str, Any]]:
    """Split analyzed pages into Search documents with citation metadata.

    Measure chunk budgets against the full embedding text, including
    document and section metadata.
    """
    title = Path(manifest_item["name"]).stem.replace("_", " ")

    def measure_input(text: str, heading: str) -> int:
        return token_count(
            build_semantic_text(
                document_title=title,
                heading_text=heading or title,
                policy_topic=manifest_item["policyTopic"],
                chunk_text=text,
            )
        )

    splitter = SentenceTextSplitter(
        max_tokens_per_section=500, measure_input=measure_input
    )
    documents: list[dict[str, Any]] = []

    for ordinal, chunk in enumerate(splitter.split_pages(pages)):
        text = chunk.text.strip()
        if not text:
            continue
        documents.append(
            base_document(
                manifest_item=manifest_item,
                ordinal=ordinal,
                text=text,
                heading_text=chunk.heading or title,
                pages=[page_number + 1 for page_number in chunk.page_numbers],
            )
        )
    return documents

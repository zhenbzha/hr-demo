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
    """
    Extract searchable text from Document Intelligence HTML.

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
        """
        Flatten table structure while retaining unrecognized markup.

        Block tags start new lines and cells use pipe separators. Known
        inline formatting tags are discarded; other tags remain literal
        text so normalization does not erase unrecognized source content.

        Parameters
        ----------
        tag : str
            Lowercase HTML tag name supplied by HTMLParser.
        attrs : list of tuple
            Parsed attributes, unused because retained tags use their
            original source markup.
        """
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
        """
        Close table context and mirror start-tag text normalization.

        End block tags with newlines and cells with spaces. Discard known
        inline formatting tags, but retain other closing tags literally.

        Parameters
        ----------
        tag : str
            Lowercase HTML tag name supplied by HTMLParser.
        """
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
        """Ignore table-layout whitespace outside cells and captions only."""
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
    """
    Add document and section labels to the text used for search.

    Parameters
    ----------
    document_title : str
        Title of the PDF.
    heading_text : str
        Section heading, or the document title when no heading is available.
    policy_topic : str
        Topic text derived from the PDF filename.
    chunk_text : str
        Extracted Markdown text or HTML table content to normalize.

    Returns
    -------
    str
        Normalized passage text preceded by its document title, section
        heading, and topic. All of this text counts toward the token limit.
    """
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
    """Return the SHA-256 hexadecimal digest of UTF-8 text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chunk_id(
    *, document: dict[str, Any], source_sha256: str, ordinal: int
) -> str:
    """
    Hash canonical record JSON with the source fingerprint and chunk ordinal.

    Parameters
    ----------
    document : dict
        Search record fields, including semantic text, before adding its
        chunk ID or embedding.
    source_sha256 : str
        SHA-256 fingerprint of the source PDF.
    ordinal : int
        Zero-based passage position in the PDF.

    Returns
    -------
    str
        First 40 hexadecimal characters of the UTF-8 JSON content hash.
        Sorted keys, compact separators, and unescaped Unicode keep the
        serialized identity deterministic.
    """
    identity = json.dumps(
        {"source_sha256": source_sha256, "ordinal": ordinal, **document},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return content_hash(identity)[:40]


def base_document(
    *,
    source: dict[str, Any],
    ordinal: int,
    text: str,
    heading_text: str,
    pages: list[int],
) -> dict[str, Any]:
    """
    Build a Search record for a passage of PDF text.

    Parameters
    ----------
    source : dict
        PDF file record returned by ``discover_sources``.
    ordinal : int
        Position of the passage in the PDF, starting at zero.
        Used to generate the search record's ID.
    text : str
        Extracted policy passage retained as ``chunk_text``.
    heading_text : str
        Section heading, or the document title when no heading is available.
    pages : list of int
        PDF page numbers containing the passage, starting at one.

    Returns
    -------
    dict
        Search record containing the passage, text for embedding, document
        ID, region, and page numbers. Includes paths to the stored PDF and
        the page data used to highlight citations.

    Raises
    ------
    ValueError
        Text prepared for embedding exceeds the model's token limit.
    """
    document_title = Path(source["name"]).stem.replace("_", " ")
    document = {
        "document_id": source["documentId"],
        "document_title": document_title,
        "region": source["region"],
        "language": source["language"],
        "policy_topic": source["policyTopic"],
        "version": source["version"],
        "source_uri": source["documentPath"],
        "document_path": source["documentPath"],
        "annotation_path": source["annotationPath"],
        "heading_text": heading_text,
        "chunk_text": text,
        "page_start": min(pages) if pages else None,
        "page_end": max(pages) if pages else None,
    }
    document["semantic_text"] = build_semantic_text(
        document_title=document_title,
        heading_text=heading_text,
        policy_topic=source["policyTopic"],
        chunk_text=text,
    )
    if token_count(document["semantic_text"]) > EMBEDDING_TOKEN_LIMIT:
        raise ValueError("Enriched semantic text exceeds the embedding input limit")
    document["chunk_id"] = _chunk_id(
        document=document, source_sha256=source["sha256"], ordinal=ordinal
    )
    return document


def build_search_documents(
    *,
    pages: list[Page],
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Split PDF text into passages for Azure AI Search.

    Target 500 tokens per passage, including the title, heading, and topic
    added for embedding. Table row groups that must stay together may exceed
    this target, up to the embedding model's input limit.

    Parameters
    ----------
    pages : list of Page
        Analyzed pages from one PDF, in reading order.
    source : dict
        File record for the PDF, returned by ``discover_sources``.

    Returns
    -------
    list of dict
        One search record per passage. ``chunk_text`` contains the extracted
        text; ``semantic_text`` contains the text prepared for embedding.
        PDF page numbers for citations start at one.
    """
    title = Path(source["name"]).stem.replace("_", " ")

    def measure_input(text: str, heading: str) -> int:
        return token_count(
            build_semantic_text(
                document_title=title,
                heading_text=heading or title,
                policy_topic=source["policyTopic"],
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
                source=source,
                ordinal=ordinal,
                text=text,
                heading_text=chunk.heading or title,
                pages=[page_number + 1 for page_number in chunk.page_numbers],
            )
        )
    return documents

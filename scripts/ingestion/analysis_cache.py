"""Cache parsed document pages with source and parser version checks."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from azure.ai.documentintelligence import DocumentIntelligenceClient
from json_output import write_json_atomic

from .document_parser import (
    ANALYSIS_API_VERSION,
    MODEL_ID,
    PARSER_VERSION,
    STRING_INDEX_TYPE,
    analyze_local_pdf,
)
from .page import ContentBlock, Page, TextLine
from .sources import verify_source

ANALYSIS_CACHE_SCHEMA_VERSION = 4


def _cache_metadata(
    document_id: str,
    document_version: str,
    source_sha256: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": ANALYSIS_CACHE_SCHEMA_VERSION,
        "parserVersion": PARSER_VERSION,
        "documentId": document_id,
        "documentVersion": document_version,
        "sourceSha256": source_sha256,
        "modelId": MODEL_ID,
        "apiVersion": ANALYSIS_API_VERSION,
        "contentFormat": "markdown",
        "stringIndexType": STRING_INDEX_TYPE,
    }


def _read_pages(payload: dict[str, Any]) -> list[Page]:
    if not isinstance(payload.get("pages"), list):
        raise ValueError("Analysis cache has no pages array")
    pages = []
    previous_page = -1
    for item in payload["pages"]:
        blocks = [
            ContentBlock(**{**block, "page_numbers": tuple(block["page_numbers"])})
            for block in item["blocks"]
        ]
        page = Page(
            **{
                **item,
                "blocks": blocks,
                "lines": [TextLine(**line) for line in item["lines"]],
            }
        )
        if type(page.page_num) is not int or page.page_num <= previous_page:
            raise ValueError("Analysis cache page numbers must be unique and ascending")
        if "".join(block.text for block in blocks) != page.text:
            raise ValueError("Analysis cache blocks do not match the cached page text")
        if any(
            block.kind not in {"text", "table"}
            or not block.page_numbers
            or any(
                type(number) is not int or number < 0 for number in block.page_numbers
            )
            for block in blocks
        ):
            raise ValueError("Analysis cache contains invalid block provenance")
        if any(
            block.table_header_rows is not None
            and (
                block.kind != "table"
                or type(block.table_header_rows) is not int
                or block.table_header_rows < 0
            )
            for block in blocks
        ):
            raise ValueError("Analysis cache contains invalid table header metadata")
        pages.append(page)
        previous_page = page.page_num
    return pages


def _write_cache(path: Path, metadata: dict[str, Any], pages: list[Page]) -> None:
    write_json_atomic(
        path, {"metadata": metadata, "pages": [asdict(page) for page in pages]},
    )


def load_or_analyze_pages(
    *,
    client: DocumentIntelligenceClient,
    source: dict[str, Any],
    analysis_dir: Path,
) -> list[Page]:
    """
    Read saved PDF analysis or run Document Intelligence.

    Parameters
    ----------
    client : DocumentIntelligenceClient
        Client used to analyze the PDF.
    source : dict
        PDF file record returned by ``discover_sources``.
    analysis_dir : pathlib.Path
        Local directory for saved analysis. Missing, outdated, or damaged
        results are regenerated automatically.

    Returns
    -------
    list of Page
        Pages containing extracted text, tables, and text coordinates.

    Raises
    ------
    ValueError
        The PDF changed after discovery.
    """
    path = Path(source["localPath"])
    verify_source(path, source["sha256"])
    output_path = (
        analysis_dir / source["documentId"] / source["version"]
        / f"{source['sha256']}.json"
    )
    metadata = _cache_metadata(
        source["documentId"], source["version"], source["sha256"],
    )
    if output_path.exists():
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("metadata") == metadata:
                pages = _read_pages(payload)
                if pages:
                    return pages
        except (ValueError, KeyError, TypeError):
            pass
    pages = analyze_local_pdf(client=client, path=path)
    verify_source(path, source["sha256"])
    _write_cache(output_path, metadata, pages)
    return pages

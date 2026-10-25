"""Cache parsed document pages with source and parser version checks."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from azure.ai.documentintelligence import DocumentIntelligenceClient

from .document_parser import (
    ANALYSIS_API_VERSION,
    MODEL_ID,
    PARSER_VERSION,
    STRING_INDEX_TYPE,
    analyze_local_pdf,
)
from .manifest import verify_source
from .page import ContentBlock, Page, TextLine

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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary_path = Path(output.name)
            json.dump(
                {"metadata": metadata, "pages": [asdict(page) for page in pages]},
                output,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def load_or_analyze_pages(
    *,
    client: DocumentIntelligenceClient,
    path: Path,
    output_path: Path,
    reuse_analysis: bool,
    retain_analysis: bool,
    document_id: str,
    document_version: str,
    source_sha256: str,
) -> list[Page]:
    """Load matching cached pages or analyze the local PDF.

    An existing cache requested for reuse must match the source and parser
    settings; a mismatch raises ValueError. Save newly analyzed pages to
    output_path only when retain_analysis is True.
    """
    verify_source(path, source_sha256)
    metadata = _cache_metadata(document_id, document_version, source_sha256)
    if reuse_analysis and output_path.exists():
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("metadata") != metadata:
            raise ValueError(
                f"Analysis cache is stale for {document_id} v{document_version}: {output_path}. "
                "Reanalyze without --reuse-analysis."
            )
        return _read_pages(payload)
    pages = analyze_local_pdf(client=client, path=path)
    verify_source(path, source_sha256)
    if retain_analysis:
        _write_cache(output_path, metadata, pages)
    return pages

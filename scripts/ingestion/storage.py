"""Store PDFs and the page data used to highlight citations in ADLS Gen2."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from azure.storage.filedatalake import (
    ContentSettings,
    DataLakeFileClient,
    FileSystemClient,
)

from .page import Page


def _page_annotation(page: Page) -> dict[str, Any]:
    """Prepare one PDF page's dimensions and text positions for highlighting."""
    lines = []
    for line in page.lines:
        lines.append(
            {
                "text": line.text,
                "polygon": line.polygon,
                "spanOffset": line.span_offset,
                "spanLength": line.span_length,
            }
        )
    return {
        "width": page.width,
        "height": page.height,
        "unit": page.unit,
        "angle": page.angle,
        "lines": lines,
    }


def annotation_payload(
    *, source: dict[str, Any], pages: list[Page]
) -> dict[str, Any]:
    """
    Prepare page sizes and text coordinates for citation highlighting.

    Parameters
    ----------
    source : dict
        PDF file record returned by ``discover_sources``.
    pages : list of Page
        Analyzed PDF pages containing text lines and their coordinates.

    Returns
    -------
    dict
        Document details and a ``pages`` dictionary containing page sizes,
        orientation, text lines, and coordinates. Page keys are strings
        starting at ``"1"``. Text offsets refer to the complete Document
        Intelligence analysis result.
    """
    return {
        "documentId": source["documentId"],
        "documentTitle": Path(source["name"]).stem.replace("_", " "),
        "version": source["version"],
        "sha256": source["sha256"],
        "pages": {str(page.page_num + 1): _page_annotation(page) for page in pages},
    }


def _ensure_directory(file_system: FileSystemClient, path: str) -> None:
    current = ""
    for segment in path.split("/"):
        current = f"{current}/{segment}".strip("/")
        try:
            file_system.get_directory_client(current).create_directory()
        except ResourceExistsError:
            continue


def _upload_versioned_file(
    file_client: DataLakeFileClient,
    content: bytes,
    metadata: dict[str, str],
    content_type: str,
) -> None:
    """
    Upload a file or verify that the existing file has the same content.

    Compare JSON in canonical form and other files byte for byte.
    Raise ValueError if existing content differs; keep that file unchanged.
    """
    try:
        # The SDK needs overwrite=True for metadata; IfMissing prevents replacement.
        file_client.upload_data(
            content,
            overwrite=True,
            match_condition=MatchConditions.IfMissing,
            metadata=metadata,
            content_settings=ContentSettings(content_type=content_type),
        )
    except (ResourceExistsError, ResourceModifiedError):
        existing = file_client.download_file().readall()
        if content_type.startswith("application/json"):
            existing = _canonical_json(json.loads(existing))
            content = _canonical_json(json.loads(content))
        if existing != content:
            raise ValueError(
                f"Stored {content_type} for {metadata['documentid']} v{metadata['version']} differs. "
                "Check the source and parser fingerprint; existing citation artifacts will not be overwritten."
            ) from None


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _upload_document(
    file_system: FileSystemClient, item: dict[str, Any], annotation: dict[str, Any]
) -> None:
    pdf = Path(item["localPath"]).read_bytes()
    if hashlib.sha256(pdf).hexdigest() != item["sha256"]:
        raise ValueError(f"PDF changed after analysis: {item['documentId']}")
    metadata = {
        "documentid": item["documentId"],
        "region": item["region"],
        "language": item["language"],
        "version": item["version"],
        "sha256": item["sha256"],
    }
    artifacts = (
        (item["documentPath"], pdf, "application/pdf"),
        (
            item["annotationPath"],
            _canonical_json(annotation),
            "application/json; charset=utf-8",
        ),
    )
    for path, content, content_type in artifacts:
        _ensure_directory(file_system, path.rsplit("/", 1)[0])
        _upload_versioned_file(
            file_system.get_file_client(path), content, metadata, content_type
        )


def upload_adls_content(
    *,
    file_system: FileSystemClient,
    source: dict[str, Any],
    annotation: dict[str, Any],
) -> None:
    """
    Upload one PDF and its citation-highlighting data to Azure Data Lake Storage.

    Check existing files against the supplied content. A mismatch raises an
    error and leaves the stored file unchanged.

    Parameters
    ----------
    file_system : FileSystemClient
        Client for the destination filesystem, which is created if absent.
        The caller is responsible for closing the client.
    source : dict
        One PDF record returned by ``discover_sources``, including the file's
        local path and upload destinations.
    annotation : dict
        Citation-highlighting data returned by ``analyze_document`` for this PDF.

    Raises
    ------
    ValueError
        A PDF changed after analysis, or an existing file differs from
        the content being uploaded to the same path.
    """
    with suppress(ResourceExistsError):
        file_system.create_file_system()
    _upload_document(file_system, source, annotation)
    print(f"Verified PDF and annotations for {source['documentId']}")

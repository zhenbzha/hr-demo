"""Validate document manifests and resolve source and artifact paths."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SUPPORTED_REGIONS = {"global", "india", "finland", "hongkong"}
SUPPORTED_LANGUAGES = {"en", "fi", "zh-Hant"}
TEXT_FIELDS = {
    "region",
    "language",
    "documentId",
    "policyTopic",
    "version",
    "effectiveDate",
    "reviewDate",
    "name",
    "localPath",
    "sourceUrl",
    "sha256",
}
PATH_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def verify_source(path: Path, expected_sha256: str) -> None:
    """Raise ValueError if the file does not match its expected SHA-256."""
    with path.open("rb") as source:
        actual_sha256 = hashlib.file_digest(source, "sha256").hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Source SHA-256 mismatch for {path.name}; update the source version and manifest"
        )


def _validate_document(item: Any) -> None:
    if not isinstance(item, dict):
        raise TypeError("Every manifest document must be an object")
    if item.keys() & {"excludePages", "excludeSections"}:
        raise ValueError(
            "Document exclusions are not supported; remove excludePages and "
            "excludeSections to index all extracted content"
        )
    invalid_fields = sorted(
        field
        for field in TEXT_FIELDS
        if not isinstance(item.get(field), str) or not item[field].strip()
    )
    if invalid_fields:
        raise ValueError(f"Missing or invalid manifest fields: {invalid_fields}")
    if type(item.get("isActive")) is not bool:
        raise ValueError("isActive must be a JSON boolean")
    if (
        item["region"] not in SUPPORTED_REGIONS
        or item["language"] not in SUPPORTED_LANGUAGES
    ):
        raise ValueError(f"Unsupported region/language for {item['documentId']}")
    for field in ("documentId", "version"):
        if not PATH_SEGMENT_PATTERN.fullmatch(item[field]):
            raise ValueError(f"{field} must be a safe, nonempty path segment")
    if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
        raise ValueError("sha256 must contain 64 lowercase hexadecimal characters")
    source_url = urlsplit(item["sourceUrl"])
    if (
        source_url.scheme != "https"
        or not source_url.hostname
        or source_url.username
        or source_url.password
    ):
        raise ValueError("sourceUrl must be an absolute HTTPS URL without credentials")
    dates = [
        datetime.fromisoformat(item[field]) for field in ("effectiveDate", "reviewDate")
    ]
    if any(value.tzinfo is None for value in dates) or dates[1] < dates[0]:
        raise ValueError(
            "Policy dates must include time zones; reviewDate cannot precede effectiveDate"
        )
    if Path(item["localPath"]).suffix.lower() != ".pdf":
        raise ValueError("The layout ingestion pipeline accepts PDF source files")


def load_manifest(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return the validated manifest and resolved active document entries.

    Resolve documentRoot relative to the manifest and localPath relative
    to documentRoot. Active entries are copies with absolute source paths
    and versioned artifact paths; the returned manifest keeps its original
    paths. Reject invalid metadata, source hash mismatches, and an empty
    active document set.
    """
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("documents"), list
    ):
        raise TypeError("Manifest must be an object with a documents array")
    if "ingestion" in manifest:
        raise ValueError(
            "Manifest ingestion exclusions are not supported; remove ingestion "
            "to index all extracted content"
        )
    root_value = manifest.get("documentRoot", ".")
    if not isinstance(root_value, str) or not root_value:
        raise ValueError(
            "documentRoot must be a nonempty path relative to the manifest"
        )
    document_root = (manifest_path.parent / root_value).resolve()
    document_ids = set()
    active_items = []
    for source_item in manifest["documents"]:
        _validate_document(source_item)
        document_id = source_item["documentId"]
        if document_id in document_ids:
            raise ValueError(f"Duplicate document ID {document_id}")
        document_ids.add(document_id)
        item = dict(source_item)
        if not item["isActive"]:
            continue
        local_path = (document_root / item["localPath"]).resolve()
        verify_source(local_path, item["sha256"])
        item["localPath"] = str(local_path)
        item["documentPath"] = f"documents/{document_id}/{item['version']}.pdf"
        item["annotationPath"] = f"annotations/{document_id}/{item['version']}.json"
        active_items.append(item)
    if not active_items:
        raise ValueError(
            "The manifest has no active documents; refusing to clear an index"
        )
    return manifest, active_items

"""Store versioned PDFs, citation annotations, and manifests in ADLS Gen2."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from azure.core import MatchConditions
from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
from azure.storage.filedatalake import (
    ContentSettings,
    DataLakeFileClient,
    DataLakeServiceClient,
    FileSystemClient,
)

from .page import Page


def annotation_payload(
    *, manifest_item: dict[str, Any], pages: list[Page]
) -> dict[str, Any]:
    """Build citation annotations with one-based pages and OCR geometry.

    Polygons use each page's reported unit. Span offsets refer to the
    Document Intelligence analysis text.
    """
    return {
        "documentId": manifest_item["documentId"],
        "documentTitle": Path(manifest_item["name"]).stem.replace("_", " "),
        "version": manifest_item["version"],
        "sha256": manifest_item["sha256"],
        "pages": {
            str(page.page_num + 1): {
                "width": page.width,
                "height": page.height,
                "unit": page.unit,
                "angle": page.angle,
                "lines": [
                    {
                        "text": line.text,
                        "polygon": line.polygon,
                        "spanOffset": line.span_offset,
                        "spanLength": line.span_length,
                    }
                    for line in page.lines
                ],
            }
            for page in pages
        },
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
    """Create an artifact only if absent, or verify the existing version.

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
                "Use a new manifest version; existing citation artifacts will not be overwritten."
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
    endpoint: str,
    file_system_name: str,
    credential: TokenCredential,
    active_items: list[dict[str, Any]],
    annotations: dict[str, dict[str, Any]],
) -> None:
    """Upload and verify active PDFs and their citation annotations in ADLS."""
    with DataLakeServiceClient(account_url=endpoint, credential=credential) as service:
        file_system = service.get_file_system_client(file_system_name)
        with suppress(ResourceExistsError):
            file_system.create_file_system()
        for item in active_items:
            _upload_document(file_system, item, annotations[item["documentId"]])
            print(f"Verified PDF and annotations for {item['documentId']}")


def publish_manifest(
    *,
    endpoint: str,
    file_system_name: str,
    credential: TokenCredential,
    manifest: dict[str, Any],
    active_items: list[dict[str, Any]],
) -> None:
    """Publish the source inventory to ADLS without local filesystem paths.

    Overwrite the published manifest with the current inventory and
    versioned citation paths. Call this only after verifying indexing.
    """
    artifact_fields = {
        item["documentId"]: {
            key: item[key] for key in ("documentPath", "annotationPath")
        }
        for item in active_items
    }
    inventory = {
        **{
            key: value
            for key, value in manifest.items()
            if key not in {"documents", "documentRoot"}
        },
        "documents": [
            {
                **{key: value for key, value in item.items() if key != "localPath"},
                **artifact_fields.get(item["documentId"], {}),
            }
            for item in manifest["documents"]
        ],
    }
    with DataLakeServiceClient(account_url=endpoint, credential=credential) as service:
        file_system = service.get_file_system_client(file_system_name)
        _ensure_directory(file_system, "manifests")
        file_system.get_file_client("manifests/manifest.json").upload_data(
            json.dumps(inventory, ensure_ascii=False, indent=2, allow_nan=False).encode(
                "utf-8"
            ),
            overwrite=True,
            content_settings=ContentSettings(
                content_type="application/json; charset=utf-8"
            ),
        )

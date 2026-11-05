"""Keep per-PDF ingestion progress in a local JSON file."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from json_output import write_json_atomic

STATE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class FileRecord:
    """
    Saved processing details for one PDF.

    Attributes
    ----------
    sha256 : str
        SHA-256 hash of the PDF bytes.
    version : str
        PDF and annotation version, including parser settings.
    chunk_count : int
        Number of chunks in the last successful update, for local reporting.
    """

    sha256: str
    version: str
    chunk_count: int


@dataclass
class SavedFileRecords:
    """
    Contents of the local JSON file used to remember processed PDFs.

    Attributes
    ----------
    settings : dict
        Last successfully applied processing configuration and destinations.
    files : dict of FileRecord
        Successfully processed PDFs, keyed by document ID.
    pending_document_ids : list of str
        PDFs whose updates or deletions must be retried. These IDs remain
        recorded even if a PDF is removed after an interrupted upload.
    """

    settings: dict[str, Any]
    files: dict[str, FileRecord]
    pending_document_ids: list[str]


def _read_files(records: dict[str, Any]) -> dict[str, FileRecord]:
    """Validate saved file hashes and processing details before reusing them."""
    files = {}
    for document_id, record in records.items():
        if not document_id or not isinstance(record, dict):
            raise ValueError("Invalid saved PDF record")
        file = FileRecord(**record)
        if (
            not isinstance(file.sha256, str)
            or len(file.sha256) != 64
            or any(character not in "0123456789abcdef" for character in file.sha256)
            or not isinstance(file.version, str)
            or not file.version
            or type(file.chunk_count) is not int
            or file.chunk_count <= 0
        ):
            raise ValueError("Invalid saved PDF details")
        files[document_id] = file
    return files


def load_file_records(path: Path, settings: dict[str, Any]) -> SavedFileRecords:
    """
    Read the previous run's local per-file processing records.

    Parameters
    ----------
    path : pathlib.Path
        Local ingestion-state JSON file.
    settings : dict
        Current processing configuration and service destinations.

    Returns
    -------
    SavedFileRecords
        Saved records and last applied settings for the same Search target.
        The caller decides which changes require PDF processing and saves new
        settings only after the index update succeeds.
        Missing or invalid state reprocesses current PDFs, but cannot identify
        previously removed PDFs. Filesystem access errors are propagated.
    """
    empty = SavedFileRecords(settings=settings, files={}, pending_document_ids=[])
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("Ingestion state must be an object")
        if (
            type(payload.get("schema_version")) is not int
            or payload["schema_version"] != STATE_SCHEMA_VERSION
        ):
            raise ValueError("Unsupported ingestion state version")
        if (
            not isinstance(payload.get("settings"), dict)
            or not isinstance(payload.get("files"), dict)
            or not isinstance(payload.get("pending_document_ids"), list)
        ):
            raise ValueError("Invalid ingestion state")
        pending = payload["pending_document_ids"]
        if any(not isinstance(key, str) or not key for key in pending):
            raise ValueError("Invalid pending PDF IDs")
        saved_settings = payload["settings"]
        if any(
            saved_settings.get(name) != settings.get(name)
            for name in ("search_endpoint", "index_name")
        ):
            return empty
        files = _read_files(payload["files"])
        return SavedFileRecords(
            settings=saved_settings,
            files=files,
            pending_document_ids=pending,
        )
    except FileNotFoundError:
        return empty
    except (ValueError, TypeError):
        print(
            f"Invalid ingestion state in {path}; current PDFs will be processed. "
            "Previously removed PDFs require restored state or explicit cleanup."
        )
        return empty


def save_file_records(path: Path, records: SavedFileRecords) -> None:
    """
    Atomically save per-file ingestion progress.

    Parameters
    ----------
    path : pathlib.Path
        Local ingestion-state JSON file to create or replace.
    records : SavedFileRecords
        Last successful file records and the IDs of unfinished PDFs.
    """
    write_json_atomic(path, {"schema_version": STATE_SCHEMA_VERSION, **asdict(records)})

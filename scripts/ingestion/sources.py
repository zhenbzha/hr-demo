"""Find policy PDFs and collect their file details for ingestion."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .document_parser import (
    ANALYSIS_API_VERSION,
    MODEL_ID,
    PARSER_VERSION,
    STRING_INDEX_TYPE,
)

REGION_LANGUAGES = {
    "global": "en",
    "india": "en",
    "finland": "fi",
    "hongkong": "zh-Hant",
}
LANGUAGE_SUFFIXES = {"en": "en", "fi": "fi", "zh-HK": "zh-Hant", "zh-Hant": "zh-Hant"}


def _pdf_sha256(path: Path) -> str:
    """Hash the PDF contents without loading the whole file into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify_source(path: Path, expected_sha256: str) -> None:
    """
    Check that a PDF still matches its recorded content hash.

    Parameters
    ----------
    path : pathlib.Path
        PDF file to check.
    expected_sha256 : str
        SHA-256 hash recorded by ``discover_sources``.

    Raises
    ------
    ValueError
        The PDF changed after discovery. Restart ingestion to process it again.
    OSError
        The source cannot be opened or read.
    """
    actual_sha256 = _pdf_sha256(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"PDF changed after discovery: {path}; restart ingestion")


def _raise_walk_error(error: OSError) -> None:
    # Skipping an unreadable directory could delete its existing Search chunks.
    raise error


def _find_pdf_paths(data_dir: Path) -> list[Path]:
    """Find PDFs recursively, rejecting symbolic links and junctions."""
    paths = []
    for directory, subdirectories, names in data_dir.walk(on_error=_raise_walk_error):
        # walk() can list directory links as files instead of visiting their PDFs.
        for name in subdirectories + names:
            path = directory / name
            if path.is_symlink() or path.is_junction():
                raise ValueError(
                    f"Symbolic links and junctions are not supported: {path}; "
                    "use regular files and directories to avoid omitting sources"
                )
        paths.extend(
            directory / name for name in names if Path(name).suffix.lower() == ".pdf"
        )
    return sorted(paths, key=lambda path: path.relative_to(data_dir).as_posix())


def _document_version(path: Path, source_sha256: str) -> str:
    """
    Identify the PDF and citation-data version to store.

    Parameters
    ----------
    path : pathlib.Path
        PDF path whose filename contributes to the extracted document title.
    source_sha256 : str
        SHA-256 hash of the PDF contents.

    Returns
    -------
    str
        Version used in the PDF and JSON storage paths. Changes to the PDF,
        filename, or parser settings produce a new version.
    """
    version_details = (
        f"{source_sha256}:{PARSER_VERSION}:{ANALYSIS_API_VERSION}:"
        f"{MODEL_ID}:{STRING_INDEX_TYPE}:{path.stem}"
    )
    return hashlib.sha256(version_details.encode()).hexdigest()[:32]


def discover_sources(data_dir: Path) -> list[dict[str, Any]]:
    """
    Discover every PDF under the regional folders of a local data directory.

    Parameters
    ----------
    data_dir : pathlib.Path
        Root containing ``global``, ``india``, ``finland``, or ``hongkong``
        folders. Nested folders are allowed. Non-PDF files are ignored.
        Language defaults are English for ``global`` and ``india``, Finnish
        for ``finland``, and Traditional Chinese for ``hongkong``. A filename
        ending in ``_en``, ``_fi``, ``_zh-HK``, or ``_zh-Hant`` overrides
        the folder's default.

    Returns
    -------
    list of dict
        One record per PDF, sorted by relative path. Each record contains:

        - ``localPath``: absolute path to the PDF on this machine.
        - ``name``: PDF filename.
        - ``documentId``: stable ID generated from the relative file path.
        - ``region``: name of the regional folder containing the PDF.
        - ``language``: language code from the filename or folder default.
        - ``policyTopic``: filename without its extension, with underscores
          replaced by spaces.
        - ``sha256``: hash of the PDF contents, used to detect file changes.
        - ``version``: code generated from the PDF contents, filename, and
          parser settings, used to identify matching saved results.
        - ``documentPath``: destination path for the PDF in Azure Data Lake
          Storage.
        - ``annotationPath``: destination path for the JSON file containing
          page sizes and text positions used to highlight citations.

    Raises
    ------
    NotADirectoryError
        The data root is absent or not a directory.
    ValueError
        The directory contains symbolic links or junctions, a PDF is outside
        a supported regional folder, a path resolves outside the root, source
        IDs conflict, or no PDFs are found.
    OSError
        A directory or PDF cannot be read.

    Notes
    -----
    Document IDs remain stable when PDF contents change or the data folder
    moves. Renaming or moving a PDF within that folder changes its ID,
    except for changes in letter case.
    """
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise NotADirectoryError(f"Data directory does not exist: {data_dir}")
    paths = _find_pdf_paths(data_dir)
    sources = []
    document_ids = set()
    for path in paths:
        relative = path.relative_to(data_dir)
        region = relative.parts[0]
        if len(relative.parts) < 2 or region not in REGION_LANGUAGES:
            raise ValueError(
                f"Place {relative} under a regional folder: "
                + ", ".join(REGION_LANGUAGES)
            )
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(data_dir) or not resolved.is_file():
            raise ValueError(f"PDF must be a file inside the data directory: {path}")
        document_id = str(
            uuid5(NAMESPACE_URL, "ask-hr:" + relative.as_posix().casefold())
        )
        if document_id in document_ids:
            raise ValueError(
                f"Conflicting source paths (including letter case): {relative}"
            )
        document_ids.add(document_id)
        sha256 = _pdf_sha256(resolved)
        version = _document_version(path, sha256)
        language_suffix = path.stem.rsplit("_", 1)[-1]
        language = LANGUAGE_SUFFIXES.get(language_suffix, REGION_LANGUAGES[region])
        sources.append(
            {
                "documentId": document_id,
                "name": path.name,
                "localPath": str(resolved),
                "region": region,
                "language": language,
                "policyTopic": path.stem.replace("_", " "),
                "sha256": sha256,
                "version": version,
                "documentPath": f"documents/{document_id}/{version}.pdf",
                "annotationPath": f"annotations/{document_id}/{version}.json",
            }
        )
    if not sources:
        raise ValueError(f"No PDFs found in {data_dir}; refusing to clear an index")
    return sources

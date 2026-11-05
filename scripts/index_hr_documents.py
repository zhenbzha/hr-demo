#!/usr/bin/env python3
"""Ingest policy PDFs into ADLS Gen2 and a dedicated Azure AI Search index."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.identity import AzureCliCredential, get_bearer_token_provider
from azure.search.documents import SearchClient, SearchIndexingBufferedSender
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.filedatalake import FileSystemClient
from ingestion.analysis_cache import load_or_analyze_pages
from ingestion.azure_http import OPENAI_API_VERSION, OPENAI_SCOPE, SEARCH_API_VERSION
from ingestion.document_parser import ANALYSIS_API_VERSION
from ingestion.search_index import (
    create_index,
    embed_documents,
    plan_index_update,
    synchronize_document,
)
from ingestion.search_schema import (
    DEFAULT_INDEX_NAME,
    DEFAULT_VECTOR_DIMENSIONS,
    index_schema,
    validate_dimensions,
)
from ingestion.semantic import build_search_documents
from ingestion.sources import discover_sources
from ingestion.state import (
    FileRecord,
    SavedFileRecords,
    load_file_records,
    save_file_records,
)
from ingestion.storage import annotation_payload, upload_adls_content
from json_output import write_json
from openai import AzureOpenAI

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Increment when chunking or indexed document content changes.
INGESTION_VERSION = 1


def _add_service_arguments(parser: argparse.ArgumentParser) -> None:
    for option, variable in (
        ("document-intelligence-endpoint", "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"),
        ("openai-endpoint", "AZURE_OPENAI_ENDPOINT"),
        ("embedding-deployment", "AZURE_OPENAI_EMBEDDING_DEPLOYMENT"),
        ("search-endpoint", "AZURE_SEARCH_ENDPOINT"),
        ("index-name", "AZURE_SEARCH_INDEX_NAME"),
        ("storage-dfs-endpoint", "AZURE_STORAGE_DFS_ENDPOINT"),
        ("storage-file-system", "AZURE_STORAGE_FILE_SYSTEM"),
    ):
        parser.add_argument(
            f"--{option}", default=os.environ.get(variable), help=f"Or set {variable}."
        )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        default=os.environ.get(
            "AZURE_OPENAI_EMBEDDING_DIMENSIONS", str(DEFAULT_VECTOR_DIMENSIONS)
        ),
    )


def _add_workflow_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / "data",
        help="Scan regional folders and process only new or changed PDFs.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "output" / "ingestion"
    )
    parser.add_argument(
        "--recreate-index",
        action="store_true",
        help="DESTRUCTIVE: delete and rebuild a disposable index. Never use on a serving production index.",
    )
    parser.add_argument(
        "--skip-adls-upload",
        action="store_true",
        help="Skip artifact persistence; PDF/highlight links require matching artifacts already in ADLS.",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Validate local sources and write the schema without Azure calls.",
    )


def _validate_arguments(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    required = ["openai_endpoint", "embedding_deployment"]
    if not args.schema_only:
        required += ["document_intelligence_endpoint", "search_endpoint", "index_name"]
        if not args.skip_adls_upload:
            required += ["storage_dfs_endpoint", "storage_file_system"]
    missing = [name.replace("_", "-") for name in required if not getattr(args, name)]
    if missing:
        parser.error("Missing CLI/environment configuration: " + ", ".join(missing))
    for name in required:
        if name.endswith("endpoint"):
            endpoint = urlsplit(getattr(args, name))
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                parser.error(
                    f"{name} must be an HTTPS service endpoint without credentials or query parameters"
                )
    try:
        validate_dimensions(args.embedding_dimensions)
    except ValueError as error:
        parser.error(str(error))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    _add_service_arguments(parser)
    _add_workflow_arguments(parser)
    args = parser.parse_args()
    _validate_arguments(parser, args)
    return args


def analyze_document(
    *,
    client: DocumentIntelligenceClient,
    source: dict[str, Any],
    analysis_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Extract one PDF's text and prepare its chunks and citation data.

    Parameters
    ----------
    client : DocumentIntelligenceClient
        Document Intelligence client used to extract PDF text and layout.
    source : dict
        One PDF record returned by ``discover_sources``, including its local path.
    analysis_dir : pathlib.Path
        Directory for saved PDF analysis results.

    Returns
    -------
    chunks : list of dict
        This PDF's Search records, including extracted text, page numbers,
        and embedding text.
    annotation : dict
        This PDF's page sizes and text positions used to highlight citations.

    Raises
    ------
    RuntimeError
        A PDF produces no searchable text.
    ValueError
        A PDF changed after discovery.
    """
    local_path = Path(source["localPath"])
    print(f"Processing {local_path.name}...")
    pages = load_or_analyze_pages(
        client=client,
        source=source,
        analysis_dir=analysis_dir,
    )
    chunks = build_search_documents(pages=pages, source=source)
    if not chunks:
        raise RuntimeError(f"No searchable content remains in {local_path.name}")
    print(f"Created {len(chunks)} chunks from {local_path.name}")
    return chunks, annotation_payload(source=source, pages=pages)


def _find_file_changes(
    sources: list[dict[str, Any]],
    saved_files: dict[str, FileRecord],
    unfinished_file_ids: list[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    """
    Select new, changed, interrupted, and deleted PDFs from local state.

    Parameters
    ----------
    sources : list of dict
        Current PDF file records returned by ``discover_sources``.
    saved_files : dict of FileRecord
        Last successful hash, version, and chunk count for each known PDF.
    unfinished_file_ids : list of str
        IDs of PDFs whose previous update or deletion did not finish.

    Returns
    -------
    pending : list of dict
        New or changed PDFs, including PDFs with incomplete indexing.
    deleted : set of str
        Previously known PDF IDs no longer present in the data directory.
    """
    pending = []
    unfinished = set(unfinished_file_ids)
    for source in sources:
        document_id = source["documentId"]
        previous = saved_files.get(document_id)
        if (
            previous is None
            or document_id in unfinished
            or previous.sha256 != source["sha256"]
            or previous.version != source["version"]
        ):
            pending.append(source)
    deleted = (saved_files.keys() | unfinished) - {
        source["documentId"] for source in sources
    }
    return pending, deleted


def _ingestion_settings(
    args: argparse.Namespace,
    schema: dict[str, Any],
) -> dict[str, Any]:
    """
    Describe the desired processing settings and service destinations.

    Parameters
    ----------
    args : argparse.Namespace
        CLI settings for service destinations and optional artifact uploads.
    schema : dict
        Search index definition used to detect processing-setting changes.

    Returns
    -------
    dict
        Settings compared with the last successfully applied local record.
    """
    return {
        "ingestion_version": INGESTION_VERSION,
        "search_endpoint": args.search_endpoint.rstrip("/"),
        "index_name": args.index_name,
        "schema": schema,
        "storage": None if args.skip_adls_upload else {
            "endpoint": args.storage_dfs_endpoint.rstrip("/"),
            "fileSystem": args.storage_file_system,
        },
    }


@contextmanager
def _search_clients(
    args: argparse.Namespace,
) -> Iterator[
    tuple[AzureCliCredential, SearchIndexClient, SearchClient, Callable[..., SearchIndexingBufferedSender]]
]:
    """
    Open shared Search clients and close them on exit.

    Parameters
    ----------
    args : argparse.Namespace
        CLI settings for the Search endpoint and index name.

    Yields
    ------
    credential : AzureCliCredential
        Shared credential, also used to open PDF-processing clients.
    index_client : SearchIndexClient
        Client for reading and updating the index definition.
    search_client : SearchClient
        Client for file-scoped key lookups.
    sender_factory : callable
        Constructor for a fresh buffered sender per upload/delete operation.
    """
    with (
        AzureCliCredential() as credential,
        SearchIndexClient(
            args.search_endpoint, credential, api_version=SEARCH_API_VERSION,
        ) as index_client,
        SearchClient(
            args.search_endpoint, args.index_name, credential,
            api_version=SEARCH_API_VERSION,
        ) as search_client,
    ):
        sender_factory = partial(
            SearchIndexingBufferedSender,
            args.search_endpoint, args.index_name, credential,
            api_version=SEARCH_API_VERSION,
        )
        yield credential, index_client, search_client, sender_factory


def _prepare_index_update(
    *,
    args: argparse.Namespace,
    sources: list[dict[str, Any]],
    settings: dict[str, Any],
    records: SavedFileRecords,
    records_path: Path,
    index_client: SearchIndexClient,
) -> tuple[list[dict[str, Any]], set[str]]:
    """
    Save unfinished file IDs, apply the schema, and select PDFs to process.

    Parameters
    ----------
    args : argparse.Namespace
        CLI settings, including the explicit rebuild override.
    sources : list of dict
        All current PDFs, including unchanged files needed after a rebuild.
    settings : dict
        Desired settings from ``_ingestion_settings``.
    records : SavedFileRecords
        Loaded local file records, updated in place as preparation succeeds.
    records_path : pathlib.Path
        Atomic local progress file, saved before any index mutation.
    index_client : SearchIndexClient
        Caller-owned client for planning and applying the index update.

    Returns
    -------
    changed_files : list of dict
        PDFs that need processing, including backfills and unfinished retries.
    deleted_files : set of str
        Known PDFs no longer present in the source directory.
    """
    action = plan_index_update(
        client=index_client, schema=settings["schema"], recreate=args.recreate_index,
    )
    reprocess = action in {"create", "rebuild", "backfill"} or any(
        records.settings.get(key) != settings.get(key)
        for key in ("ingestion_version", "storage")
    )
    if reprocess:
        records.pending_document_ids = sorted(
            set(records.pending_document_ids) | records.files.keys()
            | {source["documentId"] for source in sources}
        )
    if action in {"create", "rebuild"}:
        records.files.clear()
    changed_files, deleted_files = _find_file_changes(
        sources, records.files, records.pending_document_ids,
    )
    records.pending_document_ids = sorted(
        {source["documentId"] for source in changed_files} | deleted_files
    )
    save_file_records(records_path, records)
    create_index(client=index_client, schema=settings["schema"], action=action)
    records.settings = settings
    save_file_records(records_path, records)
    return changed_files, deleted_files


@contextmanager
def _document_clients(
    args: argparse.Namespace,
    credential: AzureCliCredential,
) -> Iterator[tuple[DocumentIntelligenceClient, AzureOpenAI, FileSystemClient | None]]:
    """
    Open clients shared by changed PDFs and close them on exit.

    Parameters
    ----------
    args : argparse.Namespace
        CLI settings for DI, OpenAI, and optional ADLS uploads.
    credential : AzureCliCredential
        Credential owned by the enclosing Search-client context.

    Yields
    ------
    document_client : DocumentIntelligenceClient
        Client for PDF text and layout extraction.
    embedding_client : AzureOpenAI
        Client for embedding each PDF's chunks.
    file_system : FileSystemClient or None
        Artifact destination, or ``None`` when uploads are disabled.
    """
    with (
        DocumentIntelligenceClient(
            endpoint=args.document_intelligence_endpoint,
            credential=credential,
            api_version=ANALYSIS_API_VERSION,
        ) as document_client,
        AzureOpenAI(
            azure_endpoint=args.openai_endpoint,
            api_version=OPENAI_API_VERSION,
            azure_ad_token_provider=get_bearer_token_provider(credential, OPENAI_SCOPE),
            timeout=180,
            max_retries=3,
        ) as embedding_client,
        (
            nullcontext() if args.skip_adls_upload else FileSystemClient(
                account_url=args.storage_dfs_endpoint,
                file_system_name=args.storage_file_system,
                credential=credential,
            )
        ) as file_system,
    ):
        yield document_client, embedding_client, file_system


def _ingest_document(
    *,
    args: argparse.Namespace,
    source: dict[str, Any],
    document_client: DocumentIntelligenceClient,
    embedding_client: AzureOpenAI,
    file_system: FileSystemClient | None,
    search_client: SearchClient,
    sender_factory: Callable[..., SearchIndexingBufferedSender],
) -> tuple[FileRecord, int]:
    """
    Extract, embed, upload, and index one PDF using caller-owned clients.

    Parameters
    ----------
    args : argparse.Namespace
        CLI settings for the analysis cache and embedding model/dimensions.
    source : dict
        One PDF record returned by ``discover_sources``.
    document_client : DocumentIntelligenceClient
        Client for PDF text and layout extraction.
    embedding_client : AzureOpenAI
        Client for embedding this PDF's chunks.
    file_system : FileSystemClient or None
        Artifact destination, or ``None`` to skip ADLS uploads.
    search_client : SearchClient
        Client for looking up this PDF's existing chunk IDs.
    sender_factory : callable
        Constructor for buffered Search upload/delete senders.

    Returns
    -------
    file_record : FileRecord
        Successful hash, version, and chunk count for the caller to save.
    removed_chunks : int
        Obsolete chunks deleted after the replacement chunks were uploaded.
    """
    documents, annotation = analyze_document(
        client=document_client,
        source=source,
        analysis_dir=args.output_dir / "document-analysis",
    )
    embed_documents(
        client=embedding_client,
        documents=documents,
        deployment=args.embedding_deployment,
        dimensions=args.embedding_dimensions,
    )
    if file_system is not None:
        upload_adls_content(
            file_system=file_system,
            source=source,
            annotation=annotation,
        )
    removed_chunks = synchronize_document(
        client=search_client,
        sender_factory=sender_factory,
        document_id=source["documentId"],
        documents=documents,
    )
    return FileRecord(
        sha256=source["sha256"],
        version=source["version"],
        chunk_count=len(documents),
    ), removed_chunks


def run_ingestion(
    args: argparse.Namespace,
    sources: list[dict[str, Any]],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """
    Finish each changed PDF end-to-end before starting the next one.

    Save each successful PDF before processing the next; remove known deleted
    PDFs afterward. Unchanged runs make no Azure calls. Shared clients are closed
    by context managers. SDK acknowledgements, not query visibility, define success.

    Parameters
    ----------
    args : argparse.Namespace
        Command-line settings returned by ``parse_args``.
    sources : list of dict
        PDF file records returned by ``discover_sources``.
    schema : dict
        Search index definition from ``index_schema``, using the
        same embedding deployment and dimensions as ``args``.

    Returns
    -------
    dict
        Completion time, total and processed PDF counts, skipped PDF count,
        last successful chunk count, index name, and number of obsolete chunks
        deleted.

    Warnings
    --------
    Use one writer and preserve its local state. Unknown PDFs in the index are
    not discovered or deleted, and out-of-band index edits are not detected.
    """
    records_path = args.output_dir / "ingestion-state.json"
    settings = _ingestion_settings(args, schema)
    records = load_file_records(records_path, settings)
    changed_files, deleted_files = _find_file_changes(
        sources, records.files, records.pending_document_ids,
    )
    removed_chunks = 0
    if changed_files or deleted_files or records.settings != settings or args.recreate_index:
        with _search_clients(args) as (credential, index_client, search_client, sender_factory):
            changed_files, deleted_files = _prepare_index_update(
                args=args, sources=sources, settings=settings, records=records,
                records_path=records_path, index_client=index_client,
            )
            if changed_files:
                with _document_clients(args, credential) as (
                    document_client, embedding_client, file_system,
                ):
                    for source in changed_files:
                        document_id = source["documentId"]
                        file_record, deleted_chunks = _ingest_document(
                            args=args,
                            source=source,
                            document_client=document_client,
                            embedding_client=embedding_client,
                            file_system=file_system,
                            search_client=search_client,
                            sender_factory=sender_factory,
                        )
                        removed_chunks += deleted_chunks
                        records.files[document_id] = file_record
                        records.pending_document_ids.remove(document_id)
                        save_file_records(records_path, records)
            for document_id in sorted(deleted_files):
                removed_chunks += synchronize_document(
                    client=search_client,
                    sender_factory=sender_factory,
                    document_id=document_id,
                    documents=[],
                )
                records.files.pop(document_id, None)
                records.pending_document_ids.remove(document_id)
                save_file_records(records_path, records)
    else:
        print("No PDF changes; skipping ingestion.")
    return {
        "completedAt": datetime.now(UTC).isoformat(),
        "documents": len(sources),
        "processedDocuments": len(changed_files),
        "skippedDocuments": len(sources) - len(changed_files),
        "chunks": sum(file.chunk_count for file in records.files.values()),
        "indexName": args.index_name,
        "removedStaleChunks": removed_chunks,
    }


def main() -> None:
    args = parse_args()
    sources = discover_sources(args.data_dir)
    schema = index_schema(
        index_name=args.index_name or DEFAULT_INDEX_NAME,
        openai_endpoint=args.openai_endpoint,
        embedding_deployment=args.embedding_deployment,
        dimensions=args.embedding_dimensions,
    )
    schema_path = args.output_dir / "index-schema.json"
    write_json(schema_path, schema)
    if args.schema_only:
        print(
            json.dumps(
                {
                    "documents": len(sources),
                    "schema": str(schema_path),
                    "indexChanged": False,
                },
                indent=2,
            )
        )
        return
    summary = run_ingestion(args, sources, schema)
    write_json(args.output_dir / "ingestion-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

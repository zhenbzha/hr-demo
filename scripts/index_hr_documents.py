#!/usr/bin/env python3
"""Ingest policy PDFs into ADLS Gen2 and a dedicated Azure AI Search index."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential
from ingestion.analysis_cache import load_or_analyze_pages
from ingestion.document_parser import ANALYSIS_API_VERSION
from ingestion.manifest import load_manifest
from ingestion.search_index import (
    create_index,
    embed_documents,
    synchronize_documents,
    validate_index,
)
from ingestion.search_schema import (
    DEFAULT_INDEX_NAME,
    DEFAULT_VECTOR_DIMENSIONS,
    index_schema,
    validate_dimensions,
)
from ingestion.semantic import build_search_documents
from ingestion.storage import annotation_payload, publish_manifest, upload_adls_content

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
        "--manifest", type=Path, default=PROJECT_ROOT / "data" / "manifest.json"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "output" / "ingestion"
    )
    parser.add_argument(
        "--analysis-dir",
        type=Path,
        help="Directory of source- and parser-bound analysis caches.",
    )
    parser.add_argument(
        "--recreate-index",
        action="store_true",
        help="DESTRUCTIVE: delete and rebuild a disposable index. Never use on a serving production index.",
    )
    parser.add_argument("--reuse-analysis", action="store_true")
    parser.add_argument(
        "--retain-intermediate-artifacts",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--skip-adls-upload",
        action="store_true",
        help="Skip artifact persistence; PDF/highlight links require matching artifacts already in ADLS.",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip only the hybrid smoke query. Exact chunk-key synchronization is always checked.",
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
    if args.reuse_analysis and not args.analysis_dir:
        parser.error("--reuse-analysis requires --analysis-dir")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    _add_service_arguments(parser)
    _add_workflow_arguments(parser)
    args = parser.parse_args()
    _validate_arguments(parser, args)
    return args


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def analyze_documents(
    *,
    client: DocumentIntelligenceClient,
    active_items: list[dict[str, Any]],
    analysis_dir: Path,
    reuse_analysis: bool,
    retain_analysis: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return Search documents and citation annotations for the active PDFs."""
    documents = []
    annotations = {}
    for item in active_items:
        local_path = Path(item["localPath"])
        analysis_path = (
            analysis_dir
            / item["documentId"]
            / item["version"]
            / f"{item['sha256']}.json"
        )
        print(f"Analyzing {local_path.name}...")
        pages = load_or_analyze_pages(
            client=client,
            path=local_path,
            output_path=analysis_path,
            reuse_analysis=reuse_analysis,
            retain_analysis=retain_analysis,
            document_id=item["documentId"],
            document_version=item["version"],
            source_sha256=item["sha256"],
        )
        chunks = build_search_documents(pages=pages, manifest_item=item)
        if not chunks:
            raise RuntimeError(f"No searchable content remains in {local_path.name}")
        documents.extend(chunks)
        annotations[item["documentId"]] = annotation_payload(
            manifest_item=item, pages=pages
        )
        print(f"Created {len(chunks)} chunks from {local_path.name}")
    return documents, annotations


def _synchronize_index(
    args: argparse.Namespace,
    schema: dict[str, Any],
    documents: list[dict[str, Any]],
    credential: TokenCredential,
) -> dict[str, Any]:
    create_index(
        endpoint=args.search_endpoint,
        index_name=args.index_name,
        schema=schema,
        recreate=args.recreate_index,
        credential=credential,
    )
    removed = synchronize_documents(
        endpoint=args.search_endpoint,
        index_name=args.index_name,
        documents=documents,
        credential=credential,
    )
    summary = {"removedStaleChunks": removed, "exactChunkCoverage": True}
    if not args.skip_validation:
        summary["retrieval"] = validate_index(
            endpoint=args.search_endpoint,
            index_name=args.index_name,
            expected_regions={item["region"] for item in documents},
            expected_document_ids={item["document_id"] for item in documents},
            expected_chunk_ids={item["chunk_id"] for item in documents},
            credential=credential,
        )
    return summary


def run_ingestion(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    active_items: list[dict[str, Any]],
    schema: dict[str, Any],
) -> dict[str, Any]:
    """Run PDF ingestion and return document counts and validation results.

    Analyze and embed documents before updating storage or Search.
    Unless ADLS upload is skipped, upload citation artifacts before
    indexing and publish the manifest after index synchronization.
    """
    with (
        AzureCliCredential() as credential,
        DocumentIntelligenceClient(
            endpoint=args.document_intelligence_endpoint,
            credential=credential,
            api_version=ANALYSIS_API_VERSION,
        ) as document_client,
    ):
        documents, annotations = analyze_documents(
            client=document_client,
            active_items=active_items,
            analysis_dir=args.analysis_dir or args.output_dir / "document-analysis",
            reuse_analysis=args.reuse_analysis,
            retain_analysis=args.reuse_analysis or args.retain_intermediate_artifacts,
        )
        if args.retain_intermediate_artifacts:
            _write_json(args.output_dir / "chunks-without-vectors.json", documents)
        embed_documents(
            documents=documents,
            endpoint=args.openai_endpoint,
            deployment=args.embedding_deployment,
            dimensions=args.embedding_dimensions,
            credential=credential,
        )
        if not args.skip_adls_upload:
            upload_adls_content(
                endpoint=args.storage_dfs_endpoint,
                file_system_name=args.storage_file_system,
                credential=credential,
                active_items=active_items,
                annotations=annotations,
            )
        validation = _synchronize_index(args, schema, documents, credential)
        if not args.skip_adls_upload:
            publish_manifest(
                endpoint=args.storage_dfs_endpoint,
                file_system_name=args.storage_file_system,
                credential=credential,
                manifest=manifest,
                active_items=active_items,
            )
        return {
            "completedAt": datetime.now(UTC).isoformat(),
            "documents": len(active_items),
            "chunks": len(documents),
            "indexName": args.index_name,
            "validation": validation,
        }


def main() -> None:
    args = parse_args()
    manifest, active_items = load_manifest(args.manifest)
    schema = index_schema(
        index_name=args.index_name or DEFAULT_INDEX_NAME,
        openai_endpoint=args.openai_endpoint,
        embedding_deployment=args.embedding_deployment,
        dimensions=args.embedding_dimensions,
    )
    schema_path = args.output_dir / "index-schema.json"
    _write_json(schema_path, schema)
    if args.schema_only:
        print(
            json.dumps(
                {
                    "documents": len(active_items),
                    "schema": str(schema_path),
                    "indexChanged": False,
                },
                indent=2,
            )
        )
        return
    summary = run_ingestion(args, manifest, active_items, schema)
    _write_json(args.output_dir / "validation.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

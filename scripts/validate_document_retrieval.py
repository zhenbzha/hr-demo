#!/usr/bin/env python3
"""Check expected documents in hybrid Search results."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential
from ingestion.azure_http import (
    SEARCH_API_VERSION,
    SEARCH_SCOPE,
    authorization_headers,
    request_json,
)
from ingestion.search_index import embed_documents
from ingestion.search_schema import (
    DEFAULT_INDEX_NAME,
    DEFAULT_VECTOR_DIMENSIONS,
)

RUNNABLE_CASE_IDS = {
    "DOC-REGION-001",
    "DOC-REGION-002",
    "DOC-KNOWN-001",
    "DOC-KNOWN-002",
    "DOC-COMPARE-001",
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def embed_query(
    *,
    endpoint: str,
    deployment: str,
    dimensions: int,
    query: str,
    credential: TokenCredential,
) -> list[float]:
    document: dict[str, Any] = {"semantic_text": query}
    embed_documents(
        documents=[document],
        endpoint=endpoint,
        deployment=deployment,
        dimensions=dimensions,
        credential=credential,
    )
    return document["chunk_vector"]


def run_case(
    *,
    case: dict[str, Any],
    search_endpoint: str,
    index_name: str,
    openai_endpoint: str,
    embedding_deployment: str,
    dimensions: int,
    credential: TokenCredential,
) -> dict[str, Any]:
    query = case["query"]
    vector = embed_query(
        endpoint=openai_endpoint,
        deployment=embedding_deployment,
        dimensions=dimensions,
        query=query,
        credential=credential,
    )
    url = (
        f"{search_endpoint.rstrip('/')}/indexes/"
        f"{urllib.parse.quote(index_name)}/docs/search"
        f"?api-version={SEARCH_API_VERSION}"
    )
    _, _, result = request_json(
        "POST",
        url,
        headers=authorization_headers(credential, SEARCH_SCOPE),
        json_body={
            "search": query,
            "queryType": "semantic",
            "semanticConfiguration": "ask-hr-semantic",
            "filter": case["filter"],
            "vectorQueries": [
                {
                    "kind": "vector",
                    "vector": vector,
                    "fields": "chunk_vector",
                    "k": 50,
                }
            ],
            "select": (
                "chunk_id,document_id,document_title,region,language,"
                "policy_topic,heading_text,page_start,page_end,source_uri"
            ),
            "top": 20,
            "count": True,
        },
    )
    values = result.get("value")
    if not isinstance(values, list):
        raise RuntimeError(f"{case['id']}: Search returned no result array")
    document_ids = {value["document_id"] for value in values}
    regions = {value["region"] for value in values}

    expected_ids = set(case.get("expectedDocumentIds", []))
    missing_ids = expected_ids - document_ids
    if missing_ids:
        raise RuntimeError(
            f"{case['id']} missing expected documents {sorted(missing_ids)}; "
            f"returned {sorted(document_ids)}"
        )

    forbidden_ids = set(case.get("forbiddenDocumentIds", []))
    unexpected_ids = forbidden_ids & document_ids
    if unexpected_ids:
        raise RuntimeError(
            f"{case['id']} returned forbidden documents {sorted(unexpected_ids)}"
        )

    forbidden_regions = set(case.get("forbiddenRegions", []))
    unexpected_regions = forbidden_regions & regions
    if unexpected_regions:
        raise RuntimeError(
            f"{case['id']} returned forbidden regions {sorted(unexpected_regions)}"
        )

    return {
        "id": case["id"],
        "query": query,
        "filter": case["filter"],
        "returnedDocumentIds": sorted(document_ids),
        "returnedRegions": sorted(regions),
        "topResults": [
            {
                "documentId": value["document_id"],
                "region": value["region"],
                "heading": value.get("heading_text"),
                "pageStart": value.get("page_start"),
                "pageEnd": value.get("page_end"),
                "rerankerScore": value.get("@search.rerankerScore"),
            }
            for value in values[:5]
        ],
        "passed": True,
    }


def validate_no_dovetail(
    *,
    search_endpoint: str,
    index_name: str,
    credential: TokenCredential,
) -> dict[str, Any]:
    url = (
        f"{search_endpoint.rstrip('/')}/indexes/"
        f"{urllib.parse.quote(index_name)}/docs/search"
        f"?api-version={SEARCH_API_VERSION}"
    )
    _, _, result = request_json(
        "POST",
        url,
        headers=authorization_headers(credential, SEARCH_SCOPE),
        json_body={
            "search": "Dovetail",
            "queryType": "simple",
            "searchMode": "all",
            "searchFields": "document_title,heading_text,chunk_text",
            "top": 0,
            "count": True,
        },
    )
    count = result.get("@odata.count")
    if type(count) is not int or count < 0:
        raise RuntimeError("Stale-link validation requires a valid Search result count")
    if count:
        raise RuntimeError(
            f"DOC-STALE-LINK-001 found {count} chunks containing Dovetail"
        )
    return {
        "id": "DOC-STALE-LINK-001",
        "query": "Dovetail",
        "count": count,
        "passed": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "tests" / "document-retrieval-cases.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            PROJECT_ROOT / "output" / "ingestion" / "document-retrieval-validation.json"
        ),
    )
    parser.add_argument(
        "--search-endpoint",
        default=os.environ.get("AZURE_SEARCH_ENDPOINT"),
    )
    parser.add_argument(
        "--index-name",
        default=os.environ.get("AZURE_SEARCH_INDEX_NAME", DEFAULT_INDEX_NAME),
    )
    parser.add_argument(
        "--openai-endpoint",
        default=os.environ.get("AZURE_OPENAI_ENDPOINT"),
    )
    parser.add_argument(
        "--embedding-deployment",
        default=os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT"),
    )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        default=int(
            os.environ.get(
                "AZURE_OPENAI_EMBEDDING_DIMENSIONS",
                DEFAULT_VECTOR_DIMENSIONS,
            )
        ),
    )
    args = parser.parse_args()
    required = {
        "--search-endpoint or AZURE_SEARCH_ENDPOINT": args.search_endpoint,
        "--openai-endpoint or AZURE_OPENAI_ENDPOINT": args.openai_endpoint,
        "--embedding-deployment or AZURE_OPENAI_EMBEDDING_DEPLOYMENT": (
            args.embedding_deployment
        ),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error("Missing required configuration: " + ", ".join(missing))
    return args


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    cases_payload = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = [case for case in cases_payload["cases"] if case["id"] in RUNNABLE_CASE_IDS]
    with AzureCliCredential() as credential:
        results = [
            run_case(
                case=case,
                search_endpoint=args.search_endpoint,
                index_name=args.index_name,
                openai_endpoint=args.openai_endpoint,
                embedding_deployment=args.embedding_deployment,
                dimensions=args.embedding_dimensions,
                credential=credential,
            )
            for case in cases
        ]
        results.append(
            validate_no_dovetail(
                search_endpoint=args.search_endpoint,
                index_name=args.index_name,
                credential=credential,
            )
        )
    output = {
        "indexName": args.index_name,
        "passed": len(results),
        "failed": 0,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

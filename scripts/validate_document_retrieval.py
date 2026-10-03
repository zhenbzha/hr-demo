#!/usr/bin/env python3
"""Run document-level retrieval regressions against the Ask HR Search index."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from index_hr_documents import (
    DEFAULT_INDEX_NAME,
    DEFAULT_VECTOR_DIMENSIONS,
    SEARCH_API_VERSION,
    access_token,
    azd_values,
    embedding_url,
    request_json,
)


RUNNABLE_CASE_IDS = {
    "DOC-REGION-001",
    "DOC-REGION-002",
    "DOC-KNOWN-001",
    "DOC-KNOWN-002",
    "DOC-COMPARE-001",
}


def search_headers() -> dict[str, str]:
    return {
        "Authorization": (
            "Bearer " + access_token("https://search.azure.com/.default")
        )
    }


def embed_query(
    *,
    endpoint: str,
    deployment: str,
    dimensions: int,
    query: str,
) -> list[float]:
    _, _, result = request_json(
        "POST",
        embedding_url(endpoint, deployment),
        headers={
            "Authorization": (
                "Bearer "
                + access_token("https://cognitiveservices.azure.com/.default")
            )
        },
        json_body={"input": [query], "dimensions": dimensions},
    )
    return result["data"][0]["embedding"]


def run_case(
    *,
    case: dict[str, Any],
    search_endpoint: str,
    index_name: str,
    openai_endpoint: str,
    embedding_deployment: str,
    dimensions: int,
) -> dict[str, Any]:
    query = case["query"]
    vector = embed_query(
        endpoint=openai_endpoint,
        deployment=embedding_deployment,
        dimensions=dimensions,
        query=query,
    )
    url = (
        f"{search_endpoint.rstrip('/')}/indexes/"
        f"{urllib.parse.quote(index_name)}/docs/search"
        f"?api-version={SEARCH_API_VERSION}"
    )
    _, _, result = request_json(
        "POST",
        url,
        headers=search_headers(),
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
                    "k": 20,
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
    values = result.get("value", [])
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
            f"{case['id']} returned forbidden documents "
            f"{sorted(unexpected_ids)}"
        )

    forbidden_regions = set(case.get("forbiddenRegions", []))
    unexpected_regions = forbidden_regions & regions
    if unexpected_regions:
        raise RuntimeError(
            f"{case['id']} returned forbidden regions "
            f"{sorted(unexpected_regions)}"
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
    *, search_endpoint: str, index_name: str
) -> dict[str, Any]:
    url = (
        f"{search_endpoint.rstrip('/')}/indexes/"
        f"{urllib.parse.quote(index_name)}/docs/search"
        f"?api-version={SEARCH_API_VERSION}"
    )
    _, _, result = request_json(
        "POST",
        url,
        headers=search_headers(),
        json_body={
            "search": "Dovetail",
            "queryType": "simple",
            "searchMode": "all",
            "searchFields": "document_title,heading_text,chunk_text",
            "top": 0,
            "count": True,
        },
    )
    count = int(result.get("@odata.count", 0))
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
        default=Path("tests/document-retrieval-cases.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/ingestion/document-retrieval-validation.json"),
    )
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    cases_payload = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = [
        case
        for case in cases_payload["cases"]
        if case["id"] in RUNNABLE_CASE_IDS
    ]
    values = azd_values()
    search_endpoint = values["AZURE_SEARCH_ENDPOINT"]
    index_name = values.get("AZURE_SEARCH_INDEX_NAME", DEFAULT_INDEX_NAME)
    openai_endpoint = values["AZURE_OPENAI_ENDPOINT"]
    embedding_deployment = values["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]
    dimensions = int(
        values.get(
            "AZURE_OPENAI_EMBEDDING_DIMENSIONS",
            DEFAULT_VECTOR_DIMENSIONS,
        )
    )

    results = [
        run_case(
            case=case,
            search_endpoint=search_endpoint,
            index_name=index_name,
            openai_endpoint=openai_endpoint,
            embedding_deployment=embedding_deployment,
            dimensions=dimensions,
        )
        for case in cases
    ]
    results.append(
        validate_no_dovetail(
            search_endpoint=search_endpoint,
            index_name=index_name,
        )
    )
    output = {
        "indexName": index_name,
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

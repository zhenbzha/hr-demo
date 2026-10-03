#!/usr/bin/env python3
"""Index local HR PDFs into Azure AI Search with layout-aware citations."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OPENAI_API_VERSION = "2024-10-21"
SEARCH_API_VERSION = "2026-08-01-preview"
DEFAULT_INDEX_NAME = "ask-hr-docs"
DEFAULT_VECTOR_DIMENSIONS = 1024
SUPPORTED_REGIONS = {"global", "india", "finland", "hongkong"}
SUPPORTED_LANGUAGES = {"en", "fi", "zh-Hant"}
REQUIRED_MANIFEST_FIELDS = {
    "region",
    "language",
    "documentId",
    "policyTopic",
    "isActive",
    "version",
    "effectiveDate",
    "reviewDate",
    "name",
    "localPath",
    "sourceUrl",
    "sha256",
}


def run_command(command: list[str]) -> str:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def executable(name: str) -> str:
    path = shutil.which(f"{name}.cmd") or shutil.which(name)
    if not path:
        raise FileNotFoundError(f"{name} was not found on PATH")
    return path


def azd_values() -> dict[str, str]:
    output = run_command(
        [executable("azd"), "env", "get-values", "--output", "json"]
    )
    parsed = json.loads(output)
    return {str(key): str(value) for key, value in parsed.items()}


def access_token(scope: str) -> str:
    return run_command(
        [
            executable("az"),
            "account",
            "get-access-token",
            "--scope",
            scope,
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ]
    )


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any | None = None,
    raw_body: bytes | None = None,
    timeout: int = 180,
    retries: int = 4,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = dict(headers or {})
    body = raw_body
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return (
                    response.status,
                    {
                        key.lower(): value
                        for key, value in response.headers.items()
                    },
                    response.read(),
                )
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries:
                retry_after = exc.headers.get("Retry-After")
                delay = int(retry_after) if retry_after else attempt * 5
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"{method} {url} failed with HTTP {exc.code}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(attempt * 5)
                continue
            raise RuntimeError(f"{method} {url} failed: {exc}") from exc

    raise RuntimeError(f"{method} {url} exhausted its retry budget")


def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any | None = None,
    raw_body: bytes | None = None,
    timeout: int = 180,
    retries: int = 4,
) -> tuple[int, dict[str, str], dict[str, Any]]:
    status, response_headers, body = request(
        method,
        url,
        headers=headers,
        json_body=json_body,
        raw_body=raw_body,
        timeout=timeout,
        retries=retries,
    )
    payload = json.loads(body.decode("utf-8-sig")) if body else {}
    return status, response_headers, payload


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def deterministic_id(parent_id: str, ordinal: int, value: str) -> str:
    material = f"{parent_id}|{ordinal}|{content_hash(value)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def extract_heading(text: str, fallback: str) -> str:
    headings = re.findall(
        r"(?m)^#{1,6}\s+(.+?)\s*$",
        text,
    )
    return clean_text(headings[-1]) if headings else fallback


def load_or_analyze_pages(
    *,
    client: Any,
    path: Path,
    output_path: Path,
    reuse_analysis: bool,
) -> list[Any]:
    from ingestion.document_parser import (
        analyze_local_pdf,
        remove_document_control,
    )
    from ingestion.page import Page

    if reuse_analysis and output_path.exists():
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        pages = [Page(**item) for item in payload]
        for page in pages:
            page.text = remove_document_control(page.text)
        return [page for page in pages if page.text]

    pages = analyze_local_pdf(client=client, path=path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            [
                {
                    "page_num": page.page_num,
                    "offset": page.offset,
                    "text": page.text,
                    "tables": page.tables,
                }
                for page in pages
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return pages


def build_sdk_documents(
    *,
    pages: list[Any],
    manifest_item: dict[str, Any],
) -> list[dict[str, Any]]:
    from ingestion.textsplitter import SentenceTextSplitter

    parent_id = hashlib.sha256(
        manifest_item["sourceUrl"].encode("utf-8")
    ).hexdigest()[:24]
    title = Path(manifest_item["name"]).stem.replace("_", " ")
    splitter = SentenceTextSplitter(max_tokens_per_section=500)
    documents: list[dict[str, Any]] = []

    for ordinal, chunk in enumerate(splitter.split_pages(pages)):
        text = chunk.text.strip()
        if not text:
            continue
        page_number = int(chunk.page_num) + 1
        documents.append(
            base_document(
                manifest_item=manifest_item,
                parent_id=parent_id,
                ordinal=ordinal,
                text=text,
                heading_text=extract_heading(text, title),
                pages=[page_number],
            )
        )
    return documents


def base_document(
    *,
    manifest_item: dict[str, Any],
    parent_id: str,
    ordinal: int,
    text: str,
    heading_text: str,
    pages: list[int],
) -> dict[str, Any]:
    return {
        "chunk_id": deterministic_id(parent_id, ordinal, text),
        "document_id": manifest_item["documentId"],
        "document_title": Path(manifest_item["name"]).stem.replace("_", " "),
        "region": manifest_item["region"],
        "language": manifest_item["language"],
        "policy_topic": manifest_item["policyTopic"],
        "version": manifest_item["version"],
        "source_uri": manifest_item["sourceUrl"],
        "heading_text": heading_text,
        "chunk_text": text,
        "page_start": min(pages) if pages else None,
        "page_end": max(pages) if pages else None,
    }


def embedding_url(endpoint: str, deployment: str) -> str:
    encoded_deployment = urllib.parse.quote(deployment, safe="")
    return (
        f"{endpoint.rstrip('/')}/openai/deployments/{encoded_deployment}/"
        f"embeddings?api-version={OPENAI_API_VERSION}"
    )


def embed_documents(
    *,
    documents: list[dict[str, Any]],
    endpoint: str,
    deployment: str,
    dimensions: int,
) -> None:
    token = access_token("https://cognitiveservices.azure.com/.default")
    url = embedding_url(endpoint, deployment)
    batch_size = 16

    for batch_start in range(0, len(documents), batch_size):
        batch = documents[batch_start : batch_start + batch_size]
        _, _, result = request_json(
            "POST",
            url,
            headers={"Authorization": f"Bearer {token}"},
            json_body={
                "input": [document["chunk_text"] for document in batch],
                "dimensions": dimensions,
            },
            timeout=180,
        )
        vectors = sorted(result["data"], key=lambda item: item["index"])
        if len(vectors) != len(batch):
            raise RuntimeError("Embedding response count did not match input")
        for document, vector in zip(batch, vectors, strict=True):
            embedding = vector["embedding"]
            if len(embedding) != dimensions:
                raise RuntimeError(
                    f"Expected {dimensions} dimensions, got {len(embedding)}"
                )
            document["chunk_vector"] = embedding
        print(
            f"Embedded {min(batch_start + batch_size, len(documents))}/"
            f"{len(documents)} chunks"
        )


def index_schema(
    *,
    index_name: str,
    openai_endpoint: str,
    embedding_deployment: str,
    dimensions: int,
) -> dict[str, Any]:
    return {
        "name": index_name,
        "fields": [
            {
                "name": "chunk_id",
                "type": "Edm.String",
                "key": True,
                "filterable": True,
            },
            {
                "name": "document_id",
                "type": "Edm.String",
                "retrievable": True,
                "filterable": True,
                "facetable": True,
            },
            {
                "name": "document_title",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
                "filterable": True,
            },
            {
                "name": "region",
                "type": "Edm.String",
                "retrievable": True,
                "filterable": True,
                "facetable": True,
            },
            {
                "name": "language",
                "type": "Edm.String",
                "retrievable": True,
                "filterable": True,
            },
            {
                "name": "policy_topic",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
                "filterable": True,
                "facetable": True,
            },
            {
                "name": "version",
                "type": "Edm.String",
                "retrievable": True,
            },
            {"name": "source_uri", "type": "Edm.String", "retrievable": True},
            {
                "name": "heading_text",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
            },
            {
                "name": "chunk_text",
                "type": "Edm.String",
                "searchable": True,
                "retrievable": True,
                "analyzer": "standard.lucene",
            },
            {
                "name": "chunk_vector",
                "type": "Collection(Edm.Single)",
                "searchable": True,
                "retrievable": False,
                "stored": False,
                "dimensions": dimensions,
                "vectorSearchProfile": "ask-hr-vector-profile",
            },
            {
                "name": "page_start",
                "type": "Edm.Int32",
                "retrievable": True,
            },
            {
                "name": "page_end",
                "type": "Edm.Int32",
                "retrievable": True,
            },
        ],
        "vectorSearch": {
            "algorithms": [
                {
                    "name": "ask-hr-hnsw",
                    "kind": "hnsw",
                    "hnswParameters": {
                        "metric": "cosine",
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 500,
                    },
                }
            ],
            "profiles": [
                {
                    "name": "ask-hr-vector-profile",
                    "algorithm": "ask-hr-hnsw",
                    "vectorizer": "ask-hr-openai-vectorizer",
                }
            ],
            "vectorizers": [
                {
                    "name": "ask-hr-openai-vectorizer",
                    "kind": "azureOpenAI",
                    "azureOpenAIParameters": {
                        "resourceUri": openai_endpoint,
                        "deploymentId": embedding_deployment,
                        "modelName": "text-embedding-3-large",
                        "authIdentity": None,
                    },
                }
            ],
        },
        "semantic": {
            "defaultConfiguration": "ask-hr-semantic",
            "configurations": [
                {
                    "name": "ask-hr-semantic",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "document_title"},
                        "prioritizedContentFields": [
                            {"fieldName": "chunk_text"}
                        ],
                        "prioritizedKeywordsFields": [
                            {"fieldName": "heading_text"},
                            {"fieldName": "policy_topic"},
                        ],
                    },
                }
            ],
        },
    }


def search_headers() -> dict[str, str]:
    return {
        "Authorization": (
            "Bearer " + access_token("https://search.azure.com/.default")
        )
    }


def create_index(
    *,
    endpoint: str,
    index_name: str,
    schema: dict[str, Any],
    recreate: bool,
) -> None:
    url = (
        f"{endpoint.rstrip('/')}/indexes/{urllib.parse.quote(index_name)}"
        f"?api-version={SEARCH_API_VERSION}"
    )
    headers = search_headers()
    if recreate:
        try:
            request_json("DELETE", url, headers=headers)
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
    request_json("PUT", url, headers=headers, json_body=schema)


def upload_documents(
    *,
    endpoint: str,
    index_name: str,
    documents: list[dict[str, Any]],
) -> None:
    url = (
        f"{endpoint.rstrip('/')}/indexes/{urllib.parse.quote(index_name)}/"
        f"docs/index?api-version={SEARCH_API_VERSION}"
    )
    headers = search_headers()
    batch_size = 100
    for batch_start in range(0, len(documents), batch_size):
        batch = documents[batch_start : batch_start + batch_size]
        _, _, result = request_json(
            "POST",
            url,
            headers=headers,
            json_body={
                "value": [
                    {"@search.action": "mergeOrUpload", **document}
                    for document in batch
                ]
            },
        )
        failures = [
            item
            for item in result.get("value", [])
            if not item.get("status")
        ]
        if failures:
            raise RuntimeError(
                "Search indexing failed:\n"
                + json.dumps(failures, ensure_ascii=False, indent=2)
            )
        print(
            f"Uploaded {min(batch_start + batch_size, len(documents))}/"
            f"{len(documents)} chunks"
        )


def validate_index(
    *,
    endpoint: str,
    index_name: str,
    openai_endpoint: str,
    embedding_deployment: str,
    dimensions: int,
    expected_regions: set[str],
    expected_document_ids: set[str],
) -> dict[str, Any]:
    token = access_token("https://cognitiveservices.azure.com/.default")
    _, _, embedding_result = request_json(
        "POST",
        embedding_url(openai_endpoint, embedding_deployment),
        headers={"Authorization": f"Bearer {token}"},
        json_body={
            "input": ["What leave and holiday policies apply?"],
            "dimensions": dimensions,
        },
    )
    vector = embedding_result["data"][0]["embedding"]
    url = (
        f"{endpoint.rstrip('/')}/indexes/{urllib.parse.quote(index_name)}/"
        f"docs/search?api-version={SEARCH_API_VERSION}"
    )
    _, _, result = request_json(
        "POST",
        url,
        headers=search_headers(),
        json_body={
            "search": "leave holiday policy",
            "queryType": "semantic",
            "semanticConfiguration": "ask-hr-semantic",
            "vectorQueries": [
                {
                    "kind": "vector",
                    "vector": vector,
                    "fields": "chunk_vector",
                    "k": 5,
                }
            ],
            "select": (
                "chunk_id,document_id,document_title,region,language,"
                "policy_topic,heading_text,"
                "page_start,page_end,source_uri"
            ),
            "top": 5,
            "count": True,
        },
    )
    if not result.get("value"):
        raise RuntimeError("Search validation returned no documents")

    _, _, facets = request_json(
        "POST",
        url,
        headers=search_headers(),
        json_body={
            "search": "*",
            "facets": [
                "region,count:20",
                "document_id,count:100",
            ],
            "top": 0,
            "count": True,
        },
    )
    facet_values = facets.get("@search.facets", {})
    actual_regions = {
        str(item["value"])
        for item in facet_values.get("region", [])
    }
    actual_document_ids = {
        str(item["value"])
        for item in facet_values.get("document_id", [])
    }
    if actual_regions != expected_regions:
        raise RuntimeError(
            "Region facets did not match the manifest: "
            f"expected {sorted(expected_regions)}, got {sorted(actual_regions)}"
        )
    if actual_document_ids != expected_document_ids:
        raise RuntimeError(
            "Indexed document IDs did not match the manifest: "
            f"expected {sorted(expected_document_ids)}, "
            f"got {sorted(actual_document_ids)}"
        )
    return {"hybridQuery": result, "facets": facets}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Index Global, India, Finland, and Hong Kong HR PDFs into AI Search."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/manifest.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/ingestion"),
    )
    parser.add_argument("--recreate-index", action="store_true")
    parser.add_argument("--reuse-analysis", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help=(
            "Validate the manifest and write the proposed index schema "
            "without analyzing PDFs or changing Azure AI Search."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    document_ids: set[str] = set()
    for manifest_item in manifest["documents"]:
        missing_fields = sorted(
            REQUIRED_MANIFEST_FIELDS - manifest_item.keys()
        )
        if missing_fields:
            raise ValueError(
                f"Missing manifest fields {missing_fields} for "
                f"{manifest_item.get('name', '<unnamed>')}"
            )
        region = manifest_item.get("region")
        language = manifest_item.get("language")
        if region not in SUPPORTED_REGIONS:
            raise ValueError(
                f"Unsupported or missing region {region!r} for "
                f"{manifest_item.get('name', '<unnamed>')}"
            )
        if not isinstance(manifest_item["isActive"], bool):
            raise ValueError(
                f"isActive must be true or false for {manifest_item['name']}"
            )
        document_id = manifest_item["documentId"]
        if document_id in document_ids:
            raise ValueError(f"Duplicate document ID {document_id}")
        document_ids.add(document_id)
        local_path = Path(manifest_item["localPath"])
        if not local_path.exists():
            raise FileNotFoundError(local_path)
        actual_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
        if actual_hash != manifest_item["sha256"]:
            raise RuntimeError(
                f"SHA-256 mismatch for {local_path}: "
                f"manifest {manifest_item['sha256']}, actual {actual_hash}"
            )
        if language not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Unsupported or missing language {language!r} for "
                f"{manifest_item.get('name', '<unnamed>')}"
            )
    active_manifest_items = [
        item for item in manifest["documents"] if item["isActive"]
    ]
    if not active_manifest_items:
        raise ValueError("The manifest contains no active documents")
    values = azd_values()

    document_endpoint = values["AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"]
    openai_endpoint = values["AZURE_OPENAI_ENDPOINT"]
    embedding_deployment = values["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]
    dimensions = int(
        values.get(
            "AZURE_OPENAI_EMBEDDING_DIMENSIONS",
            DEFAULT_VECTOR_DIMENSIONS,
        )
    )
    search_endpoint = values["AZURE_SEARCH_ENDPOINT"]
    index_name = values.get("AZURE_SEARCH_INDEX_NAME", DEFAULT_INDEX_NAME)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    schema = index_schema(
        index_name=index_name,
        openai_endpoint=openai_endpoint,
        embedding_deployment=embedding_deployment,
        dimensions=dimensions,
    )
    schema_path = args.output_dir / "index-schema.json"
    schema_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if args.schema_only:
        print(
            json.dumps(
                {
                    "documents": len(active_manifest_items),
                    "regions": sorted(
                        {item["region"] for item in active_manifest_items}
                    ),
                    "languages": sorted(
                        {item["language"] for item in active_manifest_items}
                    ),
                    "documentIds": sorted(
                        item["documentId"] for item in active_manifest_items
                    ),
                    "schema": str(schema_path),
                    "indexChanged": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.identity import AzureCliCredential

    credential = AzureCliCredential()
    document_client = DocumentIntelligenceClient(
        endpoint=document_endpoint,
        credential=credential,
    )
    documents: list[dict[str, Any]] = []
    try:
        for manifest_item in active_manifest_items:
            local_path = Path(manifest_item["localPath"])
            analysis_path = (
                args.output_dir
                / "sdk-layout"
                / manifest_item["region"]
                / f"{local_path.stem}.json"
            )
            print(f"Analyzing {local_path}...")
            pages = load_or_analyze_pages(
                client=document_client,
                path=local_path,
                output_path=analysis_path,
                reuse_analysis=args.reuse_analysis,
            )
            document_chunks = build_sdk_documents(
                pages=pages,
                manifest_item=manifest_item,
            )
            if not document_chunks:
                raise RuntimeError(
                    f"No chunks were created for {local_path}"
                )
            documents.extend(document_chunks)
            print(
                f"Created {len(document_chunks)} chunks from "
                f"{local_path.name}"
            )
    finally:
        document_client.close()
        credential.close()

    chunks_path = args.output_dir / "chunks-without-vectors.json"
    chunks_path.write_text(
        json.dumps(documents, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Created {len(documents)} chunks from {len(active_manifest_items)} PDFs")

    print("Generating embeddings...")
    embed_documents(
        documents=documents,
        endpoint=openai_endpoint,
        deployment=embedding_deployment,
        dimensions=dimensions,
    )

    print(f"Creating Azure AI Search index {index_name}...")
    create_index(
        endpoint=search_endpoint,
        index_name=index_name,
        schema=schema,
        recreate=args.recreate_index,
    )
    upload_documents(
        endpoint=search_endpoint,
        index_name=index_name,
        documents=documents,
    )

    validation_path = None
    if not args.skip_validation:
        validation = validate_index(
            endpoint=search_endpoint,
            index_name=index_name,
            openai_endpoint=openai_endpoint,
            embedding_deployment=embedding_deployment,
            dimensions=dimensions,
            expected_regions={
                item["region"] for item in active_manifest_items
            },
            expected_document_ids={
                item["documentId"] for item in active_manifest_items
            },
        )
        validation_path = args.output_dir / "validation.json"
        validation_path.write_text(
            json.dumps(validation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print(
        json.dumps(
            {
                "completedAt": datetime.now(timezone.utc).isoformat(),
                "documents": len(active_manifest_items),
                "chunks": len(documents),
                "indexName": index_name,
                "schema": str(schema_path),
                "validation": (
                    str(validation_path) if validation_path else None
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

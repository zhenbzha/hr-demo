"""Embed, synchronize, and verify a dedicated Azure AI Search index."""

from __future__ import annotations

import json
import logging
import math
import time
from functools import partial
from itertools import batched
from typing import Any

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.core.pipeline import PipelineResponse
from azure.core.rest import HttpRequest, HttpResponse
from azure.identity import get_bearer_token_provider
from azure.search.documents import SearchClient, SearchIndexingBufferedSender
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.models import VectorizableTextQuery
from openai import AzureOpenAI

from .azure_http import (
    OPENAI_API_VERSION,
    OPENAI_SCOPE,
    SEARCH_API_VERSION,
)
from .search_schema import EMBEDDING_MODEL, validate_dimensions
from .textsplitter import EMBEDDING_TOKEN_LIMIT, token_count

logger = logging.getLogger(__name__)
INDEX_BATCH_SIZE = 100


def _embedding_vectors(
    result: dict[str, Any],
    count: int,
    dimensions: int,
) -> list[list[float]]:
    data = result.get("data")
    if (
        not isinstance(data, list)
        or len(data) != count
        or any(not isinstance(item, dict) for item in data)
    ):
        raise RuntimeError("Embedding response count did not match input")
    indices = [item.get("index") for item in data]
    if any(type(index) is not int for index in indices) or sorted(indices) != list(
        range(count)
    ):
        raise RuntimeError(
            "Embedding response indices must uniquely match every input index"
        )
    if result.get("model") != EMBEDDING_MODEL:
        raise RuntimeError(
            f"Embedding deployment must serve {EMBEDDING_MODEL}, got {result.get('model')!r}"
        )
    vectors = [
        item.get("embedding") for item in sorted(data, key=lambda item: item["index"])
    ]
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise RuntimeError(f"Embedding response must have {dimensions} dimensions")
        if any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in vector
        ):
            raise RuntimeError(
                "Embedding response contains non-finite or non-numeric values"
            )
        if not any(vector):
            raise RuntimeError("A zero vector cannot be used for cosine similarity")
    return vectors


def embed_documents(
    *,
    documents: list[dict[str, Any]],
    endpoint: str,
    deployment: str,
    dimensions: int,
    credential: TokenCredential,
) -> None:
    """Generate embeddings and add chunk_vector to each document in place.

    Embed semantic_text after checking input token limits. Validate the
    returned model, vector dimensions, and input-to-vector correspondence.
    """
    validate_dimensions(dimensions)
    for document in documents:
        text = document.get("semantic_text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Every embedding input must contain semantic_text")
        if token_count(text) > EMBEDDING_TOKEN_LIMIT:
            raise ValueError(f"Embedding input exceeds {EMBEDDING_TOKEN_LIMIT} tokens")
    with AzureOpenAI(
        azure_endpoint=endpoint,
        api_version=OPENAI_API_VERSION,
        azure_ad_token_provider=get_bearer_token_provider(credential, OPENAI_SCOPE),
        timeout=180,
        max_retries=3,
    ) as client:
        for start in range(0, len(documents), 16):
            batch = documents[start : start + 16]
            result = client.embeddings.create(
                model=deployment,
                input=[document["semantic_text"] for document in batch],
                dimensions=dimensions,
                encoding_format="float",
            )
            vectors = _embedding_vectors(result.model_dump(), len(batch), dimensions)
            for document, vector in zip(batch, vectors, strict=True):
                document["chunk_vector"] = vector
            print(f"Embedded {start + len(batch)}/{len(documents)} chunks")


def _check_embedding_compatibility(
    existing: dict[str, Any], proposed: dict[str, Any]
) -> None:
    previous_fields = {field["name"]: field for field in existing["fields"]}
    previous_vector = previous_fields.get("chunk_vector", {})
    next_vector = next(
        field for field in proposed["fields"] if field["name"] == "chunk_vector"
    )
    if previous_vector.get("dimensions") != next_vector["dimensions"]:
        raise ValueError(
            "Changing vector dimensions requires a new index, not an in-place update"
        )
    before = existing.get("vectorSearch", {}).get("vectorizers", [])
    after = proposed["vectorSearch"]["vectorizers"]
    if len(before) != 1 or any(
        str(before[0].get("azureOpenAIParameters", {}).get(field, "")).rstrip("/")
        != str(after[0]["azureOpenAIParameters"][field]).rstrip("/")
        for field in ("resourceUri", "deploymentId", "modelName")
    ):
        raise ValueError("Changing the query embedding deployment requires a new index")


def _compatible_schema(
    existing: dict[str, Any], proposed: dict[str, Any]
) -> dict[str, Any]:
    """Merge schema additions and settings while preserving existing fields.

    Reject incompatible field types, key capabilities, or embedding
    configuration with ValueError.
    """
    _check_embedding_compatibility(existing, proposed)
    existing_fields = {field["name"]: field for field in existing["fields"]}
    key = existing_fields.get("chunk_id", {})
    if (
        not key.get("key")
        or not key.get("sortable", True)
        or not key.get("filterable", True)
    ):
        raise ValueError(
            "The existing chunk_id must be a sortable, filterable key; use a new index"
        )
    for field in proposed["fields"]:
        previous = existing_fields.get(field["name"])
        if previous is not None and previous["type"] != field["type"]:
            raise ValueError(
                f"Changing {field['name']} field type requires a new index"
            )
    logger.info(
        "Preserving existing immutable Search fields; applying additions and mutable settings"
    )
    return {
        **existing,
        **proposed,
        "fields": [
            *existing["fields"],
            *(
                field
                for field in proposed["fields"]
                if field["name"] not in existing_fields
            ),
        ],
    }


def create_index(
    *,
    endpoint: str,
    index_name: str,
    schema: dict[str, Any],
    recreate: bool,
    credential: TokenCredential,
) -> None:
    """Create an index or apply compatible schema updates.

    If recreate is True, delete the existing index before creating it.
    Otherwise, raise ValueError for incompatible schema changes.
    """
    with SearchIndexClient(
        endpoint, credential, api_version=SEARCH_API_VERSION
    ) as client:
        try:
            existing = client.get_index(index_name).as_dict()
        except ResourceNotFoundError:
            existing = None
        if existing is not None:
            if recreate:
                client.delete_index(index_name)
            else:
                schema = _compatible_schema(existing, schema)
        client.create_or_update_index(schema)


def _acknowledgements(result: object, expected_keys: set[str]) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        raise RuntimeError("Search indexing response must be a JSON object")
    items = result.get("value")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise RuntimeError("Search indexing response has no valid acknowledgements")
    returned_keys = [item.get("key") for item in items]
    if (
        any(not isinstance(key, str) for key in returned_keys)
        or len(returned_keys) != len(expected_keys)
        or set(returned_keys) != expected_keys
    ):
        raise RuntimeError(
            "Search acknowledgements do not uniquely cover the submitted keys"
        )
    if any(
        type(item.get("status")) is not bool or type(item.get("statusCode")) is not int
        for item in items
    ):
        raise RuntimeError("Search acknowledgement has invalid status fields")
    if any(item["status"] and item["statusCode"] not in {200, 201} for item in items):
        raise RuntimeError("Search acknowledgement has contradictory success status")
    return items


def _record_batch_response(
    response: PipelineResponse[HttpRequest, HttpResponse],
    *,
    contract_errors: list[str],
    failure_details: dict[str, Any],
) -> None:
    """Record acknowledgement errors and failed-item response details.

    Update the supplied contract_errors and failure_details collections
    for the caller's batch checks.
    """
    if response.http_response.status_code not in {200, 201, 207}:
        failure_details["http"] = response.http_response.text()[:2000]
        return
    try:
        submitted = json.loads(response.http_request.content)["value"]
        result = json.loads(response.http_response.text())
        items = _acknowledgements(result, {action["chunk_id"] for action in submitted})
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        contract_errors.append(str(error))
        raise
    for item in items:
        if not item["status"]:
            failure_details[item["key"]] = item


def _write_actions(
    endpoint: str,
    index_name: str,
    actions: list[dict[str, Any]],
    credential: TokenCredential,
) -> None:
    """Submit batched uploads or deletions and verify each acknowledgement.

    Require unique chunk IDs and a single operation type per call.
    Raise RuntimeError for failed or missing indexing acknowledgements.
    """
    keys = [action["chunk_id"] for action in actions]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate Search keys in a single ingestion operation")
    if not actions:
        return
    operation = actions[0]["@search.action"]
    if operation not in {"upload", "delete"} or any(
        action["@search.action"] != operation for action in actions
    ):
        raise ValueError(
            "Submit uploads and stale-key deletions as separate operations"
        )
    completed: set[str] = set()
    failed: set[str] = set()
    contract_errors: list[str] = []
    failure_details: dict[str, Any] = {}

    with SearchIndexingBufferedSender(
        endpoint,
        index_name,
        credential,
        api_version=SEARCH_API_VERSION,
        auto_flush=False,
        max_retries_per_action=3,
        on_progress=lambda action: completed.add(action["chunk_id"]),
        on_error=lambda action: failed.add(action["chunk_id"]),
        raw_response_hook=partial(
            _record_batch_response,
            contract_errors=contract_errors,
            failure_details=failure_details,
        ),
    ) as sender:
        for batch in batched(actions, INDEX_BATCH_SIZE):
            if operation == "upload":
                sender.upload_documents(list(batch))
            else:
                sender.delete_documents(list(batch))
            # flush() also reports recovered transient failures; callbacks
            # identify the final outcome, including malformed responses.
            sender.flush(timeout=120)
            missing = {action["chunk_id"] for action in batch} - completed
            if contract_errors or failed or missing:
                raise RuntimeError(
                    f"Search acknowledgements failed: missing={sorted(missing)}, "
                    f"failed={sorted(failed)}, contracts={contract_errors}, details={failure_details}"
                )


def upload_documents(
    *,
    endpoint: str,
    index_name: str,
    documents: list[dict[str, Any]],
    credential: TokenCredential,
) -> None:
    """Create or replace each Search document and verify its indexing result."""
    _write_actions(
        endpoint,
        index_name,
        [{**document, "@search.action": "upload"} for document in documents],
        credential,
    )


def _index_rows(
    endpoint: str,
    index_name: str,
    credential: TokenCredential,
    timeout: float = 120,
) -> list[dict[str, Any]]:
    """Return indexed chunk IDs, document IDs, and regions in key order.

    Use keyset pagination to read every page. Raise TimeoutError if
    enumeration exceeds timeout seconds.
    """
    rows: list[dict[str, Any]] = []
    last_key = ""
    deadline = time.monotonic() + timeout
    with SearchClient(
        endpoint, index_name, credential, api_version=SEARCH_API_VERSION
    ) as client:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Search key enumeration timed out")
            page = list(
                client.search(
                    search_text="*",
                    select=["chunk_id", "document_id", "region"],
                    order_by=["chunk_id asc"],
                    top=1000,
                    filter="chunk_id gt '" + last_key.replace("'", "''") + "'"
                    if last_key
                    else None,
                    timeout=remaining,
                    read_timeout=min(30, remaining),
                    connection_timeout=min(30, remaining),
                )
            )
            if not page:
                return rows
            keys = [item.get("chunk_id") for item in page]
            if any(
                not isinstance(key, str) or key <= last_key for key in keys
            ) or keys != sorted(set(keys)):
                raise RuntimeError(
                    "Search key enumeration did not advance in unique key order"
                )
            rows.extend(page)
            last_key = keys[-1]


def _wait_for_keys(
    endpoint: str,
    index_name: str,
    expected: set[str],
    credential: TokenCredential,
    *,
    exact: bool,
    timeout: float = 120,
) -> set[str]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "Search did not expose the expected chunk keys before the visibility deadline"
            )
        actual = {
            item["chunk_id"]
            for item in _index_rows(endpoint, index_name, credential, remaining)
        }
        matches = actual == expected if exact else expected.issubset(actual)
        if matches:
            return actual
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def synchronize_documents(
    *,
    endpoint: str,
    index_name: str,
    documents: list[dict[str, Any]],
    credential: TokenCredential,
) -> int:
    """Replace indexed chunks and return the number of stale chunks deleted.

    Upload and verify new chunks before deleting old ones. This requires
    a single writer. Updates are not atomic: readers may see old and new
    chunks while synchronization is in progress.
    """
    expected = {document["chunk_id"] for document in documents}
    if not expected or len(expected) != len(documents):
        raise ValueError("Refusing to synchronize empty or duplicate Search keys")
    previous = {
        item["chunk_id"] for item in _index_rows(endpoint, index_name, credential)
    }
    upload_documents(
        endpoint=endpoint,
        index_name=index_name,
        documents=documents,
        credential=credential,
    )
    _wait_for_keys(endpoint, index_name, expected, credential, exact=False)
    stale = previous - expected
    _write_actions(
        endpoint,
        index_name,
        [{"@search.action": "delete", "chunk_id": key} for key in sorted(stale)],
        credential,
    )
    _wait_for_keys(endpoint, index_name, expected, credential, exact=True)
    return len(stale)


def validate_index(
    *,
    endpoint: str,
    index_name: str,
    expected_regions: set[str],
    expected_document_ids: set[str],
    expected_chunk_ids: set[str],
    credential: TokenCredential,
) -> dict[str, Any]:
    """Verify index coverage and return a hybrid-search validation summary.

    Raise RuntimeError if regions, documents, or chunks differ from the
    expected sets, or if the hybrid query returns no results.
    """
    rows = _index_rows(endpoint, index_name, credential)
    actual = {
        "regions": {row.get("region") for row in rows},
        "documentIds": {row.get("document_id") for row in rows},
        "chunkIds": {row["chunk_id"] for row in rows},
    }
    for label, expected in (
        ("regions", expected_regions),
        ("documentIds", expected_document_ids),
        ("chunkIds", expected_chunk_ids),
    ):
        if actual[label] != expected:
            raise RuntimeError(
                f"Search {label} coverage differs from the ingestion manifest"
            )
    with SearchClient(
        endpoint, index_name, credential, api_version=SEARCH_API_VERSION
    ) as client:
        results = list(
            client.search(
                search_text="leave holiday policy",
                query_type="semantic",
                semantic_configuration_name="ask-hr-semantic",
                semantic_error_mode="fail",
                vector_queries=[
                    VectorizableTextQuery(
                        text="leave holiday policy",
                        fields="chunk_vector",
                        k_nearest_neighbors=50,
                    )
                ],
                select=["chunk_id", "document_id", "region"],
                top=5,
            )
        )
    if not results:
        raise RuntimeError("Hybrid Search validation returned no documents")
    return {
        "chunks": len(rows),
        "documents": len(expected_document_ids),
        "regions": sorted(expected_regions),
        "hybridQuery": results,
    }

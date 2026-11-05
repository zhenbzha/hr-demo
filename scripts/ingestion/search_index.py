"""Embed chunks and update Azure AI Search one source PDF at a time."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from functools import partial
from itertools import batched
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.core.pipeline import PipelineResponse
from azure.core.rest import HttpRequest, HttpResponse
from azure.search.documents import SearchClient, SearchIndexingBufferedSender
from azure.search.documents.indexes import SearchIndexClient
from openai import AzureOpenAI

from .search_schema import EMBEDDING_MODEL, validate_dimensions
from .textsplitter import EMBEDDING_TOKEN_LIMIT, token_count

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
    client: AzureOpenAI,
    documents: list[dict[str, Any]],
    deployment: str,
    dimensions: int,
) -> None:
    """
    Add an embedding vector to each search record.

    Read each record's ``semantic_text`` and store its embedding in
    ``chunk_vector``. Validate the model, vector length, and one result
    per input.

    Parameters
    ----------
    client : AzureOpenAI
        Azure OpenAI client used to generate embeddings.
        The caller is responsible for closing it.
    documents : list of dict
        Records to update. Each must contain nonempty ``semantic_text``
        that fits the embedding model's token limit.
    deployment : str
        Deployment name serving ``text-embedding-3-large``.
    dimensions : int
        Requested vector length; must match the Search schema.
    """
    validate_dimensions(dimensions)
    for document in documents:
        text = document.get("semantic_text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Every embedding input must contain semantic_text")
        if token_count(text) > EMBEDDING_TOKEN_LIMIT:
            raise ValueError(f"Embedding input exceeds {EMBEDDING_TOKEN_LIMIT} tokens")
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


def _immutable_field_settings(field: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize immutable settings for this index's scalar and vector fields.

    Azure supplies defaults for omitted flags. Compare their effective values
    rather than treating an explicit service default as a schema change.
    ``retrievable`` and ``searchAnalyzer`` are mutable and are not compared.

    Parameters
    ----------
    field : dict
        One field from the existing or proposed flat index definition.

    Returns
    -------
    dict
        Immutable properties with Azure's omitted-value defaults applied.
    """
    field_type = field["type"]
    vector = field.get("dimensions") is not None
    defaults = {
        "key": False,
        "searchable": field_type in {"Edm.String", "Collection(Edm.String)"},
        "filterable": not vector,
        "sortable": not field_type.startswith("Collection(") and field_type != "Edm.GeographyPoint",
        "facetable": not vector and field_type != "Edm.GeographyPoint",
        "stored": True,
    }
    return {
        name: field[name] if field.get(name) is not None else defaults.get(name)
        for name in (
            "type", "key", "searchable", "filterable", "sortable", "facetable",
            "stored", "analyzer", "indexAnalyzer", "normalizer", "dimensions",
            "vectorSearchProfile", "vectorEncoding",
        )
    }


def plan_index_update(
    *,
    client: SearchIndexClient,
    schema: dict[str, Any],
    recreate: bool,
) -> str:
    """
    Read the index and choose its update action without changing Azure.

    Parameters
    ----------
    client : SearchIndexClient
        Caller-owned Search administration client.
    schema : dict
        Desired definition returned by ``index_schema``.
    recreate : bool
        Explicitly request a rebuild even if an in-place update is possible.

    Returns
    -------
    {'create', 'rebuild', 'backfill', 'update'}
        Missing indexes are created. Removed fields or changed immutable
        properties require a rebuild. New fields require an in-place update
        and document backfill. Mutable settings need only an in-place update.
        Authentication, transport, and other service failures propagate.
    """
    try:
        existing = client.get_index(schema["name"]).as_dict()
    except ResourceNotFoundError:
        return "create"
    previous = {field["name"]: field for field in existing["fields"]}
    proposed = {field["name"]: field for field in schema["fields"]}
    if recreate or previous.keys() - proposed.keys():
        return "rebuild"
    if any(
        _immutable_field_settings(previous[name]) != _immutable_field_settings(proposed[name])
        for name in previous.keys() & proposed.keys()
    ):
        return "rebuild"
    return "backfill" if proposed.keys() - previous.keys() else "update"


def create_index(
    *,
    client: SearchIndexClient,
    schema: dict[str, Any],
    action: str,
) -> None:
    """
    Apply a previously planned index update.

    Parameters
    ----------
    client : SearchIndexClient
        Caller-owned Search administration client.
    schema : dict
        Complete desired index definition; existing fields are not merged.
    action : {'create', 'rebuild', 'backfill', 'update'}
        Result from ``plan_index_update``. Only ``rebuild`` deletes the index.
        The caller must save unfinished PDF IDs before applying this action.
    """
    if action not in {"create", "rebuild", "backfill", "update"}:
        raise ValueError(f"Unknown index update action: {action}")
    if action == "rebuild":
        client.delete_index(schema["name"])
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
    """
    Record acknowledgement errors and failed-item response details.

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
    sender_factory: Callable[..., SearchIndexingBufferedSender],
    actions: list[dict[str, Any]],
) -> None:
    """
    Submit batched uploads or deletions and verify each acknowledgement.

    Require unique chunk IDs and a single operation type per call.
    Raise RuntimeError for failed or missing indexing acknowledgements.
    The configured constructor creates a fresh sender for this operation's
    callbacks; this function closes that sender before returning.
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

    with sender_factory(
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
    sender_factory: Callable[..., SearchIndexingBufferedSender],
    documents: list[dict[str, Any]],
) -> None:
    """
    Create or replace each Search document and verify its indexing result.

    Use ``synchronize_document`` to also remove obsolete records for a PDF.
    Successful SDK acknowledgements do not guarantee immediate query visibility.

    Parameters
    ----------
    sender_factory : callable
        Function that creates a buffered Search writer for the target index.
        This function supplies the callbacks and closes the writer.
    documents : list of dict
        Chunk dictionaries with unique ``chunk_id`` values and
        fields matching the index, including generated embeddings.

    Raises
    ------
    RuntimeError
        Search reports failed or missing document acknowledgements.
    """
    _write_actions(
        sender_factory,
        [{**document, "@search.action": "upload"} for document in documents],
    )


def _document_chunk_ids(
    client: SearchClient,
    document_id: str,
    timeout: float = 120,
) -> set[str]:
    """
    Read existing chunk IDs for one PDF, not the entire index.

    Use the filterable document ID and keyset pagination. Return only keys;
    no chunk text or vectors are retrieved. Raise TimeoutError if enumeration
    exceeds timeout seconds.
    """
    chunk_ids: set[str] = set()
    document_filter = "document_id eq '" + document_id.replace("'", "''") + "'"
    last_key = ""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Search key enumeration timed out")
        key_filter = document_filter
        if last_key:
            escaped_key = last_key.replace("'", "''")
            key_filter += f" and chunk_id gt '{escaped_key}'"
        page = list(
            client.search(
                search_text="*",
                select=["chunk_id"],
                order_by=["chunk_id asc"],
                top=1000,
                filter=key_filter,
                timeout=remaining,
                read_timeout=min(30, remaining),
                connection_timeout=min(30, remaining),
            )
        )
        if not page:
            return chunk_ids
        keys = [item.get("chunk_id") for item in page]
        if any(
            not isinstance(key, str) or key <= last_key for key in keys
        ) or keys != sorted(set(keys)):
            raise RuntimeError(
                "Search key enumeration did not advance in unique key order"
            )
        chunk_ids.update(keys)
        if len(page) < 1000:
            return chunk_ids
        last_key = keys[-1]


def synchronize_document(
    *,
    client: SearchClient,
    sender_factory: Callable[..., SearchIndexingBufferedSender],
    document_id: str,
    documents: list[dict[str, Any]],
) -> int:
    """
    Replace one PDF's chunks, or remove them for a deleted PDF.

    Read this PDF's existing keys before writing. Require successful upload
    acknowledgements before deleting old keys absent from the new chunk set.
    Run only one writer at a time. No read-back verification or visibility
    polling is performed; queries may briefly see old and new records.

    Parameters
    ----------
    client : SearchClient
        Client used to read existing keys for this PDF only.
        The caller is responsible for closing it.
    sender_factory : callable
        Function that creates buffered writers for the same index as
        ``client``. Uploads and deletions use separate writers.
    document_id : str
        Stable source PDF ID stored in each chunk's ``document_id`` field.
    documents : list of dict
        Complete replacement chunk set for this PDF, including embeddings.
        Each must have a unique ``chunk_id`` and the supplied ``document_id``.
        An empty list deletes this PDF's existing chunks.

    Returns
    -------
    int
        Number of obsolete keys with successful deletion acknowledgements.
    """
    uploaded_keys = {document["chunk_id"] for document in documents}
    if (
        not isinstance(document_id, str)
        or not document_id
        or len(uploaded_keys) != len(documents)
        or any(not isinstance(key, str) or not key for key in uploaded_keys)
        or any(document.get("document_id") != document_id for document in documents)
    ):
        raise ValueError("Expected unique chunk IDs belonging to the supplied PDF")
    previous = _document_chunk_ids(client, document_id)
    if documents:
        upload_documents(
            sender_factory=sender_factory,
            documents=documents,
        )
    stale = previous - uploaded_keys
    _write_actions(
        sender_factory,
        [{"@search.action": "delete", "chunk_id": key} for key in sorted(stale)],
    )
    return len(stale)

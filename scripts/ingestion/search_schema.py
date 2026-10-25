"""Define Search fields, semantic ranking, and query-time vectorization."""

from typing import Any

DEFAULT_INDEX_NAME = "ask-hr-docs"
DEFAULT_VECTOR_DIMENSIONS = 1024
EMBEDDING_MODEL = "text-embedding-3-large"
MAX_VECTOR_DIMENSIONS = 3072


def validate_dimensions(dimensions: int) -> None:
    if type(dimensions) is not int or not 1 <= dimensions <= MAX_VECTOR_DIMENSIONS:
        raise ValueError(
            f"{EMBEDDING_MODEL} requires 1-{MAX_VECTOR_DIMENSIONS} dimensions"
        )


def _text_field(
    name: str,
    *,
    searchable: bool = False,
    filterable: bool = False,
    facetable: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "type": "Edm.String",
        "retrievable": True,
        "searchable": searchable,
        "filterable": filterable,
        "facetable": facetable,
    }


def _index_fields(dimensions: int) -> list[dict[str, Any]]:
    return [
        {**_text_field("chunk_id", filterable=True), "key": True, "sortable": True},
        _text_field("document_id", filterable=True, facetable=True),
        _text_field("document_title", searchable=True, filterable=True),
        _text_field("region", filterable=True, facetable=True),
        _text_field("language", filterable=True),
        _text_field("policy_topic", searchable=True, filterable=True, facetable=True),
        _text_field("version"),
        _text_field("source_uri"),
        _text_field("document_path"),
        _text_field("annotation_path"),
        _text_field("heading_text", searchable=True),
        {**_text_field("chunk_text", searchable=True), "analyzer": "standard.lucene"},
        {
            **_text_field("semantic_text", searchable=True),
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
        {"name": "page_start", "type": "Edm.Int32", "retrievable": True},
        {"name": "page_end", "type": "Edm.Int32", "retrievable": True},
    ]


def _vector_configuration(endpoint: str, deployment: str) -> dict[str, Any]:
    return {
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
                    "resourceUri": endpoint.rstrip("/"),
                    "deploymentId": deployment,
                    "modelName": EMBEDDING_MODEL,
                    "authIdentity": None,
                },
            }
        ],
    }


def index_schema(
    *,
    index_name: str,
    openai_endpoint: str,
    embedding_deployment: str,
    dimensions: int,
) -> dict[str, Any]:
    """Build the schema with matching index and query vector settings."""
    validate_dimensions(dimensions)
    return {
        "name": index_name,
        "fields": _index_fields(dimensions),
        "vectorSearch": _vector_configuration(openai_endpoint, embedding_deployment),
        "semantic": {
            "defaultConfiguration": "ask-hr-semantic",
            "configurations": [
                {
                    "name": "ask-hr-semantic",
                    "prioritizedFields": {
                        "titleField": {"fieldName": "document_title"},
                        "prioritizedContentFields": [
                            {"fieldName": "semantic_text"},
                            {"fieldName": "chunk_text"},
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

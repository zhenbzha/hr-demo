from __future__ import annotations

import os
from typing import Any, Protocol
from urllib.parse import quote, urlparse


class PlatformContext(Protocol):
    user_id_key: str | None


class SecurityContext(Protocol):
    client_headers: dict[str, str]
    platform_context: PlatformContext


def require_security_context(context: SecurityContext) -> tuple[str, str]:
    user_search_token = context.client_headers.get(
        "x-client-search-authorization"
    )
    if not user_search_token:
        raise PermissionError(
            "A delegated Azure AI Search token is required"
        )
    user_id = context.platform_context.user_id_key
    if not user_id:
        raise PermissionError("A trusted represented-user identity is required")
    return user_search_token, user_id


def validated_search_endpoint() -> str:
    endpoint = os.environ["AZURE_SEARCH_ENDPOINT"].rstrip("/")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("AZURE_SEARCH_ENDPOINT must be a fixed HTTPS origin")
    return endpoint


def build_search_url(search_endpoint: str) -> str:
    knowledge_base = os.environ["AZURE_SEARCH_KNOWLEDGE_BASE_NAME"]
    return (
        f"{search_endpoint}/knowledgebases/{quote(knowledge_base, safe='')}"
        "/retrieve?api-version=2026-08-01-preview"
    )


def build_search_headers(
    service_token: str,
    user_search_token: str,
) -> dict[str, str]:
    return {
        "Authorization": "Bearer" + " " + service_token,
        "x-ms-query-source-authorization": user_search_token,
        "Content-Type": "application/json",
    }


def build_search_body(question: str, region: str) -> dict[str, Any]:
    return {
        "intents": [{"type": "semantic", "search": question}],
        "outputMode": "extractiveData",
        "retrievalReasoningEffort": {"kind": "minimal"},
        "includeActivity": True,
        "knowledgeSourceParams": [
            {
                "knowledgeSourceName": os.environ[
                    "AZURE_SEARCH_KNOWLEDGE_SOURCE_NAME"
                ],
                "kind": "searchIndex",
                "filterAddOn": f"region eq '{region}'",
                "includeReferences": True,
                "includeReferenceSourceData": True,
                "alwaysQuerySource": True,
                "resultsProcessing": "rerank",
                "rerankerThreshold": 1.5,
            }
        ],
    }


def compact_search_evidence(
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for scoped_result in evidence:
        scope = scoped_result.get("scope")
        result = scoped_result.get("result")
        if not isinstance(result, dict):
            continue
        references = result.get("references")
        if not isinstance(references, list):
            continue
        scoped_evidence = []
        for reference in references:
            if not isinstance(reference, dict):
                continue
            source = reference.get("sourceData")
            if not isinstance(source, dict):
                continue
            content = source.get("chunk_text")
            if not isinstance(content, str) or not content.strip():
                continue
            source_id = source.get("chunk_id")
            if not isinstance(source_id, str) or not source_id:
                raise ValueError("Policy evidence is missing its source ID")
            scoped_evidence.append(
                {
                    "source_id": source_id,
                    "document_title": source.get("document_title"),
                    "region": source.get("region"),
                    "page_start": source.get("page_start"),
                    "page_end": source.get("page_end"),
                    "content": content,
                }
            )
        compact.append(
            {
                "scope": scope,
                "references": scoped_evidence,
            }
        )
    return compact

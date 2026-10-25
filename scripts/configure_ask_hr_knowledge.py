#!/usr/bin/env python3
"""Create and validate the Ask HR Foundry IQ knowledge source and base."""

from __future__ import annotations

import argparse
import json
import os
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


def knowledge_source_definition(
    *,
    name: str,
    index_name: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "searchIndex",
        "description": (
            "Ask HR indexed Global, India, Finland, and Hong Kong policies."
        ),
        "encryptionKey": None,
        "searchIndexParameters": {
            "searchIndexName": index_name,
            "semanticConfigurationName": "ask-hr-semantic",
            "searchFields": [
                {"name": "semantic_text"},
                {"name": "chunk_text"},
                {"name": "document_title"},
                {"name": "heading_text"},
                {"name": "policy_topic"},
                {"name": "chunk_vector"},
            ],
            "sourceDataFields": [
                {"name": "chunk_id"},
                {"name": "document_id"},
                {"name": "document_title"},
                {"name": "region"},
                {"name": "language"},
                {"name": "policy_topic"},
                {"name": "version"},
                {"name": "source_uri"},
                {"name": "document_path"},
                {"name": "annotation_path"},
                {"name": "heading_text"},
                {"name": "chunk_text"},
                {"name": "semantic_text"},
                {"name": "page_start"},
                {"name": "page_end"},
            ],
        },
    }


def knowledge_base_definition(
    *,
    name: str,
    knowledge_source_name: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": (
            "Ask HR extractive knowledge base. The hosted agent performs "
            "region classification, local-versus-Global precedence, and "
            "grounded answer synthesis."
        ),
        "outputMode": "extractiveData",
        "knowledgeSources": [{"name": knowledge_source_name}],
        "models": [],
        "encryptionKey": None,
        "retrievalReasoningEffort": {"kind": "minimal"},
        "retrieveDefaults": {
            "maxRuntimeInSeconds": 30,
            "maxOutputDocuments": 12,
            "maxOutputSizeInTokens": 12000,
        },
    }


def put_object(
    *,
    endpoint: str,
    collection: str,
    name: str,
    body: dict[str, Any],
    credential: TokenCredential,
) -> dict[str, Any]:
    url = (
        f"{endpoint.rstrip('/')}/{collection}/"
        f"{urllib.parse.quote(name)}?api-version={SEARCH_API_VERSION}"
    )
    _, _, result = request_json(
        "PUT",
        url,
        headers=authorization_headers(credential, SEARCH_SCOPE),
        json_body=body,
    )
    return result


def retrieve(
    *,
    endpoint: str,
    knowledge_base_name: str,
    knowledge_source_name: str,
    credential: TokenCredential,
) -> dict[str, Any]:
    url = (
        f"{endpoint.rstrip('/')}/knowledgebases/"
        f"{urllib.parse.quote(knowledge_base_name)}/retrieve"
        f"?api-version={SEARCH_API_VERSION}"
    )
    _, _, result = request_json(
        "POST",
        url,
        headers=authorization_headers(credential, SEARCH_SCOPE),
        json_body={
            "intents": [
                {
                    "type": "semantic",
                    "search": "香港僱員的強積金安排是甚麼？",
                }
            ],
            "outputMode": "extractiveData",
            "retrievalReasoningEffort": {"kind": "minimal"},
            "includeActivity": True,
            "knowledgeSourceParams": [
                {
                    "knowledgeSourceName": knowledge_source_name,
                    "kind": "searchIndex",
                    "filterAddOn": ("region eq 'hongkong' or region eq 'global'"),
                    "alwaysQuerySource": True,
                    "includeReferences": True,
                }
            ],
        },
        timeout=180,
    )
    serialized = json.dumps(result, ensure_ascii=False)
    expected_title = "HongKong HR Payroll MPF and Benefits zh-HK"
    if expected_title not in serialized or "強制性公積金" not in serialized:
        raise RuntimeError(
            "Foundry IQ validation did not return Hong Kong MPF evidence:\n"
            + json.dumps(result, ensure_ascii=False, indent=2)
        )
    activities = result.get("activity", [])
    search_activities = [
        activity for activity in activities if activity.get("type") == "searchIndex"
    ]
    expected_filter = "region eq 'hongkong' or region eq 'global'"
    if not search_activities or (
        search_activities[0].get("searchIndexArguments", {}).get("filter")
        != expected_filter
    ):
        raise RuntimeError(
            "Foundry IQ validation did not apply the expected region filter"
        )
    if "India HR" in serialized or "Finland HR" in serialized:
        raise RuntimeError("Foundry IQ validation returned an unrelated local region")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--search-endpoint",
        default=os.environ.get("AZURE_SEARCH_ENDPOINT"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output") / "foundry-iq" / "validation.json",
    )
    parser.add_argument(
        "--index-name",
        default=os.environ.get("AZURE_SEARCH_INDEX_NAME"),
    )
    parser.add_argument(
        "--knowledge-source-name",
        default=os.environ.get("AZURE_SEARCH_KNOWLEDGE_SOURCE"),
    )
    parser.add_argument(
        "--knowledge-base-name",
        default=os.environ.get("AZURE_SEARCH_KNOWLEDGE_BASE"),
    )
    args = parser.parse_args()
    required = {
        "--search-endpoint or AZURE_SEARCH_ENDPOINT": args.search_endpoint,
        "--index-name or AZURE_SEARCH_INDEX_NAME": args.index_name,
        (
            "--knowledge-source-name or AZURE_SEARCH_KNOWLEDGE_SOURCE"
        ): args.knowledge_source_name,
        "--knowledge-base-name or AZURE_SEARCH_KNOWLEDGE_BASE": (
            args.knowledge_base_name
        ),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error("Missing required configuration: " + ", ".join(missing))
    return args


def main() -> None:
    args = parse_args()
    endpoint = args.search_endpoint
    index_name = args.index_name
    knowledge_source_name = args.knowledge_source_name
    knowledge_base_name = args.knowledge_base_name

    with AzureCliCredential() as credential:
        knowledge_source = put_object(
            endpoint=endpoint,
            collection="knowledgesources",
            name=knowledge_source_name,
            body=knowledge_source_definition(
                name=knowledge_source_name,
                index_name=index_name,
            ),
            credential=credential,
        )
        knowledge_base = put_object(
            endpoint=endpoint,
            collection="knowledgebases",
            name=knowledge_base_name,
            body=knowledge_base_definition(
                name=knowledge_base_name,
                knowledge_source_name=knowledge_source_name,
            ),
            credential=credential,
        )
        validation = retrieve(
            endpoint=endpoint,
            knowledge_base_name=knowledge_base_name,
            knowledge_source_name=knowledge_source_name,
            credential=credential,
        )
    output = {
        "knowledgeSource": knowledge_source,
        "knowledgeBase": knowledge_base,
        "validation": validation,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "knowledgeSource": knowledge_source_name,
                "knowledgeBase": knowledge_base_name,
                "validation": str(args.output),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

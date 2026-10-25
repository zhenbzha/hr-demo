"""Collect hosted-agent answers, citations, and diagnostic metadata."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from azure.core.credentials import TokenCredential
from azure.identity import get_bearer_token_provider
from ingestion.azure_http import SEARCH_SCOPE, request_json

CITATION_FIELDS = (
    "chunk_id",
    "document_id",
    "document_title",
    "region",
    "language",
    "version",
    "page_start",
    "page_end",
    "document_path",
    "annotation_path",
    "chunk_text",
)


@dataclass(frozen=True)
class AgentResult:
    response: str
    references: list[dict[str, Any]]
    response_id: str
    tool_calls: list[dict[str, Any]]
    diagnostics: dict[str, Any] | None

    @property
    def citations(self) -> list[dict[str, Any]]:
        return [
            {field: reference[field] for field in CITATION_FIELDS if field in reference}
            for reference in self.references
        ]

    @property
    def context(self) -> str:
        return (
            "\n\n".join(
                reference["chunk_text"]
                for reference in self.references
                if isinstance(reference.get("chunk_text"), str)
                and reference["chunk_text"]
            )
            or "No policy evidence was returned in this response."
        )


def build_captured_agent_query(
    row: dict[str, Any], generation_instructions: str | None
) -> list[dict[str, Any]]:
    """Use the recorded model conversation, without inventing missing turns."""
    messages: list[dict[str, Any]] = []
    query = row["query"]
    generation_input = row.get("generation_input")
    if generation_input is not None:
        if not isinstance(generation_input, str) or not generation_input.strip():
            raise ValueError("Captured generation input must be nonempty text")
        if (
            not isinstance(generation_instructions, str)
            or not generation_instructions.strip()
        ):
            raise ValueError("Captured generation instructions are required")
        captured = json.loads(generation_input)
        if (
            not isinstance(captured, dict)
            or captured.get("question") != row["query"]
            or captured.get("authorized_evidence") != json.loads(row["context"])
        ):
            raise ValueError("Evaluation context differs from the captured model input")
        messages.append(
            {
                "role": "system",
                "content": [{"type": "text", "text": generation_instructions}],
            }
        )
        query = generation_input
    messages.append({"role": "user", "content": [{"type": "text", "text": query}]})
    return messages


def endpoint_version_status(endpoint: str, agent_name: str, version: str) -> str:
    """Validate the Responses URL and report how its version is selected.

    Raise ValueError if the URL conflicts with the selected agent or
    version. An unversioned URL routes to the active version, so its status
    identifies the supplied version as an operator label.
    """
    url = urlsplit(endpoint)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
    ):
        raise ValueError(
            "Agent endpoint must be an HTTPS URL without embedded credentials"
        )
    segments = [unquote(segment) for segment in url.path.split("/") if segment]
    if "agents" not in segments or segments[-1] != "responses":
        raise ValueError("Use the deployed Foundry agent's Responses endpoint")
    agent_index = segments.index("agents")
    if agent_index + 1 >= len(segments) or segments[agent_index + 1] != agent_name:
        raise ValueError("Agent name does not match the supplied endpoint")
    if "versions" in segments:
        version_index = segments.index("versions")
        if version_index + 1 >= len(segments) or segments[version_index + 1] != version:
            raise ValueError("Agent version does not match the supplied endpoint")
        return "versioned_endpoint"
    return "operator_supplied_label; endpoint_routes_to_active_version"


def _tool_references(output: dict[str, Any]) -> list[dict[str, Any]]:
    references = []
    evidence_items = output.get("evidence", [])
    if not isinstance(evidence_items, list):
        raise ValueError("Agent evidence must be an array")
    for evidence in evidence_items:
        if not isinstance(evidence, dict) or not isinstance(
            evidence.get("result"), dict
        ):
            raise ValueError("Agent evidence has no valid result object")
        sources = evidence["result"].get("references", [])
        if not isinstance(sources, list):
            raise ValueError("Agent references must be an array")
        for reference in sources:
            if not isinstance(reference, dict) or not isinstance(
                reference.get("sourceData"), dict
            ):
                raise ValueError("Agent citation has no valid sourceData")
            references.append(reference["sourceData"])
    return references


def _diagnostics(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Decode optional Ask HR diagnostics, returning None when absent."""
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Agent metadata must be an object")
    raw = metadata.get("ask_hr_diagnostics")
    if raw is None:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("ask_hr_diagnostics must contain a JSON object")
    return parsed


def parse_agent_response(payload: dict[str, Any]) -> AgentResult:
    """Parse a completed response into its answer, citations, and diagnostics.

    Require nonempty answer text and valid evidence objects.
    """
    if (
        payload.get("status") != "completed"
        or payload.get("error")
        or payload.get("incomplete_details")
    ):
        raise RuntimeError(
            f"Agent response did not complete: status={payload.get('status')}, error={payload.get('error')}"
        )
    response_id = payload.get("id")
    output = payload.get("output")
    if not isinstance(response_id, str) or not isinstance(output, list):
        raise ValueError("Agent response must include an ID and output array")
    text_parts: list[str] = []
    references = []
    tool_calls = []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError("Agent output items must be objects")
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text_parts.append(content["text"])
        elif item.get("type") == "function_call":
            tool_calls.append(
                {key: item.get(key) for key in ("name", "call_id", "arguments")}
            )
        elif item.get("type") == "function_call_output":
            tool_output = json.loads(item["output"])
            if not isinstance(tool_output, dict):
                raise ValueError("Ask HR tool output must be a JSON object")
            references.extend(_tool_references(tool_output))
    answer = "\n\n".join(text_parts).strip()
    if not answer:
        raise RuntimeError("Completed agent response contains no answer text")
    return AgentResult(
        answer, references, response_id, tool_calls, _diagnostics(payload)
    )


def invoke_agent(
    *,
    endpoint: str,
    agent_name: str,
    credential: TokenCredential,
    user_id: str,
    query: str,
    timeout: float,
    max_retry_seconds: float,
) -> AgentResult:
    """Send a query to the hosted agent and return its parsed response.

    Use the supplied credential for Foundry and delegated Search access.
    """
    ai_token = get_bearer_token_provider(credential, "https://ai.azure.com/.default")
    search_token = get_bearer_token_provider(credential, SEARCH_SCOPE)

    def headers() -> dict[str, str]:
        return {
            "Authorization": "Bearer " + ai_token(),
            "x-ms-user-identity": user_id,
            "x-client-search-authorization": search_token(),
        }

    _, _, payload = request_json(
        "POST",
        endpoint,
        headers=headers,
        json_body={"model": agent_name, "input": query, "store": False},
        timeout=timeout,
        max_retry_seconds=max_retry_seconds,
    )
    return parse_agent_response(payload)

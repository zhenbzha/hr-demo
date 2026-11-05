"""Collect hosted-agent answers, citations, and structured response items."""

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
    """
    An Ask HR answer and its supporting source information.

    ``citations`` contains source details for the answer. ``context``
    preserves the text and source labels of the cited passages for evaluation.

    Attributes
    ----------
    response : str
        Employee-facing answer text.
    references : list of dict
        Cited passages and source details returned by Ask HR.
    response_id : str
        Foundry ID used to locate this response in logs and reports.
    tool_calls : list of dict
        Tool calls included in the response.
    output_items : list of dict or None, default=None
        Original Responses output items, including messages and tool results.
        None identifies a local capture containing only answer text.
    """

    response: str
    references: list[dict[str, Any]]
    response_id: str
    tool_calls: list[dict[str, Any]]
    output_items: list[dict[str, Any]] | None = None

    @property
    def citations(self) -> list[dict[str, Any]]:
        return [
            {field: reference[field] for field in CITATION_FIELDS if field in reference}
            for reference in self.references
        ]

    @property
    def context(self) -> str:
        passages = [
            reference
            for reference in self.citations
            if isinstance(reference.get("chunk_text"), str) and reference["chunk_text"]
        ]
        return (
            json.dumps(passages, ensure_ascii=False)
            if passages
            else "No policy evidence was returned in this response."
        )


def captured_response_items(row: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Convert recorded response items to Foundry evaluation messages.

    Parameters
    ----------
    row : dict
        Saved answer with ``response`` and optional original ``output_items``.

    Returns
    -------
    list of dict
        Messages with explicit roles and typed content. Actual Responses
        function calls/results become ``tool_call``/``tool_result`` content,
        preserving their names, arguments, call IDs, results, and order.
        Text-only captures produce one assistant message containing the
        recorded answer; missing tool calls are not reconstructed.

    Raises
    ------
    ValueError
        An output item has no supported message representation, or a
        function call's arguments are not valid JSON.
    """
    if "output_items" in row:
        messages = []
        for item in row["output_items"]:
            if item.get("type") == "function_call":
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_call",
                                "tool_call_id": item["call_id"],
                                "name": item["name"],
                                "arguments": json.loads(item["arguments"]),
                            }
                        ],
                    }
                )
            elif item.get("type") == "function_call_output":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["call_id"],
                        "content": [
                            {"type": "tool_result", "tool_result": item["output"]}
                        ],
                    }
                )
            elif item.get("role") in {
                "assistant", "developer", "system", "tool", "user",
            }:
                messages.append(item)
            else:
                raise ValueError(
                    f"Unsupported response item for evaluation: {item.get('type')}"
                )
        return messages
    return [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": row["response"]}],
        }
    ]


def build_captured_agent_query(
    row: dict[str, Any], generation_instructions: str | None
) -> list[dict[str, Any]]:
    """
    Build evaluator messages from a saved question or model conversation.

    Parameters
    ----------
    row : dict
        Saved evaluation row containing the question (``query``) and policy
        text (``context``). Optional ``generation_input`` is the saved JSON
        user message containing the question and supporting policy text.
    generation_instructions : str or None
        System instructions saved with the model request.
        Required when ``row`` includes ``generation_input``.

    Returns
    -------
    list of dict
        Foundry messages containing the saved system and user turns when
        available, otherwise the user question.

    Raises
    ------
    ValueError
        Saved inputs are invalid, required instructions are missing, or
        the recorded question or evidence differs from the evaluation row.
    """
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
    """
    Validate the Responses URL and report how its version is selected.

    Parameters
    ----------
    endpoint : str
        Full HTTPS URL of the agent's Responses API.
    agent_name : str
        Agent name expected in the URL.
    version : str
        Version selected in the URL, or a label supplied for an endpoint
        that uses the active version.

    Returns
    -------
    str
        ``versioned_endpoint`` when the URL selects the supplied version.
        For active-version URLs, a status indicating that the supplied
        version label could not be verified.

    Raises
    ------
    ValueError
        The URL is invalid or conflicts with the selected agent
        or version.
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


def parse_agent_response(payload: dict[str, Any]) -> AgentResult:
    """
    Parse a completed response while preserving its original output items.

    Parameters
    ----------
    payload : dict
        JSON object returned by the Ask HR Responses endpoint.
        Citation sources come from function-call output evidence.

    Returns
    -------
    AgentResult
        Answer, source references, response ID, tool calls, and original
        structured output items.

    Raises
    ------
    RuntimeError
        The response is incomplete, errored, or has no answer.
    ValueError
        Response fields or evidence are malformed.
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
    return AgentResult(answer, references, response_id, tool_calls, output)


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
    """
    Send a query to the hosted agent and return its parsed response.

    Parameters
    ----------
    endpoint : str
        Full HTTPS URL of a compatible Ask HR Responses endpoint.
    agent_name : str
        Agent name sent in the request's ``model`` field.
    credential : TokenCredential
        Entra credential used to obtain Foundry and Search tokens.
    user_id : str
        Represented user's Entra object ID for ``x-ms-user-identity``.
    query : str
        Evaluation question sent as the agent input.
    timeout : float
        Initial HTTP request timeout in seconds.
    max_retry_seconds : float
        Retry budget in seconds, measured from the first
        attempt. Zero disables retries; retry attempts also respect the
        remaining budget.

    Returns
    -------
    AgentResult
        Parsed answer, source references, response ID, and structured output.
        The request uses ``store: false``; the caller decides how to retain
        the collected evaluation data.
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

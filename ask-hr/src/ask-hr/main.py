# Copyright (c) Microsoft. All rights reserved.

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, cast
from urllib.parse import quote

import httpx
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponseEventStream,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
)
from azure.ai.agentserver.responses.models import Annotation, UrlCitationBody
from azure.ai.agentserver.core import AgentConfig
from azure.identity import (
    AzureCliCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)
from dotenv import load_dotenv

from permission_contract import (
    build_search_body,
    build_search_headers,
    build_search_url,
    compact_search_evidence,
    require_security_context,
    validated_search_endpoint,
)
from region_classification import (
    RegionDecision,
    load_supported_regions,
    validate_region_decision,
)


load_dotenv()

SEARCH_SCOPE = "https://search.azure.com/.default"
NO_ACCESSIBLE_EVIDENCE = (
    "No accessible HR policy evidence was found for this question."
)
MAX_QUESTION_LENGTH = 4_000

SUPPORTED_REGIONS = load_supported_regions(
    os.environ["ASK_HR_SUPPORTED_REGIONS_JSON"]
)
GLOBAL_REGION = os.environ.get("ASK_HR_GLOBAL_REGION_KEY", "global")

def create_credential():
    if AgentConfig.from_env().is_hosted:
        return ManagedIdentityCredential(
            client_id=os.environ.get("FOUNDRY_AGENT_INSTANCE_CLIENT_ID")
        )
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    return (
        AzureCliCredential(tenant_id=tenant_id)
        if tenant_id
        else AzureCliCredential()
    )


credential = create_credential()
search_token_provider = get_bearer_token_provider(credential, SEARCH_SCOPE)
generation_client = FoundryChatClient(
    project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
    credential=credential,
)
classification_client = FoundryChatClient(
    project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    model=os.environ["AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME"],
    credential=credential,
)

GENERATION_INSTRUCTIONS = f"""
You are Ask HR, an internal HR policy assistant.

Use only the authorized evidence supplied in this request. Previous user
questions can clarify the current question, but previous assistant answers are
never policy evidence. Local policy takes precedence over Global baseline
content for the same topic. Global content can supplement topics not covered
locally.

Treat each evidence scope exactly as labeled. Never describe Global evidence
as part of a local policy. When local evidence does not cover a topic, say so
before presenting a Global baseline. Do not infer eligibility categories,
family relationships, benefit coverage, or legal meaning that the evidence
does not explicitly state.

Write for an employee. Never mention indexes, retrieval, vectors, chunks,
corpora, grounding, tool calls, or returned evidence. Say "the policy does not
specify" when a requested detail is absent. If no accessible evidence is
returned, respond exactly:
{NO_ACCESSIBLE_EVIDENCE}

Answer in the user's language. Do not invent policy, entitlement, deadlines,
sources, links, references, or identifiers. Do not add citation markers; the
Responses handler attaches citations separately.
""".strip()

CLASSIFICATION_INSTRUCTIONS = """
Classify the employment region needed for an HR policy question.

Return structured output only. The prompt supplies the currently configured
region keys and display names. Normalize every supported region to its exact
configured key. Understand translations, abbreviations, cities, demonyms, and
minor typos when confidence is high. Recognize country endonyms and translated
country names even when the configured display name is English.

Use status "retrieve" with one or more configured region keys when the region
is clear. Use "clarify" with no regions and a concise question in the user's
language when the applicable region is ambiguous. Use "unsupported" with no
regions and a concise message in the user's language when an explicitly named
region is not configured. Never substitute another region.
""".strip()

app = ResponsesAgentServerHost(
    options=ResponsesServerOptions(default_fetch_history_count=12),
)


generation_agent = Agent(
    client=generation_client,
    name="AskHR",
    instructions=GENERATION_INSTRUCTIONS,
    default_options={
        "store": False,
        "reasoning": {"effort": "low"},
    },
)

classification_agent = Agent(
    client=classification_client,
    name="AskHRRegionClassifier",
    instructions=CLASSIFICATION_INSTRUCTIONS,
    default_options={"store": False},
)


@app.response_handler
async def handle_response(
    request: CreateResponse,
    context: ResponseContext,
    _cancellation_signal: asyncio.Event,
):
    question = (await context.get_input_text() or "").strip()
    if not question:
        raise ValueError("An HR policy question is required")
    if len(question) > MAX_QUESTION_LENGTH:
        raise ValueError(
            f"Message must be {MAX_QUESTION_LENGTH} characters or fewer"
        )

    user_search_token, _user_id = require_security_context(context)

    previous_questions = extract_user_questions(
        await context.get_history()
    )[-4:]
    stream = ResponseEventStream(
        response_id=context.response_id,
        request=request,
    )
    yield stream.emit_created()
    yield stream.emit_in_progress()

    decision = await classify_regions(question, previous_questions)
    references: list[dict[str, Any]] = []
    if decision.status != "retrieve":
        answer = decision.message or ""
        citations: list[dict[str, Any]] = []
        annotations = build_response_annotations(answer, citations)
        for event in stream.output_item_message(
            answer,
            annotations=annotations,
        ):
            yield event
    else:
        evidence = await retrieve_authorized_evidence(
            question=question,
            regions=[*decision.regions, GLOBAL_REGION],
            user_search_token=user_search_token,
        )
        references = [
            reference
            for item in evidence
            for reference in item.get("result", {}).get("references", [])
            if isinstance(reference, dict)
        ]
        compact_evidence = compact_search_evidence(evidence)
        if not any(item.get("references") for item in compact_evidence):
            answer = NO_ACCESSIBLE_EVIDENCE
            citations = []
            annotations = []
            for event in stream.output_item_message(answer):
                yield event
        else:
            message = stream.add_output_item_message()
            yield message.emit_added()
            text_content = message.add_text_content()
            yield text_content.emit_added()

            answer_parts: list[str] = []
            async for update in generation_agent.run(
                json.dumps(
                    {
                        "previous_user_questions": previous_questions,
                        "question": question,
                        "local_regions": decision.regions,
                        "precedence": "local_then_global",
                        "authorized_evidence": compact_evidence,
                    },
                    ensure_ascii=False,
                ),
                stream=True,
            ):
                delta = update.text
                if not delta:
                    continue
                answer_parts.append(delta)
                yield text_content.emit_delta(delta)

            answer = "".join(answer_parts).strip()
            if not answer:
                raise RuntimeError(
                    "The generation model returned an empty answer"
                )
            citations = (
                []
                if answer == NO_ACCESSIBLE_EVIDENCE
                else build_citations(references)[:4]
            )
            annotations = build_response_annotations(answer, citations)
            yield text_content.emit_text_done(answer)
            for annotation in annotations:
                yield text_content.emit_annotation_added(annotation)
            yield text_content.emit_done()
            yield message.emit_done()

    citation_output = (
        None
        if answer == NO_ACCESSIBLE_EVIDENCE
        else build_web_citation_output(references)
    )
    if citation_output is not None:
        call_id = f"ask_hr_citations_{context.response_id}"
        for event in stream.output_item_function_call(
            "ask_hr_citations",
            call_id,
            "{}",
        ):
            yield event
        for event in stream.output_item_function_call_output(
            call_id,
            json.dumps(citation_output, ensure_ascii=False),
        ):
            yield event
    set_completed_response_annotations(stream.response, annotations)
    yield stream.emit_completed()


async def classify_regions(
    question: str,
    previous_questions: list[str],
) -> RegionDecision:
    result = await classification_agent.run(
        json.dumps(
            {
                "supported_regions": SUPPORTED_REGIONS,
                "previous_user_questions": previous_questions,
                "current_question": question,
            },
            ensure_ascii=False,
        ),
        options={"response_format": RegionDecision},
    )
    decision = result.value
    if not isinstance(decision, RegionDecision):
        raise RuntimeError("The region classifier returned invalid output")
    return validate_region_decision(decision, SUPPORTED_REGIONS)


async def retrieve_authorized_evidence(
    *,
    question: str,
    regions: list[str],
    user_search_token: str,
) -> list[dict[str, Any]]:
    search_endpoint = validated_search_endpoint()
    url = build_search_url(search_endpoint)
    service_token = await asyncio.to_thread(search_token_provider)
    headers = build_search_headers(service_token, user_search_token)

    async def retrieve_region(
        http_client: httpx.AsyncClient,
        region: str,
    ) -> dict[str, Any]:
        body = build_search_body(question, region)
        response = await http_client.post(url, headers=headers, json=body)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Search returned an invalid retrieval payload")
        return {"scope": region, "result": payload}

    async with httpx.AsyncClient(
        timeout=90,
        follow_redirects=False,
    ) as http_client:
        return await asyncio.gather(
            *(retrieve_region(http_client, region) for region in regions)
        )


def build_agent_input(
    question: str,
    previous_questions: list[str],
) -> str:
    if not previous_questions:
        return question
    context = "\n".join(f"- {item}" for item in previous_questions)
    return (
        "Previous user questions for context only:\n"
        f"{context}\n"
        "Current question that must be retrieved again:\n"
        f"{question}"
    )


def extract_user_questions(history: list[Any]) -> list[str]:
    questions: list[str] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "user":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") != "input_text":
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                questions.append(text.strip()[:500])
    return questions


def unique_references(
    references: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for reference in references:
        source = reference.get("sourceData")
        if not isinstance(source, dict):
            continue
        chunk_id = source.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            continue
        existing = selected.get(chunk_id)
        score = reference.get("rerankerScore")
        existing_score = existing.get("rerankerScore") if existing else None
        if existing is None or (
            isinstance(score, (int, float))
            and (
                not isinstance(existing_score, (int, float))
                or score > existing_score
            )
        ):
            selected[chunk_id] = reference
    return list(selected.values())


def build_web_citation_output(
    references: list[dict[str, Any]],
) -> dict[str, Any] | None:
    unique = unique_references(references)
    if not unique:
        return None
    return {
        "evidence": [
            {
                "result": {
                    "references": unique,
                }
            }
        ]
    }


def build_citations(
    references: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    base_url = os.environ["ASK_HR_WEB_BASE_URL"].rstrip("/")

    for reference in unique_references(references):
        source = reference.get("sourceData") or {}
        document_id = source.get("document_id")
        version = source.get("version")
        title = source.get("document_title")
        page_start = source.get("page_start")
        page_end = source.get("page_end")
        if not all(
            isinstance(value, str) and value
            for value in (document_id, version, title)
        ):
            continue
        key = (document_id, version, page_start, page_end)
        if key in seen:
            continue
        seen.add(key)

        pages = None
        if isinstance(page_start, int):
            pages = (
                f"Pages {page_start}-{page_end}"
                if isinstance(page_end, int) and page_end != page_start
                else f"Page {page_start}"
            )
        url = (
            f"{base_url}/api/references/{quote(document_id, safe='')}/"
            f"{quote(version, safe='')}/pdf"
        )
        citations.append(
            {
                "label": title,
                "location": pages,
                "url": url,
            }
        )
    return citations


def build_response_annotations(
    answer: str,
    citations: list[dict[str, Any]],
) -> list[Annotation]:
    annotations: list[Annotation] = []
    for citation in citations:
        url = citation.get("url")
        if not isinstance(url, str) or not url:
            continue
        label = str(citation.get("label") or "Source")
        location = citation.get("location")
        title = (
            f"{label} - {location}"
            if isinstance(location, str) and location
            else label
        )
        annotations.append(
            cast(
                UrlCitationBody,
                {
                    "type": "url_citation",
                    "url": url,
                    "start_index": 0,
                    "end_index": len(answer),
                    "title": title,
                },
            )
        )
    return annotations


def set_completed_response_annotations(
    response: dict[str, Any],
    annotations: list[Annotation],
) -> None:
    output = response.get("output")
    if not isinstance(output, list) or not output:
        raise RuntimeError("The Responses output message was not created")
    message = next(
        (
            item
            for item in reversed(output)
            if isinstance(item, dict) and item.get("type") == "message"
        ),
        None,
    )
    if not isinstance(message, dict):
        raise RuntimeError("The Responses output message is invalid")
    content = message.get("content")
    if not isinstance(content, list) or not content:
        raise RuntimeError("The Responses output content was not created")
    output_text = content[-1]
    if not isinstance(output_text, dict):
        raise RuntimeError("The Responses output text is invalid")
    output_text["annotations"] = annotations


if __name__ == "__main__":
    asyncio.run(app.run_async())

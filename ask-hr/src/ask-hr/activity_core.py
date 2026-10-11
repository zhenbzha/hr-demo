from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.ai.agentserver.core import AgentConfig
from azure.identity import (
    AzureCliCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)

from permission_contract import (
    build_search_body,
    build_search_headers,
    build_search_url,
    compact_search_evidence,
    validated_search_endpoint,
)
from region_classification import (
    RegionDecision,
    load_supported_regions,
    validate_region_decision,
)


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

Use only the authorized evidence supplied in this request. Local policy takes
precedence over Global baseline content for the same topic. Global content can
supplement topics not covered locally.

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
sources, links, references, or identifiers. Do not add citation markers.
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

generation_agent = Agent(
    client=generation_client,
    name="AskHRActivity",
    instructions=GENERATION_INSTRUCTIONS,
    default_options={
        "store": False,
        "reasoning": {"effort": "low"},
    },
)

classification_agent = Agent(
    client=classification_client,
    name="AskHRActivityRegionClassifier",
    instructions=CLASSIFICATION_INSTRUCTIONS,
    default_options={"store": False},
)


async def classify_regions(question: str) -> RegionDecision:
    result = await classification_agent.run(
        json.dumps(
            {
                "supported_regions": SUPPORTED_REGIONS,
                "previous_user_questions": [],
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
        response = await http_client.post(
            url,
            headers=headers,
            json=build_search_body(question, region),
        )
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


async def answer_question(
    question: str,
    user_search_token: str,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    decision = await classify_regions(question)
    if decision.status != "retrieve":
        return decision.message or "", []

    if on_progress:
        await on_progress("Searching the relevant HR policies...")
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
        return NO_ACCESSIBLE_EVIDENCE, []

    if on_progress:
        await on_progress("Preparing the answer...")
    result = await generation_agent.run(
        json.dumps(
            {
                "previous_user_questions": [],
                "question": question,
                "local_regions": decision.regions,
                "precedence": "local_then_global",
                "authorized_evidence": compact_evidence,
            },
            ensure_ascii=False,
        )
    )
    answer = result.text.strip()
    if not answer:
        raise RuntimeError("The generation model returned an empty answer")
    return answer, build_citations(references)[:4]


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
        citations.append(
            {
                "label": title,
                "location": pages,
                "url": (
                    f"{base_url}/api/references/{quote(document_id, safe='')}/"
                    f"{quote(version, safe='')}/pdf"
                ),
            }
        )
    return citations

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import httpx
from agent_framework import Agent, AgentResponseUpdate, ResponseStream
from agent_framework.foundry import FoundryChatClient
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
    validated_search_endpoint,
)
from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_core import from_json
from region_classification import (
    RegionDecision,
    load_supported_regions,
    validate_region_decision,
)

load_dotenv()

SEARCH_SCOPE = "https://search.azure.com/.default"
NO_ACCESSIBLE_EVIDENCE = (
    "The available HR policy information does not cover this request. "
    "Please check with your HR team for confirmation. "
    "Missing information does not mean that a request or expense is approved or prohibited."
)
MAX_QUESTION_LENGTH = 4_000
GENERATION_REASONING_EFFORT = "high"


class PolicyAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str = Field(
        description=(
            "The complete employee-facing answer in the user's language. "
            "Cover the question's applicable policy benefits and their conditions. "
            "Do not include source IDs or inline citation markers."
        )
    )
    source_ids: list[str] = Field(
        description=(
            "Exact source_id values from authorized_evidence supporting the answer. "
            "Include all sources needed to support its factual claims, and only "
            "those sources. Exclude unused or merely retrieved passages. "
            "Use an empty list when no policy answer is supported."
        )
    )


SUPPORTED_REGIONS = load_supported_regions(
    os.environ["ASK_HR_SUPPORTED_REGIONS_JSON"]
)
GLOBAL_REGION = os.environ.get("ASK_HR_GLOBAL_REGION_KEY", "global")

GENERATION_INSTRUCTIONS = f"""
You are Ask HR, an internal HR policy assistant.

Use only the authorized evidence supplied in this request. Previous user
questions can clarify the current question, but previous assistant answers are
never policy evidence. Local policy takes precedence over Global baseline
content for the same topic. Global content can supplement topics not covered
in the supplied local evidence.

Preserve source meaning when translating or summarizing. Answer the employee's
question directly, retaining relevant policy facts, eligibility conditions,
limits, exceptions, and uncertainty. Preserve legal or contractual precedence
rules when they materially affect the requested entitlement or process.

For routine policy questions, omit boilerplate about the documents being
fictional, demonstration/testing material, or not legal advice, and omit blanket
legal/contractual precedence disclaimers. State company-specific entitlements
as company policy within the answer, not as universal rights for everyone in a
country; a short phrase such as "Our Hong Kong policy provides..." is enough.
Do not add a separate company, scope, or effective-date preamble unless needed
to answer the question. If the user asks
about document status, authority, scope, dates, or precedence, answer truthfully
from the evidence and preserve the full scope of the applicable qualification.

Before finalizing, silently check coverage in both directions: no relevant
requested fact or applicable qualification is omitted, and every answer claim,
including every table cell, matches its own source scope, units, conditions,
and exceptions. For every quantitative limit, establish
the source comparison relation, translate it using unmistakably inclusive or
exclusive wording, and verify that the original and translated statements have
the same truth value at the boundary itself. Do not use colloquial approximations
or transfer qualifiers or counting units between policies. If equivalence of a
counting term in the answer language is not established, retain the original
term rather than assume a conversion. Do not omit relevant facts to avoid this
verification. Output only the employee answer, not the internal check.

Treat each evidence scope exactly as labeled. Never describe Global evidence
as part of a local policy. When the supplied local evidence does not cover a
topic, say that the local position is unknown from that evidence before
presenting an explicitly labeled Global baseline; do not imply that no local
policy exists, that all local policies have been checked, or that Global
entitlements are confirmed local terms. Do not infer eligibility categories,
family relationships, benefit coverage, or legal meaning that the evidence
does not explicitly state.

Write for an employee. Never mention indexes, retrieval, vectors, chunks,
corpora, grounding, tool calls, or returned evidence. Say "the supplied policy
information does not establish this detail" when a requested detail is absent. If no accessible evidence is
returned, respond exactly:
{NO_ACCESSIBLE_EVIDENCE}

Answer in the user's language. Do not invent policy, entitlement, deadlines,
sources, links, references, or identifiers. Do not add citation markers;
protocol adapters attach citations separately.
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

Determine the message language from current_question, not from the region names
or previous_user_questions. An English current_question requires an English
message. For other languages, keep the message in the current question's language.
""".strip()


def create_credential() -> AzureCliCredential | ManagedIdentityCredential:
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

generation_agent = Agent(
    client=generation_client,
    name="AskHR",
    instructions=GENERATION_INSTRUCTIONS,
    default_options={
        "store": False,
        "reasoning": {"effort": GENERATION_REASONING_EFFORT},
    },
)

classification_agent = Agent(
    client=classification_client,
    name="AskHRRegionClassifier",
    instructions=CLASSIFICATION_INSTRUCTIONS,
    default_options={"store": False},
)


async def classify_regions(
    question: str,
    previous_questions: list[str] | None = None,
) -> RegionDecision:
    result = await classification_agent.run(
        json.dumps(
            {
                "supported_regions": SUPPORTED_REGIONS,
                "previous_user_questions": previous_questions or [],
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
    user_search_token: str | None,
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


def extract_references(
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        reference
        for item in evidence
        for reference in item.get("result", {}).get("references", [])
        if isinstance(reference, dict)
    ]


def prepare_evidence(
    evidence: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    compact_evidence = compact_search_evidence(evidence)
    supplied_ids = {
        reference["source_id"]
        for item in compact_evidence
        for reference in item["references"]
    }
    references = [
        reference
        for reference in unique_references(extract_references(evidence))
        if reference["sourceData"]["chunk_id"] in supplied_ids
    ]
    return references, compact_evidence


def build_generation_input(
    *,
    question: str,
    previous_questions: list[str],
    local_regions: list[str],
    compact_evidence: list[dict[str, Any]],
) -> str:
    return json.dumps(
        {
            "previous_user_questions": previous_questions,
            "question": question,
            "local_regions": local_regions,
            "precedence": "local_then_global",
            "authorized_evidence": compact_evidence,
        },
        ensure_ascii=False,
    )


def build_answer_response_format(
    references: list[dict[str, Any]],
) -> type[PolicyAnswer]:
    identifiers = [
        reference["sourceData"]["chunk_id"]
        for reference in unique_references(references)
    ]
    if not identifiers:
        raise ValueError("Answer generation requires at least one policy source")
    source_id_type = StrEnum(
        "PolicySourceId",
        {f"SOURCE_{index}": identifier for index, identifier in enumerate(identifiers)},
    )
    return create_model(
        "EvidencePolicyAnswer",
        __base__=PolicyAnswer,
        source_ids=(
            list[source_id_type],
            Field(description=PolicyAnswer.model_fields["source_ids"].description),
        ),
    )


def select_answer_sources(
    value: object, references: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(value, PolicyAnswer):
        raise RuntimeError("The generation model returned invalid structured output")
    answer = value.answer.strip()
    if not answer:
        raise RuntimeError("The generation model returned an empty answer")
    if answer == NO_ACCESSIBLE_EVIDENCE:
        if value.source_ids:
            raise RuntimeError("An unsupported answer must not claim policy sources")
        return answer, []
    if not value.source_ids:
        raise RuntimeError("The policy answer did not identify any supporting sources")
    available = {
        reference["sourceData"]["chunk_id"]: reference
        for reference in unique_references(references)
    }
    if any(identifier not in available for identifier in value.source_ids):
        raise RuntimeError(
            "The policy answer cited a source outside its supplied evidence"
        )
    return answer, [
        available[identifier] for identifier in dict.fromkeys(value.source_ids)
    ]


async def generate_answer(
    generation_input: str, references: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    result = await generation_agent.run(
        generation_input,
        options={"response_format": build_answer_response_format(references)},
    )
    return select_answer_sources(result.value, references)


class _AnswerTextDecoder:
    def __init__(self) -> None:
        self.buffer = ""
        self.text = ""

    def append(self, delta: str) -> str:
        self.buffer += delta
        if not self.buffer.strip():
            return ""
        parsed = from_json(
            self.buffer, allow_partial="trailing-strings", allow_inf_nan=False
        )
        if not isinstance(parsed, dict):
            raise RuntimeError("The streamed policy answer must be a JSON object")
        current = parsed.get("answer", "")
        if not isinstance(current, str) or not current.startswith(self.text):
            raise RuntimeError("The streamed answer is not a consistent text value")
        addition = current[len(self.text) :]
        self.text = current
        return addition


def stream_answer(
    generation_input: str, references: list[dict[str, Any]]
) -> ResponseStream[str, tuple[str, list[dict[str, Any]]]]:
    response_stream = generation_agent.run(
        generation_input,
        stream=True,
        options={"response_format": build_answer_response_format(references)},
    )
    decoder = _AnswerTextDecoder()

    def decode(update: AgentResponseUpdate) -> list[str]:
        delta = decoder.append(update.text) if update.text else ""
        return [delta] if delta else []

    async def finalize(
        _updates: Sequence[str],
    ) -> tuple[str, list[dict[str, Any]]]:
        response = await response_stream.get_final_response()
        answer, selected = select_answer_sources(response.value, references)
        if answer != decoder.text.strip():
            raise RuntimeError("The completed answer differs from its streamed text")
        return answer, selected

    return response_stream.flat_map(decode, finalize)


async def answer_question(
    question: str,
    user_search_token: str,
    previous_questions: list[str] | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    history = previous_questions or []
    decision = await classify_regions(question, history)
    if decision.status != "retrieve":
        return decision.message or "", []

    if on_progress:
        await on_progress("Searching the relevant HR policies...")
    evidence = await retrieve_authorized_evidence(
        question=question,
        regions=[*decision.regions, GLOBAL_REGION],
        user_search_token=user_search_token,
    )
    references, compact_evidence = prepare_evidence(evidence)
    if not any(item.get("references") for item in compact_evidence):
        return NO_ACCESSIBLE_EVIDENCE, []

    if on_progress:
        await on_progress("Preparing the answer...")
    answer, selected = await generate_answer(
        build_generation_input(
            question=question,
            previous_questions=history,
            local_regions=decision.regions,
            compact_evidence=compact_evidence,
        ),
        references,
    )
    return answer, build_citations(selected)


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

# Copyright (c) Microsoft. All rights reserved.

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated, Literal

from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.core import AgentConfig
from azure.identity import (
    AzureCliCredential as SyncAzureCliCredential,
    ManagedIdentityCredential as SyncManagedIdentityCredential,
)
from azure.identity.aio import (
    AzureCliCredential as AsyncAzureCliCredential,
    ManagedIdentityCredential as AsyncManagedIdentityCredential,
)
from azure.search.documents.knowledgebases.aio import (
    KnowledgeBaseRetrievalClient,
)
from azure.search.documents.knowledgebases.models import (
    KnowledgeBaseRetrievalRequest,
    KnowledgeRetrievalSemanticIntent,
    SearchIndexKnowledgeSourceParams,
)
from dotenv import load_dotenv
from pydantic import Field


LOCAL_REGIONS = ("india", "finland", "hongkong")

INSTRUCTIONS = """
You are Ask HR, an internal HR policy assistant.

Before retrieving documents, semantically classify the user's employment
region from the current question. The allowed local regions are India,
Finland, and Hong Kong. Understand translations, abbreviations, and typos.

Only call retrieve_hr_policies when the applicable local region is clear with
high confidence. If it is not clear, ask which employment region applies and
do not call the tool. If the user explicitly asks about an unavailable region,
state that no active policy for that region is available; never substitute a
different region.

For explicit comparisons, pass every named supported local region. The tool
automatically retrieves Global baseline evidence separately.

Use only evidence returned by the tool. Local policy takes precedence over
Global baseline content for the same policy topic. Global content can
supplement topics not covered locally. Clearly label local policy versus
Global baseline context.

Write for an employee, not for a developer. State the policy answer directly.
Never mention indexes, indexing, retrieval, search, vectors, chunks, corpora,
grounding, tool calls, or returned evidence. Say "the policy does not specify"
or "the available policy does not cover" when a requested detail is absent.

Answer in the user's language. Do not interleave citations with the answer.
After the answer, append one machine-readable citation line per unique source
page in exactly this format: "Source: <document title>, p. <page>." Use "pp."
for a page range. These lines are rendered separately by the application.
Never invent a policy, entitlement, deadline, source, link, or citation.
""".strip()


def sync_credential():
    if AgentConfig.from_env().is_hosted:
        return SyncManagedIdentityCredential(
            client_id=os.environ.get("FOUNDRY_AGENT_INSTANCE_CLIENT_ID")
        )
    return SyncAzureCliCredential()


def async_credential():
    if AgentConfig.from_env().is_hosted:
        return AsyncManagedIdentityCredential(
            client_id=os.environ.get("FOUNDRY_AGENT_INSTANCE_CLIENT_ID")
        )
    return AsyncAzureCliCredential()


@tool(
    name="retrieve_hr_policies",
    description=(
        "Retrieve original HR policy evidence after the user's local region "
        "has been identified with high confidence. Global evidence is added "
        "automatically. Do not call this tool for an ambiguous region."
    ),
    approval_mode="never_require",
)
async def retrieve_hr_policies(
    query: Annotated[
        str,
        Field(description="The user's complete HR policy question."),
    ],
    regions: Annotated[
        list[Literal["india", "finland", "hongkong"]],
        Field(
            description=(
                "One or more high-confidence local regions explicitly or "
                "semantically identified from the user's question."
            )
        ),
    ],
) -> str:
    """Retrieve local and Global HR policy evidence from Foundry IQ."""
    normalized_regions = list(dict.fromkeys(regions))
    if not normalized_regions:
        return json.dumps(
            {
                "retrievalPerformed": False,
                "clarificationRequired": True,
                "message": "Ask which employment region applies.",
            }
        )

    invalid_regions = [
        region for region in normalized_regions if region not in LOCAL_REGIONS
    ]
    if invalid_regions:
        raise ValueError(f"Unsupported regions: {invalid_regions}")

    credential = async_credential()
    client = KnowledgeBaseRetrievalClient(
        endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
        knowledge_base_name=os.environ[
            "AZURE_SEARCH_KNOWLEDGE_BASE_NAME"
        ],
        credential=credential,
    )
    try:
        async def retrieve_region(region: str) -> dict:
            request = KnowledgeBaseRetrievalRequest(
                intents=[KnowledgeRetrievalSemanticIntent(search=query)],
                include_activity=True,
                knowledge_source_params=[
                    SearchIndexKnowledgeSourceParams(
                        knowledge_source_name=os.environ[
                            "AZURE_SEARCH_KNOWLEDGE_SOURCE_NAME"
                        ],
                        filter_add_on=f"region eq '{region}'",
                        include_references=True,
                        include_reference_source_data=True,
                    )
                ],
            )
            result = await client.retrieve(request)
            return {
                "scope": region,
                "result": result.as_dict(),
            }

        scopes = [*normalized_regions, "global"]
        evidence = await asyncio.gather(
            *(retrieve_region(region) for region in scopes)
        )
        return json.dumps(
            {
                "retrievalPerformed": True,
                "localRegions": normalized_regions,
                "precedence": "local_then_global",
                "evidence": evidence,
            },
            ensure_ascii=False,
        )
    finally:
        await client.close()
        await credential.close()


async def main() -> None:
    load_dotenv()
    credential = sync_credential()
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=credential,
    )
    agent = Agent(
        client=client,
        name="AskHR",
        instructions=INSTRUCTIONS,
        tools=[retrieve_hr_policies],
        default_options={"store": False},
    )
    server = ResponsesHostServer(agent)
    await server.run_async()


if __name__ == "__main__":
    asyncio.run(main())

# Copyright (c) Microsoft. All rights reserved.

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import ask_hr_pipeline
from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponseEventStream,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
)
from azure.ai.agentserver.responses.models import Annotation, UrlCitationBody
from permission_contract import require_security_context

app = ResponsesAgentServerHost(
    options=ResponsesServerOptions(default_fetch_history_count=12),
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
    if len(question) > ask_hr_pipeline.MAX_QUESTION_LENGTH:
        raise ValueError(
            f"Message must be {ask_hr_pipeline.MAX_QUESTION_LENGTH} "
            "characters or fewer"
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

    decision = await ask_hr_pipeline.classify_regions(
        question,
        previous_questions,
    )
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
        evidence = await ask_hr_pipeline.retrieve_authorized_evidence(
            question=question,
            regions=[*decision.regions, ask_hr_pipeline.GLOBAL_REGION],
            user_search_token=user_search_token,
        )
        references, compact_evidence = ask_hr_pipeline.prepare_evidence(
            evidence
        )
        if not any(item.get("references") for item in compact_evidence):
            answer = ask_hr_pipeline.NO_ACCESSIBLE_EVIDENCE
            citations = []
            annotations = []
            for event in stream.output_item_message(answer):
                yield event
        else:
            message = stream.add_output_item_message()
            yield message.emit_added()
            text_content = message.add_text_content()
            yield text_content.emit_added()

            answer_stream = ask_hr_pipeline.stream_answer(
                ask_hr_pipeline.build_generation_input(
                    question=question,
                    previous_questions=previous_questions,
                    local_regions=decision.regions,
                    compact_evidence=compact_evidence,
                ),
                references,
            )
            async for delta in answer_stream:
                yield text_content.emit_delta(delta)

            answer, references = await answer_stream.get_final_response()
            citations = ask_hr_pipeline.build_citations(references)
            annotations = build_response_annotations(answer, citations)
            yield text_content.emit_text_done(answer)
            for annotation in annotations:
                yield text_content.emit_annotation_added(annotation)
            yield text_content.emit_done()
            yield message.emit_done()

    citation_output = (
        None
        if answer == ask_hr_pipeline.NO_ACCESSIBLE_EVIDENCE
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


def build_web_citation_output(
    references: list[dict[str, Any]],
) -> dict[str, Any] | None:
    unique = ask_hr_pipeline.unique_references(references)
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

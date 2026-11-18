from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from agent_framework import AgentResponse, AgentResponseUpdate, Content, ResponseStream
from azure.ai.agentserver.core import AgentServerHost
from microsoft_agents.activity import ResourceResponse
from pydantic import ValidationError
from region_classification import RegionDecision
from starlette.testclient import TestClient

state_directory = tempfile.TemporaryDirectory(prefix="ask-hr-tests-")
unittest.addModuleCleanup(state_directory.cleanup)
initialize_host = AgentServerHost.__init__
with (
    patch.dict(
        os.environ,
        {
            "FOUNDRY_PROJECT_ENDPOINT": (
                "https://example.services.ai.azure.com/api/projects/test"
            ),
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "test-generation",
            "AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME": "test-classification",
            "ASK_HR_SUPPORTED_REGIONS_JSON": '{"finland":"Finland"}',
            "AGENTSERVER_STATE_ROOT": state_directory.name,
            "FOUNDRY_HOSTING_ENVIRONMENT": "",
        },
    ),
    patch("agent_framework.Agent") as agent_factory,
    patch("agent_framework.foundry.FoundryChatClient"),
    patch.object(
        AgentServerHost,
        "__init__",
        autospec=True,
        side_effect=lambda self, **kwargs: initialize_host(
            self, configure_observability=None, **kwargs
        ),
    ),
):
    import ask_hr_pipeline as pipeline
    import main as responses_adapter


class CombinedHostTests(unittest.TestCase):
    def test_both_protocols_use_one_host_and_preserve_delegated_access(self) -> None:
        activity_app = responses_adapter.activity_app
        with (
            patch.object(
                pipeline,
                "classify_regions",
                new=AsyncMock(
                    return_value=RegionDecision(
                        status="clarify", message="Which country?"
                    )
                ),
            ) as classify,
            patch.object(
                pipeline,
                "answer_question",
                new=AsyncMock(return_value=("Authorized Activity answer", [])),
            ) as answer,
            patch.object(
                activity_app.auth,
                "get_token",
                new=AsyncMock(
                    return_value=SimpleNamespace(token="activity-search-token")
                ),
            ) as get_token,
            patch.object(
                activity_app.adapter,
                "send_activities",
                new=AsyncMock(return_value=[ResourceResponse(id="status-message")]),
            ),
            patch.object(
                activity_app.adapter,
                "update_activity",
                new=AsyncMock(return_value=ResourceResponse(id="status-message")),
            ) as update_activity,
            TestClient(responses_adapter.app) as client,
        ):
            for headers in (
                {"x-client-search-authorization": "responses-search-token"},
                {"x-agent-user-id": "trusted-user"},
            ):
                with self.subTest(headers=list(headers)):
                    rejected = client.post(
                        "/responses",
                        headers=headers,
                        json={"input": "Question", "store": False},
                    )
                    self.assertEqual(rejected.status_code, 500)
                    self.assertIn("error", rejected.json())
                    classify.assert_not_awaited()

            response = client.post(
                "/responses",
                headers={
                    "x-client-search-authorization": "responses-search-token",
                    # The local SDK header stands in for Foundry's trusted context.
                    "x-agent-user-id": "trusted-user",
                },
                json={"input": "Question", "stream": True, "store": True},
            )
            self.assertEqual(response.status_code, 200)
            events = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            self.assertEqual(events[-1]["type"], "response.completed")
            self.assertEqual(
                events[-1]["response"]["output"][0]["content"][0]["text"],
                "Which country?",
            )
            classify.assert_awaited_once_with("Question", [])

            response_id = events[-1]["response"]["id"]
            follow_up = client.post(
                "/responses",
                headers={
                    "x-client-search-authorization": "responses-search-token",
                    "x-agent-user-id": "trusted-user",
                },
                json={
                    "input": "Follow-up",
                    "previous_response_id": response_id,
                    "store": False,
                },
            )
            self.assertEqual(follow_up.status_code, 200)
            self.assertEqual(follow_up.json()["status"], "completed")
            self.assertEqual(classify.await_args.args, ("Follow-up", ["Question"]))

            activity = {
                "type": "message",
                "id": "turn-1",
                "text": "Question",
                "channelId": "emulator",
                "serviceUrl": "https://example.test",
                "from": {"id": "activity-user"},
                "recipient": {"id": "ask-hr"},
                "conversation": {"id": "activity-conversation"},
            }
            response = client.post("/activity/messages", json=activity)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(get_token.await_args.args[1], "SEARCH")
            self.assertEqual(
                answer.await_args.kwargs["user_search_token"],
                "activity-search-token",
            )
            self.assertEqual(answer.await_args.kwargs["previous_questions"], [])
            self.assertEqual(
                update_activity.await_args.args[1].text, "Authorized Activity answer"
            )

            activity.update(id="turn-2", text="Follow-up")
            response = client.post("/activity/messages", json=activity)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(
                answer.await_args.kwargs["previous_questions"], ["Question"]
            )

            get_token.return_value = None
            answer.reset_mock()
            activity.update(id="turn-3", text="No token")
            response = client.post("/activity/messages", json=activity)
            self.assertEqual(response.status_code, 202)
            answer.assert_not_awaited()
            self.assertIn(
                "couldn't verify your access",
                update_activity.await_args.args[1].text,
            )


def model_stream(
    payload: str,
    response_format: type[pipeline.PolicyAnswer] = pipeline.PolicyAnswer,
) -> ResponseStream[AgentResponseUpdate, AgentResponse]:
    async def updates():
        for character in payload:
            yield AgentResponseUpdate(
                role="assistant", contents=[Content.from_text(character)]
            )

    return ResponseStream(
        updates(),
        finalizer=lambda updates: AgentResponse.from_updates(
            updates, output_format_type=response_format
        ),
    )


def mock_streaming_generation(payload: str) -> Mock:
    return Mock(
        side_effect=lambda *args, **kwargs: model_stream(
            payload, kwargs["options"]["response_format"]
        )
    )


class GenerationConfigurationTests(unittest.TestCase):
    def test_generation_uses_high_reasoning_without_storing_responses(self) -> None:
        options = next(
            call.kwargs["default_options"]
            for call in agent_factory.call_args_list
            if call.kwargs.get("name") == "AskHR"
        )
        self.assertEqual(pipeline.GENERATION_REASONING_EFFORT, "high")
        self.assertEqual(options, {"store": False, "reasoning": {"effort": "high"}})


class PipelineEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.references = [
            {
                "sourceData": {
                    "chunk_id": f"chunk-{index}",
                    "document_id": f"policy-{index}",
                    "document_title": f"Policy {index}",
                    "version": "1.0",
                    "region": "finland",
                    "page_start": 1,
                    "page_end": 1,
                    "chunk_text": f"Policy content {index}",
                }
            }
            for index in range(12)
        ]
        self.classify = self.enterContext(
            patch.object(
                pipeline,
                "classify_regions",
                new=AsyncMock(
                    return_value=RegionDecision(status="retrieve", regions=["finland"])
                ),
            )
        )
        self.retrieve = self.enterContext(
            patch.object(
                pipeline,
                "retrieve_authorized_evidence",
                new=AsyncMock(
                    return_value=[
                        {
                            "scope": "finland",
                            "result": {"references": self.references},
                        }
                    ]
                ),
            )
        )
        self.generate = AsyncMock(
            return_value=SimpleNamespace(
                value=pipeline.PolicyAnswer(
                    answer="Answer",
                    source_ids=[f"chunk-{index}" for index in range(12)],
                )
            )
        )
        self.enterContext(
            patch.object(
                pipeline,
                "generation_agent",
                new=SimpleNamespace(run=self.generate),
            )
        )
        self.enterContext(
            patch.dict(os.environ, {"ASK_HR_WEB_BASE_URL": "https://example.test"})
        )

    async def test_all_returned_chunks_reach_generation_and_citations(self) -> None:
        answer, citations = await pipeline.answer_question(
            "Question", "delegated-token"
        )
        self.assertEqual(answer, "Answer")
        self.generate.assert_awaited_once()
        self.assertIsNotNone(self.generate.await_args)
        model_input = json.loads(self.generate.await_args.args[0])
        self.assertEqual(
            [
                reference["content"]
                for reference in model_input["authorized_evidence"][0]["references"]
            ],
            [f"Policy content {index}" for index in range(12)],
        )
        self.assertEqual(len(citations), 12)
        self.assertEqual(citations[-1]["label"], "Policy 11")
        response_format = self.generate.await_args.kwargs["options"]["response_format"]
        self.assertTrue(issubclass(response_format, pipeline.PolicyAnswer))
        schema = response_format.model_json_schema()
        self.assertEqual(
            schema["$defs"]["PolicySourceId"]["enum"],
            [f"chunk-{index}" for index in range(12)],
        )
        self.retrieve.assert_awaited_once_with(
            question="Question",
            regions=["finland", pipeline.GLOBAL_REGION],
            user_search_token="delegated-token",
        )

    async def test_unanswered_question_has_no_citations(self) -> None:
        self.generate.return_value = SimpleNamespace(
            value=pipeline.PolicyAnswer(
                answer=pipeline.NO_ACCESSIBLE_EVIDENCE, source_ids=[]
            )
        )
        self.assertEqual(
            await pipeline.answer_question("Question", "delegated-token"),
            (pipeline.NO_ACCESSIBLE_EVIDENCE, []),
        )

    async def test_generation_schema_rejects_unsupplied_source_ids(self) -> None:
        await pipeline.answer_question("Question", "delegated-token")
        response_format = self.generate.await_args.kwargs["options"]["response_format"]
        with self.assertRaises(ValidationError):
            response_format.model_validate_json(
                '{"answer":"Answer","source_ids":["not-authorized"]}'
            )

    async def test_sources_without_usable_content_are_not_available_to_generation(
        self,
    ) -> None:
        self.references[1]["sourceData"]["chunk_text"] = " "
        self.references[2]["sourceData"]["chunk_text"] = None
        self.generate.return_value.value.source_ids = ["chunk-0"]
        await pipeline.answer_question("Question", "delegated-token")
        model_input = json.loads(self.generate.await_args.args[0])
        supplied_ids = [
            reference["source_id"]
            for item in model_input["authorized_evidence"]
            for reference in item["references"]
        ]
        self.assertEqual(
            supplied_ids, ["chunk-0", *[f"chunk-{index}" for index in range(3, 12)]]
        )
        response_format = self.generate.await_args.kwargs["options"]["response_format"]
        self.assertEqual(
            response_format.model_json_schema()["$defs"]["PolicySourceId"]["enum"],
            supplied_ids,
        )
        self.generate.return_value.value.source_ids = ["chunk-1"]
        with self.assertRaisesRegex(RuntimeError, "outside its supplied evidence"):
            await pipeline.answer_question("Question", "delegated-token")

    async def test_empty_evidence_skips_generation(self) -> None:
        self.retrieve.return_value = []
        self.assertEqual(
            await pipeline.answer_question("Question", "delegated-token"),
            (
                "The available HR policy information does not cover this request. "
                "Please check with your HR team for confirmation. "
                "Missing information does not mean that a request or expense is approved or prohibited.",
                [],
            ),
        )
        self.generate.assert_not_awaited()

    async def test_only_empty_passages_skip_generation(self) -> None:
        for reference in self.references:
            reference["sourceData"]["chunk_text"] = ""
        self.assertEqual(
            await pipeline.answer_question("Question", "delegated-token"),
            (pipeline.NO_ACCESSIBLE_EVIDENCE, []),
        )
        self.generate.assert_not_awaited()

    async def test_responses_empty_evidence_gives_hr_follow_up_without_sources(
        self,
    ) -> None:
        self.retrieve.return_value = []
        stream = MagicMock()
        stream.response = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": pipeline.NO_ACCESSIBLE_EVIDENCE,
                        }
                    ],
                }
            ]
        }
        context = SimpleNamespace(
            get_input_text=AsyncMock(return_value="Question"),
            get_history=AsyncMock(return_value=[]),
            client_headers={"x-client-search-authorization": "delegated-token"},
            platform_context=SimpleNamespace(user_id_key="trusted-user"),
            response_id="test-response",
        )
        with patch.object(
            responses_adapter, "ResponseEventStream", return_value=stream
        ):
            async for _ in responses_adapter.handle_response(
                Mock(spec=responses_adapter.CreateResponse),
                context,
                asyncio.Event(),
            ):
                pass

        stream.output_item_message.assert_called_once_with(
            pipeline.NO_ACCESSIBLE_EVIDENCE
        )
        stream.output_item_function_call.assert_not_called()
        stream.output_item_function_call_output.assert_not_called()
        self.assertEqual(stream.response["output"][0]["content"][0]["annotations"], [])
        self.generate.assert_not_awaited()

    async def test_only_declared_supporting_sources_are_attached(self) -> None:
        self.generate.return_value.value.source_ids = ["chunk-1", "chunk-10"]
        answer, citations = await pipeline.answer_question(
            "Question", "delegated-token"
        )
        self.assertEqual(answer, "Answer")
        self.assertEqual(
            [item["label"] for item in citations], ["Policy 1", "Policy 10"]
        )
        model_input = json.loads(self.generate.await_args.args[0])
        self.assertEqual(
            [
                item["source_id"]
                for item in model_input["authorized_evidence"][0]["references"]
            ],
            [f"chunk-{index}" for index in range(12)],
        )

    async def test_unknown_sources_and_unsupported_source_claims_fail(self) -> None:
        for answer, source_ids, error in (
            ("Answer", ["not-authorized"], "outside its supplied evidence"),
            ("Answer", [], "supporting sources"),
            (pipeline.NO_ACCESSIBLE_EVIDENCE, ["chunk-1"], "must not claim"),
            (" ", ["chunk-1"], "empty answer"),
        ):
            with self.subTest(answer=answer, source_ids=source_ids):
                self.generate.return_value.value = pipeline.PolicyAnswer(
                    answer=answer, source_ids=source_ids
                )
                with self.assertRaisesRegex(RuntimeError, error):
                    await pipeline.answer_question("Question", "delegated-token")

    async def test_clarification_skips_search_and_generation(self) -> None:
        self.classify.return_value = RegionDecision(
            status="clarify", message="Which country?"
        )
        self.assertEqual(
            await pipeline.answer_question("Question", "delegated-token"),
            ("Which country?", []),
        )
        self.retrieve.assert_not_awaited()
        self.generate.assert_not_awaited()

    async def test_responses_annotations_keep_all_policy_citations(self) -> None:
        await self.check_response_sources([f"chunk-{index}" for index in range(12)])

    async def test_responses_annotations_and_web_sources_exclude_unused_passages(
        self,
    ) -> None:
        await self.check_response_sources(["chunk-1", "chunk-10"])

    async def check_response_sources(self, source_ids: list[str]) -> None:
        payload = pipeline.PolicyAnswer(
            answer="Answer", source_ids=source_ids
        ).model_dump_json()
        stream = MagicMock()
        stream.response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Answer"}],
                }
            ]
        }
        context = SimpleNamespace(
            get_input_text=AsyncMock(return_value="Question"),
            get_history=AsyncMock(return_value=[]),
            client_headers={"x-client-search-authorization": "delegated-token"},
            platform_context=SimpleNamespace(user_id_key="trusted-user"),
            response_id="test-response",
        )
        with (
            patch.object(
                pipeline,
                "generation_agent",
                new=SimpleNamespace(run=mock_streaming_generation(payload)),
            ),
            patch.object(responses_adapter, "ResponseEventStream", return_value=stream),
        ):
            async for _ in responses_adapter.handle_response(
                Mock(spec=responses_adapter.CreateResponse),
                context,
                asyncio.Event(),
            ):
                pass

        text_content = (
            stream.add_output_item_message.return_value.add_text_content.return_value
        )
        self.assertEqual(text_content.emit_annotation_added.call_count, len(source_ids))
        self.assertEqual(
            len(stream.response["output"][0]["content"][0]["annotations"]),
            len(source_ids),
        )
        self.assertEqual(
            "".join(call.args[0] for call in text_content.emit_delta.call_args_list),
            "Answer",
        )
        web_output = json.loads(
            stream.output_item_function_call_output.call_args.args[1]
        )
        self.assertEqual(
            [
                reference["sourceData"]["chunk_id"]
                for reference in web_output["evidence"][0]["result"]["references"]
            ],
            source_ids,
        )

    async def test_streaming_uses_the_same_constrained_schema_as_nonstreaming(
        self,
    ) -> None:
        await pipeline.answer_question("Question", "delegated-token")
        expected = self.generate.await_args.kwargs["options"]["response_format"]
        payload = '{"answer":"Answer","source_ids":["chunk-1"]}'
        generate = mock_streaming_generation(payload)
        with patch.object(
            pipeline, "generation_agent", new=SimpleNamespace(run=generate)
        ):
            stream = pipeline.stream_answer("Question", self.references)
            self.assertEqual("".join([delta async for delta in stream]), "Answer")
            answer, selected = await stream.get_final_response()
        self.assertEqual(answer, "Answer")
        self.assertEqual(selected, [self.references[1]])
        self.assertTrue(generate.call_args.kwargs["stream"])
        actual = generate.call_args.kwargs["options"]["response_format"]
        self.assertEqual(actual.model_json_schema(), expected.model_json_schema())

    async def test_streaming_rejects_unsupplied_source_ids_during_parsing(self) -> None:
        payload = '{"answer":"Answer","source_ids":["not-authorized"]}'
        generate = mock_streaming_generation(payload)
        with patch.object(
            pipeline, "generation_agent", new=SimpleNamespace(run=generate)
        ):
            stream = pipeline.stream_answer("Question", self.references)
            with self.assertRaises(ValidationError):
                await stream.get_final_response()

    async def test_incomplete_stream_cannot_be_accepted_as_a_complete_answer(
        self,
    ) -> None:
        payload = '{"answer":"Partial answer","source_ids":["chunk-1"]'
        with patch.object(
            pipeline,
            "generation_agent",
            new=SimpleNamespace(run=mock_streaming_generation(payload)),
        ):
            response_stream = pipeline.stream_answer("Question", self.references)
            with self.assertRaises(ValueError):
                await response_stream.get_final_response()


class AnswerResponseFormatTests(unittest.TestCase):
    def test_schema_and_json_parsing_preserve_the_exact_source_set(self) -> None:
        for identifiers in (["one"], ["one", "two", "three"]):
            with self.subTest(identifiers=identifiers):
                references = [
                    {"sourceData": {"chunk_id": identifier}}
                    for identifier in identifiers
                ]
                response_format = pipeline.build_answer_response_format(references)
                schema = response_format.model_json_schema()
                self.assertEqual(list(schema["properties"]), ["answer", "source_ids"])
                self.assertEqual(schema["required"], ["answer", "source_ids"])
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(schema["$defs"]["PolicySourceId"]["enum"], identifiers)
                self.assertEqual(
                    schema["properties"]["source_ids"]["items"],
                    {"$ref": "#/$defs/PolicySourceId"},
                )
                self.assertEqual(
                    schema["properties"]["source_ids"]["description"],
                    pipeline.PolicyAnswer.model_fields["source_ids"].description,
                )
                for selection in (identifiers, [], [identifiers[0], identifiers[0]]):
                    payload = {"answer": "Answer", "source_ids": selection}
                    value = response_format.model_validate_json(json.dumps(payload))
                    self.assertIsInstance(value, pipeline.PolicyAnswer)
                    self.assertEqual(value.source_ids, selection)
                    self.assertEqual(json.loads(value.model_dump_json()), payload)
                for payload in (
                    {"answer": "Answer", "source_ids": ["other"]},
                    {"answer": "Answer", "source_ids": [1]},
                    {"answer": "Answer", "source_ids": identifiers, "extra": True},
                    {"answer": "Answer"},
                ):
                    with self.assertRaises(ValidationError):
                        response_format.model_validate_json(json.dumps(payload))

    def test_duplicates_do_not_expand_the_schema_or_citations(self) -> None:
        first = {"sourceData": {"chunk_id": "one"}, "rerankerScore": 1}
        better = {"sourceData": {"chunk_id": "one"}, "rerankerScore": 2}
        response_format = pipeline.build_answer_response_format([first, better])
        self.assertEqual(
            response_format.model_json_schema()["$defs"]["PolicySourceId"]["enum"],
            ["one"],
        )
        value = response_format.model_validate_json(
            '{"answer":"Answer","source_ids":["one","one"]}'
        )
        self.assertEqual(
            pipeline.select_answer_sources(value, [first, better]),
            ("Answer", [better]),
        )

    def test_request_schemas_do_not_leak_source_ids(self) -> None:
        formats = [
            pipeline.build_answer_response_format(
                [{"sourceData": {"chunk_id": identifier}}]
            )
            for identifier in ("one", "two")
        ]
        for response_format, allowed, rejected in (
            (formats[0], "one", "two"),
            (formats[1], "two", "one"),
        ):
            self.assertEqual(
                response_format.model_json_schema()["$defs"]["PolicySourceId"]["enum"],
                [allowed],
            )
            with self.assertRaises(ValidationError):
                response_format.model_validate_json(
                    json.dumps({"answer": "Answer", "source_ids": [rejected]})
                )
        self.assertNotIn("$defs", pipeline.PolicyAnswer.model_json_schema())

    def test_empty_source_set_cannot_produce_an_unconstrained_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one policy source"):
            pipeline.build_answer_response_format([])


class AnswerTextDecoderTests(unittest.TestCase):
    def test_every_split_preserves_multilingual_text_without_metadata(self) -> None:
        answer = (
            'Quoted "leave"\\path\n\u5e74\u5047: 2 arkip\u00e4iv\u00e4\u00e4 \U0001f600'
        )
        for escaped in (False, True):
            payload = json.dumps(
                {"answer": answer, "source_ids": ["must-not-appear"]},
                ensure_ascii=escaped,
            )
            for split in range(1, len(payload)):
                with self.subTest(escaped=escaped, split=split):
                    decoder = pipeline._AnswerTextDecoder()
                    text = decoder.append(payload[:split])
                    text += decoder.append(payload[split:])
                    self.assertEqual(text, answer)

    def test_text_streams_before_the_answer_string_and_sources_are_complete(
        self,
    ) -> None:
        decoder = pipeline._AnswerTextDecoder()
        self.assertEqual(decoder.append('{"answer":"Annual'), "Annual")
        self.assertEqual(decoder.append(' leave","source_ids":['), " leave")
        self.assertEqual(decoder.append('"private-id"]}'), "")

    def test_invalid_or_rewritten_text_is_not_silently_accepted(self) -> None:
        for payload in ('{"answer":42}', '{"answer":"first","answer":"different"}'):
            with self.subTest(payload=payload):
                decoder = pipeline._AnswerTextDecoder()
                if '"first"' in payload:
                    decoder.append('{"answer":"first",')
                    payload = '"answer":"different"}'
                with self.assertRaisesRegex(RuntimeError, "consistent text"):
                    decoder.append(payload)


if __name__ == "__main__":
    unittest.main()

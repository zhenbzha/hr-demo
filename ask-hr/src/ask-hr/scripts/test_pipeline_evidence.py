from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from agent_framework import AgentResponse, AgentResponseUpdate, Content, ResponseStream
from region_classification import RegionDecision

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
        },
    ),
    patch("agent_framework.Agent") as agent_factory,
    patch("agent_framework.foundry.FoundryChatClient"),
):
    import ask_hr_pipeline as pipeline

with patch("azure.ai.agentserver.responses.ResponsesAgentServerHost") as host:
    host.return_value.response_handler.side_effect = lambda handler: handler
    import main as responses_adapter

with patch.object(
    sys, "path", [str(Path(__file__).resolve().parents[4] / "scripts"), *sys.path]
):
    import validate_hosted_agent as hosted_validator


def model_stream(payload: str) -> ResponseStream[AgentResponseUpdate, AgentResponse]:
    async def updates():
        for character in payload:
            yield AgentResponseUpdate(
                role="assistant", contents=[Content.from_text(character)]
            )

    return ResponseStream(
        updates(),
        finalizer=lambda updates: AgentResponse.from_updates(
            updates, output_format_type=pipeline.PolicyAnswer
        ),
    )


class GenerationConfigurationTests(unittest.TestCase):
    def test_generation_uses_medium_reasoning_without_storing_responses(self) -> None:
        options = next(
            call.kwargs["default_options"]
            for call in agent_factory.call_args_list
            if call.kwargs.get("name") == "AskHR"
        )
        self.assertEqual(pipeline.GENERATION_REASONING_EFFORT, "medium")
        self.assertEqual(options, {"store": False, "reasoning": {"effort": "medium"}})


class HostedValidationContractTests(unittest.TestCase):
    def test_no_evidence_validator_accepts_the_pipeline_response(self) -> None:
        hosted_validator.no_evidence(
            hosted_validator.AgentResponse(pipeline.NO_ACCESSIBLE_EVIDENCE, [])
        )

    def test_no_evidence_validator_still_rejects_citations(self) -> None:
        with self.assertRaisesRegex(AssertionError, "unrelated policy"):
            hosted_validator.no_evidence(
                hosted_validator.AgentResponse(
                    pipeline.NO_ACCESSIBLE_EVIDENCE, [{"document_id": "unrelated"}]
                )
            )

    def test_no_evidence_validator_still_requires_a_policy_gap(self) -> None:
        with self.assertRaisesRegex(AssertionError, "policy gap"):
            hosted_validator.no_evidence(
                hosted_validator.AgentResponse("This expense is reimbursed.", [])
            )


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
        self.assertEqual(
            self.generate.await_args.kwargs["options"],
            {"response_format": pipeline.PolicyAnswer},
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

    async def test_empty_evidence_skips_generation(self) -> None:
        self.retrieve.return_value = []
        self.assertEqual(
            await pipeline.answer_question("Question", "delegated-token"),
            (
                "I can't confirm this from the available HR policy information. "
                "Please check with your HR team for confirmation. "
                "Missing information does not mean that a request or expense is approved or prohibited.",
                [],
            ),
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
        self.assertEqual(
            stream.response["output"][0]["content"][0]["annotations"], []
        )
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
                new=SimpleNamespace(run=Mock(return_value=model_stream(payload))),
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

    async def test_incomplete_stream_cannot_be_accepted_as_a_complete_answer(
        self,
    ) -> None:
        payload = '{"answer":"Partial answer","source_ids":["chunk-1"]'
        with patch.object(
            pipeline,
            "generation_agent",
            new=SimpleNamespace(run=Mock(return_value=model_stream(payload))),
        ):
            response_stream = pipeline.stream_answer("Question", self.references)
            with self.assertRaises(ValueError):
                await response_stream.get_final_response()


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

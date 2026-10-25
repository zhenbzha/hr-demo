"""Offline contracts for the combined performance and Foundry answer evaluation."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import benchmark_ask_hr_pipeline as benchmark


def source_reference(identifier="HR-TEST", text="14 paid working days"):
    return {
        "sourceData": {
            "chunk_id": identifier + "-chunk",
            "document_id": identifier,
            "document_title": "Policy",
            "region": "hongkong",
            "page_start": 2,
            "page_end": 2,
            "chunk_text": text,
        }
    }


def pipeline_fixture(*, status="retrieve", answer="14 paid working days"):
    references = [source_reference()]
    compact = [
        {
            "scope": "hongkong",
            "references": [{"content": references[0]["sourceData"]["chunk_text"]}],
        }
    ]
    return SimpleNamespace(
        GLOBAL_REGION="global",
        NO_ACCESSIBLE_EVIDENCE=(
            "I can't confirm this from the available HR policy information. "
            "Please check with your HR team for confirmation. "
            "Missing information does not mean that a request or expense is approved or prohibited."
        ),
        GENERATION_INSTRUCTIONS="Use the policy.",
        GENERATION_REASONING_EFFORT="low",
        PolicyAnswer=SimpleNamespace(
            model_json_schema=Mock(return_value={"type": "object"})
        ),
        CLASSIFICATION_INSTRUCTIONS="Classify the region.",
        classify_regions=AsyncMock(
            return_value=SimpleNamespace(
                status=status,
                regions=["hongkong"] if status == "retrieve" else [],
                message="Which employment region applies?",
            )
        ),
        retrieve_authorized_evidence=AsyncMock(return_value=[{"live": "evidence"}]),
        prepare_evidence=Mock(return_value=(references, compact)),
        unique_references=Mock(side_effect=lambda values: values),
        build_generation_input=Mock(
            side_effect=lambda **kwargs: json.dumps(
                {
                    "question": kwargs["question"],
                    "authorized_evidence": kwargs["compact_evidence"],
                }
            )
        ),
        generate_answer=AsyncMock(return_value=(answer.strip(), references)),
    )


def case_fixture(**overrides):
    return {
        "id": "test",
        "query": "Annual leave in Hong Kong?",
        "hardRequirement": True,
        "expectedDocumentIds": ["HR-TEST"],
        **overrides,
    }


class PipelineCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_clarification_proves_no_retrieval_or_generation(self):
        pipeline = pipeline_fixture(status="clarify")
        case = case_fixture(
            expectedDocumentIds=[],
            expectedRegionClassification={"regions": [], "status": "clarify"},
            expectedRetrieval=False,
            expectedCitationCount=0,
        )
        result = await benchmark.run_case(
            pipeline=pipeline, case=case, user_search_token="test-token"
        )
        self.assertEqual(result["checks"]["status"], "passed")
        self.assertEqual(result["diagnostics"]["retrieval_regions"], [])
        self.assertEqual(result["retrieval_evidence"], [])
        self.assertEqual(result["searchSeconds"], 0)
        self.assertEqual(result["generationSeconds"], 0)
        pipeline.retrieve_authorized_evidence.assert_not_awaited()
        pipeline.generate_answer.assert_not_awaited()

    async def test_scores_the_generated_answer_with_its_actual_compact_context(self):
        pipeline = pipeline_fixture()
        references, compact = pipeline.prepare_evidence.return_value
        references.append(
            source_reference("not-in-model-input", "Never shown to the model")
        )
        result = await benchmark.run_case(
            pipeline=pipeline,
            case=case_fixture(assertions=["State 14 paid working days."]),
            user_search_token="test-token",
        )
        self.assertEqual(result["response"], "14 paid working days")
        self.assertEqual(json.loads(result["context"]), compact)
        self.assertEqual(
            result["retrieval_evidence"],
            pipeline.retrieve_authorized_evidence.return_value,
        )
        self.assertNotIn("Never shown to the model", result["context"])
        self.assertIn("State 14 paid working days.", result["expected_behavior"])
        self.assertEqual(result["citations"][0]["chunk_text"], "14 paid working days")
        self.assertEqual(
            result["response_id_origin"], "local_execution_not_hosted_response"
        )
        pipeline.generate_answer.assert_awaited_once_with(
            result["generation_input"], references
        )
        self.assertEqual(
            pipeline.retrieve_authorized_evidence.call_args.kwargs["regions"],
            ["hongkong", "global"],
        )

    async def test_deterministic_failure_does_not_discard_answer_or_latency(self):
        result = await benchmark.run_case(
            pipeline=pipeline_fixture(),
            case=case_fixture(expectedDocumentIds=["missing"]),
            user_search_token="test-token",
        )
        self.assertEqual(result["checks"]["status"], "failed")
        self.assertTrue(result["response"])
        self.assertGreaterEqual(result["totalSeconds"], 0)

    async def test_empty_answer_is_a_runtime_failure(self):
        with self.assertRaisesRegex(RuntimeError, "empty answer"):
            await benchmark.run_case(
                pipeline=pipeline_fixture(answer=" "),
                case=case_fixture(),
                user_search_token="test-token",
            )

    async def test_no_evidence_answer_matches_adapter_citation_suppression(self):
        pipeline = pipeline_fixture()
        pipeline.generate_answer.return_value = (pipeline.NO_ACCESSIBLE_EVIDENCE, [])
        result = await benchmark.run_case(
            pipeline=pipeline,
            case=case_fixture(expectedDocumentIds=[], expectedCitationCount=0),
            user_search_token="test-token",
        )
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["checks"]["status"], "passed")
        self.assertIn("14 paid working days", result["context"])

    async def test_collection_failure_retains_first_answer_and_failed_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(
                output=Path(temporary) / "result.json", request_timeout=30
            )
            summary = {"status": "incomplete", "results": [], "case_checks": []}
            pipeline = pipeline_fixture()
            first = await benchmark.run_case(
                pipeline=pipeline, case=case_fixture(), user_search_token="test-token"
            )
            token_provider = Mock(return_value="test-token")
            with (
                patch.object(
                    benchmark,
                    "run_case",
                    side_effect=[first, RuntimeError("model unavailable")],
                ),
                self.assertRaisesRegex(RuntimeError, "model unavailable"),
            ):
                await benchmark.collect_responses(
                    pipeline=pipeline,
                    args=args,
                    cases=[case_fixture(), case_fixture(id="second")],
                    token_provider=token_provider,
                    summary=summary,
                )
            saved = json.loads(args.output.read_text())
            self.assertEqual(saved["status"], "incomplete")
            self.assertEqual(saved["collecting_case"], "second")
            self.assertEqual(len(saved["results"]), 1)
            self.assertEqual(token_provider.call_count, 2)


class BenchmarkRunnerTests(unittest.TestCase):
    def test_separate_scorer_failure_preserves_remote_run_identifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(
                evaluation_python=Path(sys.executable),
                cases=root / "cases.json",
                output=root / "report.json",
                judge_model_deployment="judge",
                behavior_threshold=4,
                label="test",
                poll_interval=1,
                timeout=60,
                request_timeout=30,
            )
            summary = {"status": "incomplete", "results": []}
            benchmark.write_summary(args.output, summary)

            def failed_process(command, **kwargs):
                benchmark.write_summary(
                    args.output,
                    {
                        **summary,
                        "built_in": {
                            "evaluation_id": "eval-test",
                            "run_id": "run-test",
                        },
                    },
                )
                raise subprocess.CalledProcessError(1, command)

            with (
                patch.object(benchmark.subprocess, "run", side_effect=failed_process),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                benchmark.score_in_separate_runtime(
                    args, "https://example.test", summary
                )
            self.assertEqual(summary["built_in"]["run_id"], "run-test")

    def test_knowledge_binding_reads_the_real_index(self):
        with patch.object(
            benchmark,
            "request_json",
            side_effect=[
                (200, {}, {"knowledgeSources": [{"name": "candidate-ks"}]}),
                (
                    200,
                    {},
                    {
                        "kind": "searchIndex",
                        "searchIndexParameters": {"searchIndexName": "candidate-index"},
                    },
                ),
            ],
        ):
            binding = benchmark.knowledge_binding(
                "https://example.search.windows.net",
                "candidate-kb",
                "candidate-ks",
                Mock(),
            )
        self.assertEqual(binding["indexName"], "candidate-index")

    def test_wrong_knowledge_source_is_rejected_before_collecting_answers(self):
        with (
            patch.object(
                benchmark,
                "request_json",
                side_effect=[
                    (200, {}, {"knowledgeSources": [{"name": "serving-ks"}]}),
                    (
                        200,
                        {},
                        {
                            "kind": "searchIndex",
                            "searchIndexParameters": {
                                "searchIndexName": "candidate-index"
                            },
                        },
                    ),
                ],
            ),
            self.assertRaisesRegex(ValueError, "exactly the selected"),
        ):
            benchmark.knowledge_binding(
                "https://example.search.windows.net",
                "candidate-kb",
                "candidate-ks",
                Mock(),
            )

    def test_p95_uses_inclusive_interpolation_and_includes_failed_checks(self):
        results = [
            {
                **{
                    field: value
                    for field in (
                        "classifierSeconds",
                        "searchSeconds",
                        "generationSeconds",
                        "totalSeconds",
                    )
                },
                "checks": {"status": "failed" if value == 5 else "passed"},
            }
            for value in (1, 2, 3, 4, 5)
        ]
        summary = benchmark.latency_summary(results)
        self.assertEqual(summary["totalSeconds"]["samples"], 5)
        self.assertAlmostEqual(summary["totalSeconds"]["p95"], 4.8)

    def _run(self, root, *, scoring_error=False):
        cases_path = root / "cases.json"
        cases_path.write_text(json.dumps({"cases": [case_fixture()]}))
        args = argparse.Namespace(
            cases=cases_path,
            output=root / "result.json",
            label="test",
            knowledge_base_name="candidate-kb",
            knowledge_source_name="candidate-ks",
            judge_model_deployment="judge",
            behavior_threshold=4,
            poll_interval=1,
            timeout=60,
            request_timeout=30,
            require_behavior_pass=False,
            evaluation_python=None,
        )
        captured = {}

        def score(**kwargs):
            captured["path"] = kwargs["dataset_path"]
            captured["row"] = json.loads(kwargs["dataset_path"].read_text())
            if scoring_error:
                raise RuntimeError("unscorable Foundry result")
            for stage, names in (
                (
                    "built_in",
                    ("groundedness", "relevance", "intent_resolution", "coherence"),
                ),
                ("expected_behavior", ("behavioral_adherence",)),
            ):
                kwargs["summary"][stage] = {
                    "cases": [
                        {
                            "case_id": "test",
                            "results": [
                                {"name": name, "score": 5, "passed": True}
                                for name in names
                            ],
                        }
                    ]
                }

        pipeline = pipeline_fixture()
        with (
            patch.dict(
                os.environ,
                {
                    "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/test",
                    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "generation",
                    "AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME": "classifier",
                    "AZURE_SEARCH_ENDPOINT": "https://example.search.windows.net",
                    "ASK_HR_WEB_BASE_URL": "https://example.test",
                },
            ),
            patch.dict(
                sys.modules,
                {
                    "ask_hr_pipeline": pipeline,
                    "permission_contract": SimpleNamespace(
                        validated_search_endpoint=lambda: (
                            "https://example.search.windows.net"
                        )
                    ),
                },
            ),
            patch.object(
                benchmark.importlib.metadata, "version", return_value="test-version"
            ),
            patch.object(benchmark, "AzureCliCredential"),
            patch.object(benchmark, "check_evaluation_sdk"),
            patch.object(
                benchmark,
                "get_bearer_token_provider",
                return_value=lambda: "test-token",
            ),
            patch.object(
                benchmark,
                "knowledge_binding",
                return_value={"indexName": "candidate-index"},
            ),
            patch.object(benchmark, "score_dataset", side_effect=score) as scorer,
        ):
            if scoring_error:
                with self.assertRaisesRegex(RuntimeError, "unscorable Foundry"):
                    asyncio.run(benchmark.main_async(args))
            else:
                asyncio.run(benchmark.main_async(args))
            scorer.assert_called_once()
        self.assertFalse(captured["path"].exists())
        self.assertEqual(captured["row"]["response"], "14 paid working days")
        return json.loads(args.output.read_text())

    def test_one_run_collects_latency_and_calls_shared_foundry_scoring(self):
        with tempfile.TemporaryDirectory() as temporary:
            summary = self._run(Path(temporary))
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["latency"]["totalSeconds"]["samples"], 1)
        self.assertEqual(summary["quality"]["behavioral_adherence"]["passed"], 1)
        self.assertEqual(summary["binding"]["indexName"], "candidate-index")
        self.assertEqual(summary["agent"]["generation_instructions"], "Use the policy.")
        self.assertEqual(
            summary["agent"]["classification_instructions"], "Classify the region."
        )

    def test_scoring_error_cannot_produce_a_completed_performance_only_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            summary = self._run(Path(temporary), scoring_error=True)
        self.assertEqual(summary["status"], "incomplete")
        self.assertEqual(summary["response_collection"], "completed")
        self.assertEqual(len(summary["results"]), 1)
        self.assertNotIn("quality", summary)


if __name__ == "__main__":
    unittest.main()

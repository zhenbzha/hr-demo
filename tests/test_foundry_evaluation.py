"""Offline collection and Foundry SDK contract tests for the manual evaluator."""

import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import run_foundry_evaluation as runner
from azure.core.exceptions import ResourceExistsError
from evaluation import agent, cases, foundry
from evaluation import runner as scoring
from openai.types import EvalCreateResponse
from openai.types.evals import RunCreateResponse
from openai.types.evals.run_create_response import ResultCounts
from openai.types.evals.runs import OutputItemListResponse
from openai.types.evals.runs.output_item_list_response import Result


def response_payload(*, output: list | None = None, **overrides) -> dict:
    return {
        "id": "response-test",
        "status": "completed",
        "error": None,
        "output": output
        if output is not None
        else [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Policy answer."}],
            }
        ],
        **overrides,
    }


def agent_result(**overrides) -> agent.AgentResult:
    return agent.AgentResult(
        **{
            "response": "Policy answer.",
            "references": [],
            "response_id": "response-test",
            "tool_calls": [],
            "diagnostics": None,
            **overrides,
        }
    )


def scored_item(case_id: str, names: tuple[str, ...], *, score=5, passed=True):
    return OutputItemListResponse.model_construct(
        datasource_item={"case_id": case_id},
        status="pass",
        results=[
            Result.model_construct(
                name=name,
                metric=name,
                score=score,
                passed=passed,
                reason="Supported by policy",
            )
            for name in names
        ],
    )


def completed_run(identifier: str = "run-test", **counts):
    return RunCreateResponse.model_construct(
        id=identifier,
        status="completed",
        error=None,
        report_url="https://example.test/report",
        result_counts=ResultCounts.model_construct(
            **{
                "total": 1,
                "passed": 1,
                "failed": 0,
                "errored": 0,
                **counts,
            }
        ),
    )


class ResponseCollectionTests(unittest.TestCase):
    def test_partial_response_is_not_success_even_with_answer_text(self):
        for status in ("incomplete", "failed", "in_progress"):
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(RuntimeError, "did not complete"),
            ):
                agent.parse_agent_response(response_payload(status=status))

    def test_malformed_tool_json_is_not_silently_discarded(self):
        payload = response_payload()
        payload["output"].append({"type": "function_call_output", "output": "{broken"})
        with self.assertRaises(json.JSONDecodeError):
            agent.parse_agent_response(payload)

    def test_structured_citations_include_the_actual_chunk_text(self):
        source = {
            "chunk_id": "one",
            "document_id": "HR-TEST",
            "document_title": "Policy",
            "region": "global",
            "page_start": 1,
            "page_end": 2,
            "chunk_text": "The source policy evidence.",
        }
        payload = response_payload()
        payload["output"].append(
            {
                "type": "function_call_output",
                "output": json.dumps(
                    {"evidence": [{"result": {"references": [{"sourceData": source}]}}]}
                ),
            }
        )
        result = agent.parse_agent_response(payload)
        self.assertEqual(result.citations[0]["chunk_text"], source["chunk_text"])
        self.assertEqual(result.context, source["chunk_text"])

    def test_missing_evidence_does_not_claim_no_retrieval_call(self):
        result = agent.parse_agent_response(response_payload())
        self.assertEqual(
            result.context, "No policy evidence was returned in this response."
        )
        self.assertIsNone(result.diagnostics)

    def test_endpoint_rejects_a_different_agent_or_version(self):
        endpoint = "https://example.services.ai.azure.com/api/projects/project/agents/ask-hr/versions/3/protocols/openai/responses"
        self.assertEqual(
            agent.endpoint_version_status(endpoint, "ask-hr", "3"), "versioned_endpoint"
        )
        for name, version in (("other", "3"), ("ask-hr", "4")):
            with (
                self.subTest(name=name, version=version),
                self.assertRaises(ValueError),
            ):
                agent.endpoint_version_status(endpoint, name, version)

    def test_unversioned_endpoint_label_is_not_claimed_as_verified(self):
        status = agent.endpoint_version_status(
            "https://example.services.ai.azure.com/api/projects/project/agents/ask-hr/endpoint/protocols/openai/responses",
            "ask-hr",
            "3",
        )
        self.assertIn("operator_supplied_label", status)

    def test_invocation_has_real_bearer_header_and_zero_retry_budget(self):
        with (
            patch.object(
                agent,
                "get_bearer_token_provider",
                side_effect=[lambda: "ai-test", lambda: "search-test"],
            ),
            patch.object(
                agent, "request_json", return_value=(200, {}, response_payload())
            ) as request,
        ):
            agent.invoke_agent(
                endpoint="https://example.test",
                agent_name="ask-hr",
                credential=Mock(),
                user_id="test-user",
                query="Policy?",
                timeout=30,
                max_retry_seconds=0,
            )
        self.assertEqual(
            request.call_args.kwargs["headers"]()["Authorization"],
            "Bearer " + "ai-test",
        )
        self.assertEqual(request.call_args.kwargs["max_retry_seconds"], 0)


class CaseValidationTests(unittest.TestCase):
    def test_unknown_hard_requirements_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown/unsupported"):
            cases._validate_case(
                {
                    "id": "test",
                    "query": "Policy?",
                    "hardRequirement": True,
                    "unknownConstraint": ["must not silently pass"],
                }
            )

    def test_missing_internal_trace_is_reported_as_unverified(self):
        checks = cases.validate_case_result(
            {
                "expectedRegionClassification": {"status": "clarify", "regions": []},
                "expectedRetrieval": False,
            },
            agent_result(),
        )
        self.assertEqual(checks.status, "unverified")
        self.assertEqual(len(checks.unverified), 2)

    def test_empty_evidence_does_not_prove_retrieval_was_skipped(self):
        result = agent_result(diagnostics={"retrieval_regions": ["finland", "global"]})
        checks = cases.validate_case_result({"expectedRetrieval": False}, result)
        self.assertEqual(checks.status, "failed")

    def test_classification_region_order_does_not_change_the_result(self):
        expected = {"status": "retrieve", "regions": ["finland", "hongkong"]}
        checks = cases.validate_case_result(
            {"expectedRegionClassification": expected},
            agent_result(
                diagnostics={
                    "classification": {
                        "status": "retrieve",
                        "regions": ["hongkong", "finland"],
                    }
                }
            ),
        )
        self.assertEqual(checks.status, "passed")

    def test_classification_still_rejects_wrong_status_or_region_sets(self):
        for actual in (
            {"status": "clarify", "regions": ["finland", "hongkong"]},
            {"status": "retrieve", "regions": ["finland"]},
            {"status": "retrieve", "regions": ["finland", "hongkong", "india"]},
        ):
            with self.subTest(actual=actual):
                checks = cases.validate_case_result(
                    {
                        "expectedRegionClassification": {
                            "status": "retrieve",
                            "regions": ["finland", "hongkong"],
                        }
                    },
                    agent_result(diagnostics={"classification": actual}),
                )
                self.assertEqual(checks.status, "failed")

    def test_invalid_classification_diagnostics_are_reported(self):
        for actual in (
            "retrieve",
            {},
            {"status": "retrieve", "regions": "finland"},
            {"status": "retrieve", "regions": [["finland"]]},
            {"status": [], "regions": ["finland"]},
        ):
            with self.subTest(actual=actual):
                checks = cases.validate_case_result(
                    {
                        "expectedRegionClassification": {
                            "status": "retrieve",
                            "regions": ["finland"],
                        }
                    },
                    agent_result(diagnostics={"classification": actual}),
                )
                self.assertEqual(checks.status, "failed")
                self.assertIn("Invalid classifier diagnostics", checks.failures)

    def test_rich_citation_requirement_rejects_empty_list(self):
        checks = cases.validate_case_result(
            {"requiredCitationFields": ["chunk_text"]}, agent_result()
        )
        self.assertEqual(checks.status, "failed")

    def test_invalid_citation_pages_are_rejected(self):
        for start, end in ((0, 1), (2, 1), (True, 2), (1, None)):
            with self.subTest(start=start, end=end):
                checks = cases.validate_case_result(
                    {},
                    agent_result(references=[{"page_start": start, "page_end": end}]),
                )
                self.assertEqual(checks.status, "failed")

    def test_multiple_failures_are_kept_for_manual_review(self):
        result = agent_result(references=[{"document_id": "wrong", "region": "india"}])
        checks = cases.validate_case_result(
            {
                "expectedDocumentIds": ["expected"],
                "forbiddenRegions": ["india"],
                "requiredCitationFields": ["chunk_text"],
            },
            result,
        )
        self.assertEqual(len(checks.failures), 3)

    def test_repository_dataset_remains_complete(self):
        loaded = cases.load_cases(
            Path(__file__).resolve().parent / "agent-quality-cases.json"
        )
        self.assertEqual(len(loaded), 17)
        self.assertEqual(len({case["id"] for case in loaded}), 17)


class FoundryResultTests(unittest.TestCase):
    def summary(self, items, **kwargs):
        return foundry.evaluation_summary(
            evaluation=EvalCreateResponse.model_construct(id="eval-test"),
            run=completed_run(),
            items=items,
            expected_case_ids={"test"},
            criterion_names={"behavioral_adherence"},
            **kwargs,
        )

    def test_null_numeric_score_cannot_be_a_pass(self):
        summary = self.summary(
            [scored_item("test", ("behavioral_adherence",), score=None)]
        )
        self.assertTrue(summary["validation_errors"])

    def test_nonfinite_and_boolean_quality_scores_are_rejected(self):
        for value in (float("nan"), float("inf"), True, 6, 0):
            with self.subTest(value=value):
                item = scored_item("test", ("behavioral_adherence",))
                item.results[0].score = value
                summary = self.summary([item])
                self.assertTrue(summary["validation_errors"])

    def test_nonfinite_scores_still_produce_a_writable_failure_report(self):
        summary = self.summary(
            [scored_item("test", ("behavioral_adherence",), score=float("nan"))]
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            runner.write_summary(path, summary)
            self.assertTrue(json.loads(path.read_text())["validation_errors"])

    def test_malformed_datasource_is_reported_as_unscorable(self):
        item = scored_item("test", ("behavioral_adherence",))
        item.datasource_item = None
        self.assertTrue(self.summary([item])["validation_errors"])

    def test_ordinal_behavior_score_must_be_an_integer(self):
        summary = self.summary(
            [scored_item("test", ("behavioral_adherence",), score=4.5)], threshold=4
        )
        self.assertTrue(summary["validation_errors"])

    def test_missing_or_duplicate_cases_are_rejected(self):
        item = scored_item("test", ("behavioral_adherence",))
        self.assertTrue(self.summary([])["validation_errors"])
        self.assertTrue(self.summary([item, item])["validation_errors"])

    def test_pass_flag_must_match_behavior_threshold(self):
        summary = self.summary(
            [scored_item("test", ("behavioral_adherence",), score=1)], threshold=4
        )
        self.assertTrue(summary["validation_errors"])

    def test_missing_or_duplicate_criteria_are_rejected(self):
        item = scored_item("test", ("behavioral_adherence", "behavioral_adherence"))
        self.assertTrue(self.summary([item])["validation_errors"])

    def test_valid_scored_result_is_accepted(self):
        summary = self.summary(
            [scored_item("test", ("behavioral_adherence",))], threshold=4
        )
        self.assertEqual(summary["validation_errors"], [])

    def test_timeout_requests_remote_run_cancellation(self):
        client = Mock()
        settings = foundry.EvaluationSettings("judge", 4, 1, 2)
        with (
            patch.object(foundry.time, "monotonic", side_effect=[0, 3]),
            self.assertRaisesRegex(TimeoutError, "cancellation requested"),
        ):
            foundry.wait_for_run(
                client,
                "eval-test",
                SimpleNamespace(id="run-test", status="running"),
                settings,
            )
        client.evals.runs.cancel.assert_called_once()


class ManualRunnerTests(unittest.TestCase):
    @staticmethod
    def arguments(root: Path) -> SimpleNamespace:
        cases_path = root / "cases.json"
        cases_path.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "id": "test",
                            "query": "Policy?",
                            "hardRequirement": True,
                        }
                    ]
                }
            )
        )
        return SimpleNamespace(
            cases=cases_path,
            output_dir=root / "output",
            retain_dataset=False,
            agent_name="ask-hr",
            agent_version="3",
            agent_endpoint="https://example.services.ai.azure.com/api/projects/project/agents/ask-hr/endpoint/protocols/openai/responses",
            project_endpoint="https://example.services.ai.azure.com/api/projects/project",
            user_id="test-user",
            request_timeout=30,
            max_retry_seconds=0,
            model_deployment="judge",
            behavior_threshold=4,
            poll_interval=1,
            timeout=60,
            dataset_name="manual-test",
            evaluation_name="Manual test",
            knowledge_base=None,
            require_behavior_pass=False,
            check_sdk=False,
            benchmark_report=None,
            replay_cases=None,
            prepare_replay=False,
        )

    def test_scoring_failure_preserves_the_report_and_cleans_local_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            with (
                patch.object(runner, "parse_args", return_value=args),
                patch.object(runner, "check_evaluation_sdk"),
                patch.object(runner, "invoke_agent", return_value=agent_result()),
                patch.object(runner, "AzureCliCredential"),
                patch.object(
                    runner, "score_dataset", side_effect=RuntimeError("unscorable")
                ) as score,
                self.assertRaisesRegex(RuntimeError, "unscorable"),
            ):
                runner.main()
            summary_file = next((args.output_dir / "results").glob("*.json"))
            summary = json.loads(summary_file.read_text())
            self.assertEqual(summary["status"], "incomplete")
            self.assertEqual(summary["case_checks"][0]["case_id"], "test")
            self.assertFalse(score.call_args.kwargs["dataset_path"].exists())

    def test_collection_failure_preserves_completed_checks_and_failed_case_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            args.cases.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "first",
                                "query": "Policy?",
                                "hardRequirement": True,
                            },
                            {
                                "id": "second",
                                "query": "Another policy?",
                                "hardRequirement": True,
                            },
                        ]
                    }
                )
            )
            with (
                patch.object(runner, "parse_args", return_value=args),
                patch.object(runner, "check_evaluation_sdk"),
                patch.object(
                    runner,
                    "invoke_agent",
                    side_effect=[agent_result(), RuntimeError("agent unavailable")],
                ),
                patch.object(runner, "AzureCliCredential"),
                self.assertRaisesRegex(RuntimeError, "agent unavailable"),
            ):
                runner.main()
            summary_file = next((args.output_dir / "results").glob("*.json"))
            summary = json.loads(summary_file.read_text())
            self.assertEqual(summary["status"], "incomplete")
            self.assertEqual(summary["collecting_case"], "second")
            self.assertEqual(summary["case_checks"][0]["case_id"], "first")

    def test_manual_report_does_not_enforce_a_pass_gate(self):
        summary = {"case_checks": [{"status": "unverified"}]}
        runner.enforce_optional_pass_requirement(
            SimpleNamespace(require_behavior_pass=False), summary
        )
        with self.assertRaisesRegex(RuntimeError, "not observable"):
            runner.enforce_optional_pass_requirement(
                SimpleNamespace(require_behavior_pass=True), summary
            )

    def test_manual_runner_collects_and_scores_with_mocked_sdk_then_removes_dataset(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.evals.create.side_effect = [
                SimpleNamespace(id="eval-builtin"),
                SimpleNamespace(id="eval-behavior"),
            ]
            client.evals.runs.create.side_effect = [
                completed_run("run-builtin"),
                completed_run("run-behavior"),
            ]
            client.evals.runs.output_items.list.side_effect = [
                [
                    scored_item(
                        "test",
                        ("relevance", "groundedness", "intent_resolution", "coherence"),
                    )
                ],
                [scored_item("test", ("behavioral_adherence",))],
            ]
            project = Mock()
            project.__enter__ = Mock(return_value=project)
            project.__exit__ = Mock(return_value=False)
            project.get_openai_client.return_value = client
            project.datasets.upload_file.return_value = SimpleNamespace(
                id="azureai://test-dataset"
            )
            project.evaluators.create_version.return_value = SimpleNamespace(
                name="behavior", version="1"
            )
            credential = Mock()
            credential.__enter__ = Mock(return_value=credential)
            credential.__exit__ = Mock(return_value=False)
            with (
                patch.object(runner, "parse_args", return_value=args),
                patch.object(runner, "check_evaluation_sdk"),
                patch.object(runner, "invoke_agent", return_value=agent_result()),
                patch.object(runner, "AzureCliCredential", return_value=credential),
                patch.object(scoring, "AIProjectClient", return_value=project),
            ):
                runner.main()
            summary_file = next((args.output_dir / "results").glob("*.json"))
            self.assertEqual(
                json.loads(summary_file.read_text())["status"], "completed"
            )
            uploaded_path = Path(
                project.datasets.upload_file.call_args.kwargs["file_path"]
            )
            self.assertFalse(uploaded_path.exists())
            self.assertEqual(client.evals.create.call_count, 2)
            self.assertEqual(
                project.evaluators.create_version.call_args.kwargs["headers"],
                {"Foundry-Features": "Evaluations=V1Preview"},
            )


class CapturedBenchmarkTests(unittest.TestCase):
    @staticmethod
    def benchmark_arguments(root: Path) -> SimpleNamespace:
        args = ManualRunnerTests.arguments(root)
        args.benchmark_report = root / "benchmark.json"
        case = cases.load_cases(args.cases)[0]
        runner.write_summary(
            args.benchmark_report,
            {
                "status": "incomplete",
                "response_collection": "completed",
                "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
                "rubric_sha256": hashlib.sha256(
                    foundry.BEHAVIOR_PROMPT.encode()
                ).hexdigest(),
                "judge_deployment": args.model_deployment,
                "behavior_threshold": args.behavior_threshold,
                "project_endpoint": args.project_endpoint,
                "case_checks": [{"case_id": "test", "status": "passed"}],
                "results": [cases.evaluation_row(case, agent_result())],
            },
        )
        return args

    def captured_summary(self):
        instructions = "Answer only from the supplied fictional policies."
        context = [{"scope": "finland", "references": [{"content": "Policy fact."}]}]
        row = cases.evaluation_row(
            {"id": "test", "query": "Policy?"},
            agent_result(),
            context=json.dumps(context),
        )
        row["generation_input"] = json.dumps(
            {"question": row["query"], "authorized_evidence": context}
        )
        return {
            "agent": {
                "generation_instructions": instructions,
                "generation_instructions_sha256": hashlib.sha256(
                    instructions.encode()
                ).hexdigest(),
            },
            "results": [row],
        }

    def test_captured_judge_receives_exact_instructions_and_model_input(self):
        summary = self.captured_summary()
        original = json.dumps(summary["results"], sort_keys=True)
        rows = scoring.captured_dataset_rows(summary)
        messages = rows[0]["agent_query"]
        self.assertEqual(
            messages,
            [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": summary["agent"]["generation_instructions"],
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": summary["results"][0]["generation_input"],
                        }
                    ],
                },
            ],
        )
        self.assertEqual(json.dumps(summary["results"], sort_keys=True), original)
        self.assertEqual(rows[0]["response"], summary["results"][0]["response"])

    def test_missing_generation_turn_is_not_invented_for_refusal(self):
        summary = self.captured_summary()
        summary["results"][0]["generation_input"] = None
        rows = scoring.captured_dataset_rows(summary)
        self.assertEqual(
            rows[0]["agent_query"],
            [{"role": "user", "content": [{"type": "text", "text": "Policy?"}]}],
        )

    def test_altered_capture_context_or_instructions_fail_before_scoring(self):
        for field in ("context", "query", "instructions", "missing_instructions"):
            with self.subTest(field=field):
                summary = self.captured_summary()
                if field == "instructions":
                    summary["agent"]["generation_instructions"] = "Altered rules."
                elif field == "missing_instructions":
                    del summary["agent"]["generation_instructions"]
                else:
                    summary["results"][0][field] = (
                        "[]" if field == "context" else "Different question?"
                    )
                with self.assertRaises(ValueError):
                    scoring.captured_dataset_rows(summary)

    def test_sdk_without_catalog_fails_before_any_upload(self):
        project = Mock()
        project.__enter__ = Mock(return_value=SimpleNamespace())
        project.__exit__ = Mock(return_value=False)
        with (
            patch.object(scoring, "AIProjectClient", return_value=project),
            patch.object(scoring, "AzureCliCredential"),
            self.assertRaisesRegex(RuntimeError, "lacks the evaluator catalog"),
        ):
            scoring.check_evaluation_sdk("https://example.test")

    def test_scoring_retry_reuses_the_already_uploaded_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_path = root / "responses.jsonl"
            dataset_path.write_text('{"case_id":"test"}\n', encoding="utf-8")
            dataset = SimpleNamespace(id="azureai://test-dataset")
            project = MagicMock()
            project.__enter__.return_value = project
            project.datasets.upload_file.side_effect = [
                dataset,
                ResourceExistsError("Dataset version already exists"),
            ]
            project.datasets.get.return_value = dataset
            project.evaluators.create_version.return_value = SimpleNamespace(
                name="behavior", version="1"
            )
            summary = {
                "dataset_version": "test-version",
                "agent": {"name": "ask-hr", "version": "test"},
            }
            arguments = {
                "project_endpoint": "https://example.test",
                "dataset_name": "answers",
                "evaluation_name": "quality",
                "settings": foundry.EvaluationSettings("judge", 4, 1, 60),
                "credential": Mock(),
                "dataset_path": dataset_path,
                "summary": summary,
                "summary_path": root / "summary.json",
                "case_ids": {"test"},
            }
            with (
                patch.object(scoring, "AIProjectClient", return_value=project),
                patch.object(
                    scoring,
                    "run_stage",
                    side_effect=[RuntimeError("scoring interrupted"), None, None],
                ) as stage,
            ):
                with self.assertRaisesRegex(RuntimeError, "scoring interrupted"):
                    scoring.score_dataset(**arguments)
                scoring.score_dataset(**arguments)
            self.assertEqual(summary["dataset_id"], dataset.id)
            project.datasets.upload_file.assert_called_once()
            project.datasets.get.assert_called_once_with(
                name="answers", version="test-version"
            )
            self.assertEqual(stage.call_count, 3)

    def test_agent_input_rescore_does_not_grade_the_legacy_registered_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            summary = self.captured_summary()
            summary.update(
                dataset_version="legacy-version", dataset_id="azureai://legacy-dataset"
            )
            rows = scoring.captured_dataset_rows(summary)
            dataset_path = root / "responses.jsonl"
            dataset_path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            project = MagicMock()
            project.__enter__.return_value = project
            project.datasets.get.return_value = SimpleNamespace(
                id=summary["dataset_id"]
            )
            project.evaluators.create_version.return_value = SimpleNamespace(
                name="behavior", version="1"
            )
            with (
                patch.object(scoring, "AIProjectClient", return_value=project),
                patch.object(scoring, "run_stage") as stage,
            ):
                scoring.score_dataset(
                    project_endpoint="https://example.test",
                    dataset_name="answers",
                    evaluation_name="quality",
                    settings=foundry.EvaluationSettings("judge", 4, 1, 60),
                    credential=Mock(),
                    dataset_path=dataset_path,
                    summary=summary,
                    summary_path=root / "summary.json",
                    case_ids={"test"},
                    agent_inputs=True,
                )
            project.datasets.upload_file.assert_not_called()
            project.datasets.get.assert_called_once_with(
                name="answers", version="legacy-version"
            )
            self.assertEqual(stage.call_count, 2)
            for call in stage.call_args_list:
                self.assertEqual(call.kwargs.get("inline_rows"), rows)
            self.assertEqual(
                summary["scoring_inputs"],
                {
                    "source": "inline_verified_captured_agent_query",
                    "sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                    "registered_dataset_usage": "capture_lineage_only",
                },
            )

    def test_captured_answers_are_scored_without_hosted_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.benchmark_arguments(root)
            summary = json.loads(args.benchmark_report.read_text())
            with (
                patch.object(runner, "AzureCliCredential"),
                patch.object(runner, "score_dataset") as score,
                patch.object(runner, "invoke_agent") as invoke,
            ):
                runner.score_benchmark_report(args)
            invoke.assert_not_called()
            score.assert_called_once()
            self.assertFalse(score.call_args.kwargs["dataset_path"].exists())
            self.assertTrue(
                json.loads(args.benchmark_report.read_text())["scoring_complete"]
            )
            summary["cases_sha256"] = "different-cases"
            runner.write_summary(args.benchmark_report, summary)
            with (
                patch.object(runner, "score_dataset") as score,
                self.assertRaisesRegex(ValueError, "configuration differ"),
            ):
                runner.score_benchmark_report(args)
            score.assert_not_called()

    def test_benchmark_rescoring_enforces_only_the_requested_pass_gate(self):
        for strict in (False, True):
            for checks_pass, behavior_pass in (
                (True, True),
                (False, True),
                (True, False),
            ):
                with (
                    self.subTest(
                        strict=strict, checks=checks_pass, behavior=behavior_pass
                    ),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    args = self.benchmark_arguments(Path(temporary))
                    args.require_behavior_pass = strict

                    def score(
                        *, checks_pass=checks_pass, behavior_pass=behavior_pass, **kwargs
                    ):
                        summary = kwargs["summary"]
                        summary["case_checks"][0]["status"] = (
                            "passed" if checks_pass else "unverified"
                        )
                        summary["expected_behavior"] = {
                            "cases": [
                                {
                                    "case_id": "test",
                                    "results": [
                                        {
                                            "score": 5 if behavior_pass else 1,
                                            "passed": behavior_pass,
                                        }
                                    ],
                                }
                            ]
                        }

                    with (
                        patch.object(runner, "AzureCliCredential"),
                        patch.object(runner, "score_dataset", side_effect=score),
                        patch.object(runner, "invoke_agent") as invoke,
                        (
                            self.assertRaises(RuntimeError)
                            if strict and not (checks_pass and behavior_pass)
                            else nullcontext()
                        ),
                    ):
                        runner.score_benchmark_report(args)
                    invoke.assert_not_called()
                    saved = json.loads(args.benchmark_report.read_text())
                    self.assertTrue(saved["scoring_complete"])
                    self.assertEqual(
                        saved["expected_behavior"]["cases"][0]["results"][0]["passed"],
                        behavior_pass,
                    )


if __name__ == "__main__":
    unittest.main()

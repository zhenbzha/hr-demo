"""Offline collection and Foundry SDK contract tests for the manual evaluator."""

import argparse
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
from evaluation.references import load_policy_references, policy_dataset_rows
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
                "role": "assistant",
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


def rubric_item(case_id="test", scores=(5, 5, 5), *, threshold=0.75):
    dimensions = [
        {
            "id": definition["id"], "weight": definition["weight"],
            "applicable": True, "score": score, "reason": "Observed requirement.",
        }
        for definition, score in zip(foundry.RUBRIC_DIMENSIONS, scores, strict=True)
    ]
    aggregate = sum(
        dimension["weight"] * (dimension["score"] - 1) / 4 for dimension in dimensions
    ) / sum(dimension["weight"] for dimension in dimensions)
    item = scored_item(
        case_id, ("hr_rubric",), score=aggregate,
        passed=aggregate >= threshold and min(scores) > 1,
    )
    item.results[0].properties = {"dimension_scores": dimensions}
    return item


def rubric_case(case_id="test", scores=(5, 5, 5)):
    result = rubric_item(case_id, scores).results[0]
    return {
        "case_id": case_id,
        "results": [{
            "name": "hr_rubric",
            "score": result.score,
            "passed": result.passed,
            "dimension_scores": result.properties["dimension_scores"],
        }],
    }


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


def policy_reference_fixture(root: Path, case_rows: list[dict]) -> Path:
    pdf = root / "policy.pdf"
    pdf.write_bytes(b"Policy answer.")
    path = root / "references.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "review": {
                    "status": "assistant_source_checked",
                    "basis": "Synthetic source fixture, not human approval.",
                },
                "sources": [
                    {
                        "document_id": "policy",
                        "path": pdf.name,
                        "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                    }
                ],
                "references": [
                    {
                        "case_id": case["id"],
                        "query": case["query"],
                        "ground_truth": "Policy answer.",
                        "passages": [
                            {"document_id": "policy", "page": 1, "quote": "Policy answer."}
                        ],
                    }
                    for case in case_rows
                    if case.get("expectedCitationCount") != 0
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


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
        self.assertEqual(json.loads(result.context), [source])
        self.assertEqual(result.output_items, payload["output"])

    def test_missing_evidence_does_not_claim_no_retrieval_call(self):
        result = agent.parse_agent_response(response_payload())
        self.assertEqual(
            result.context, "No policy evidence was returned in this response."
        )

    def test_collection_normalizes_real_tool_items_without_reading_diagnostics(self):
        payload = response_payload(
            metadata={"ask_hr_diagnostics": "not part of the evaluation contract"}
        )
        payload["output"].extend(
            [
                {
                    "type": "function_call",
                    "name": "ask_hr_citations",
                    "call_id": "c1",
                    "arguments": '{"citation_ids":["one"]}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": '{"evidence":[]}',
                },
            ]
        )
        result = agent.parse_agent_response(payload)
        row = cases.evaluation_row({"id": "test", "query": "Policy?"}, result)
        self.assertEqual(result.output_items, payload["output"])
        self.assertEqual(
            row["output_items"],
            [
                payload["output"][0],
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_call",
                            "tool_call_id": "c1",
                            "name": "ask_hr_citations",
                            "arguments": {"citation_ids": ["one"]},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "c1",
                    "content": [
                        {"type": "tool_result", "tool_result": '{"evidence":[]}'}
                    ],
                },
            ],
        )
        self.assertEqual(agent.captured_response_items(row), row["output_items"])
        self.assertEqual(row["response"], "Policy answer.")

    def test_unsupported_response_items_are_not_silently_discarded(self):
        with self.assertRaisesRegex(ValueError, "Unsupported response item"):
            agent.captured_response_items(
                {"output_items": [{"type": "unknown_item"}]}
            )

    def test_policy_context_keeps_local_and_global_source_boundaries(self):
        references = [
            {
                "document_id": "india",
                "document_title": "India policy",
                "region": "india",
                "chunk_text": "Local policy text.",
            },
            {
                "document_id": "global",
                "document_title": "Global handbook",
                "region": "global",
                "chunk_text": "Responsible AI rules.",
            },
        ]
        self.assertEqual(
            json.loads(agent_result(references=references).context), references
        )

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
        self.assertEqual(
            request.call_args.kwargs["headers"]()["x-client-search-authorization"],
            "search-test",
        )
        self.assertEqual(
            request.call_args.kwargs["headers"]()["x-ms-user-identity"],
            "test-user",
        )
        self.assertEqual(request.call_args.kwargs["max_retry_seconds"], 0)


class CaseValidationTests(unittest.TestCase):
    def test_unscored_assertions_require_historical_replay(self):
        for field in (
            "expectedBehavior", "assertions",
            "expectedPrimaryDocumentIds", "expectedSupportingDocumentIds",
        ):
            with self.subTest(field=field):
                case = {
                    "id": "test", "query": "Policy?", "hardRequirement": True,
                    field: ["Historical requirement."],
                }
                with self.assertRaisesRegex(ValueError, "unscored"):
                    cases._validate_case(case)
                cases._validate_case(case, historical=True)
                self.assertIn("Historical requirement.", cases.expected_behavior(case))

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

    def test_response_checks_do_not_require_internal_diagnostics(self):
        checks = cases.validate_case_result(
            {"expectedCitationCount": 0},
            agent.parse_agent_response(response_payload()),
        )
        self.assertEqual(checks.status, "passed")
        self.assertEqual(vars(checks), {"failures": []})

    def test_retired_execution_requirements_are_rejected_for_new_evaluations(self):
        for field in (
            "expectedRegionClassification",
            "expectedRetrieval",
            "expectedRetrievalRegions",
        ):
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "unsupported"),
            ):
                cases._validate_case(
                    {
                        "id": "test",
                        "query": "Policy?",
                        "hardRequirement": True,
                        field: [],
                    }
                )

    def test_allowed_regions_check_citations_not_internal_requests(self):
        for region, expected in (
            ("finland", "passed"),
            ("global", "passed"),
            ("india", "failed"),
        ):
            with self.subTest(region=region):
                checks = cases.validate_case_result(
                    {"allowedCitationRegions": ["finland", "global"]},
                    agent_result(references=[{"region": region}]),
                )
                self.assertEqual(checks.status, expected)

    def test_original_cases_can_still_be_loaded_for_historical_replay(self):
        path = Path(__file__).resolve().parent / "agent-quality-cases-legacy.json"
        loaded = cases.load_cases(path, historical=True)
        self.assertEqual(len(loaded), 17)
        self.assertIn("expectedRegionClassification", loaded[0])

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
        load_policy_references(
            Path(__file__).resolve().parent / "agent-quality-references.json", loaded
        )


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


class PolicyReferenceTests(unittest.TestCase):
    def setUp(self):
        self.cases = [
            {"id": "answer", "query": "Policy?", "hardRequirement": True},
            {
                "id": "clarify",
                "query": "Which entitlement?",
                "hardRequirement": True,
                "expectedCitationCount": 0,
            },
        ]

    def test_reference_scope_and_answer_preservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = policy_reference_fixture(Path(temporary), self.cases)
            references = load_policy_references(path, self.cases)
            rows = [
                cases.evaluation_row(case, agent_result()) for case in self.cases
            ]
            original = json.dumps(rows, sort_keys=True)
            prepared = policy_dataset_rows(rows, references)
            self.assertEqual(prepared[0]["ground_truth"], "Policy answer.")
            self.assertEqual(prepared[1]["ground_truth"], "")
            self.assertEqual(references.answers.keys(), {"answer"})
            self.assertEqual(json.dumps(rows, sort_keys=True), original)
            for before, after in zip(rows, prepared, strict=True):
                self.assertEqual(
                    before, {
                        key: value for key, value in after.items()
                        if key not in {"ground_truth", "policy_query"}
                    }
                )
                self.assertEqual(
                    json.loads(after["policy_query"]),
                    {"question": before["query"], "policy_context": before["context"]},
                )
            with self.assertRaisesRegex(ValueError, "uniquely cover"):
                policy_dataset_rows(rows[:1], references)
            rows[0]["query"] = "Changed question"
            with self.assertRaisesRegex(ValueError, "requirements changed"):
                policy_dataset_rows(rows, references)

    def test_missing_stale_or_repurposed_references_fail_before_scoring(self):
        for mutation in ("pdf", "question", "missing", "duplicate", "control"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = policy_reference_fixture(root, self.cases)
                manifest = json.loads(path.read_text())
                if mutation == "pdf":
                    (root / "policy.pdf").write_bytes(b"Changed policy.")
                elif mutation == "question":
                    manifest["references"][0]["query"] = "Different question"
                elif mutation == "missing":
                    manifest["references"] = []
                elif mutation == "duplicate":
                    manifest["references"].append(manifest["references"][0])
                else:
                    manifest["references"][0]["case_id"] = "clarify"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_policy_references(path, self.cases)

    def test_policy_completeness_uses_independent_reference_not_generic_grounding(self):
        criteria = {
            item["name"]: item for item in foundry.policy_answer_criteria("judge")
        }
        self.assertEqual(set(criteria), {"response_completeness"})
        self.assertEqual(
            criteria["response_completeness"]["data_mapping"],
            {"ground_truth": "{{item.ground_truth}}", "response": "{{item.response}}"},
        )
        self.assertNotIn(
            "agent_query", foundry.dataset_schema(policy_answers=True)["item_schema"]["required"]
        )

    def test_clarification_is_not_auto_passed_and_incomplete_policy_answers_fail(self):
        summary = {
            "evaluation_plan": {
                "behavior_case_ids": ["answer", "clarify"],
                "policy_answer_case_ids": ["answer"],
            },
            "case_checks": [
                {"case_id": case["id"], "status": "passed"} for case in self.cases
            ],
            "expected_behavior": {
                "cases": [
                    rubric_case(
                        case["id"], (5, 5, 5) if case["id"] == "answer" else (3, 3, 3)
                    )
                    for case in self.cases
                ]
            },
            "policy_answers": {
                "cases": [
                    {
                        "case_id": "answer",
                        "results": [
                            {"name": "response_completeness", "passed": False},
                        ],
                    }
                ]
            },
        }
        summary["rubric_assessment"] = foundry.rubric_assessment(
            summary["expected_behavior"]
        )
        assessment = scoring.policy_assessment(summary)
        self.assertEqual(assessment["failed"], 2)
        self.assertEqual(
            assessment["cases"][0]["failed_requirements"], ["response_completeness"]
        )
        self.assertEqual(
            assessment["cases"][1]["failed_requirements"], ["hr_rubric"]
        )
        summary["policy_answers"]["cases"] = []
        with self.assertRaisesRegex(ValueError, "missing or duplicate"):
            scoring.policy_assessment(summary)

    def test_policy_stage_scope_does_not_depend_on_response_content(self):
        self._score_policy_profile(captured=False)

    def test_replacement_retains_factual_completeness_and_citation_gates(self):
        for defect, expected_failures in (
            ("valid", []),
            ("unsupported_claim", ["hr_rubric.policy_scope_and_evidence.minimum_score"]),
            ("incomplete", ["response_completeness"]),
            ("citation", ["citation_checks"]),
        ):
            with self.subTest(defect=defect):
                summary = {
                    "evaluation_plan": {
                        "behavior_case_ids": ["answer"],
                        "policy_answer_case_ids": ["answer"],
                    },
                    "case_checks": [{
                        "case_id": "answer",
                        "status": "failed" if defect == "citation" else "passed",
                    }],
                    "rubric_assessment": foundry.rubric_assessment({
                        "cases": [rubric_case(
                            "answer", (5, 2, 5) if defect == "unsupported_claim" else (5, 5, 5)
                        )],
                    }),
                    "policy_answers": {"cases": [{
                        "case_id": "answer",
                        "results": [
                            {
                                "name": "response_completeness",
                                "passed": defect != "incomplete",
                            },
                        ],
                    }]},
                }
                original = json.dumps(summary, sort_keys=True)
                assessment = scoring.policy_assessment(summary)
                self.assertEqual(json.dumps(summary, sort_keys=True), original)
                self.assertEqual(assessment["cases"][0]["failed_requirements"], expected_failures)
                summary["assessment"] = assessment
                with nullcontext() if not expected_failures else self.assertRaises(RuntimeError):
                    scoring.require_demo_pass(summary)
                for invalid in ("missing", "duplicate", "unscorable", "retired_metric"):
                    with self.subTest(invalid=invalid):
                        broken = json.loads(original)
                        metrics = broken["policy_answers"]["cases"][0]["results"]
                        if invalid == "missing":
                            metrics.pop(0)
                        elif invalid == "duplicate":
                            metrics.append(metrics[0])
                        elif invalid == "retired_metric":
                            metrics.append({"name": "groundedness", "passed": False})
                        else:
                            metrics[0]["passed"] = None
                        with self.assertRaisesRegex(ValueError, "invalid policy metrics"):
                            scoring.policy_assessment(broken)

    def test_policy_profile_preserves_captured_evidence_in_rubric_inputs(self):
        self._score_policy_profile(captured=True)

    def _score_policy_profile(self, *, captured):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            references = load_policy_references(
                policy_reference_fixture(root, self.cases), self.cases
            )
            rows = policy_dataset_rows(
                [
                    cases.evaluation_row(case, agent_result(response="No policy available."))
                    for case in self.cases
                ],
                references,
            )
            if captured:
                for row in rows:
                    row["agent_query"] = [{
                        "role": "user",
                        "content": [{
                            "type": "text",
                            "text": "Recorded question and supplied policy evidence.",
                        }],
                    }]
            path = root / "responses.jsonl"
            runner._write_dataset_rows(path, rows)
            summary = {
                "dataset_version": "test-version",
                "agent": {"name": "ask-hr", "version": "test"},
                "case_checks": [
                    {"case_id": case["id"], "status": "passed"} for case in self.cases
                ],
            }
            project = MagicMock()
            project.__enter__.return_value = project
            project.datasets.upload_file.return_value = SimpleNamespace(id="dataset")
            project.evaluators.create_version.return_value = SimpleNamespace(
                name="behavior", version="1"
            )

            def record_stage(**kwargs):
                summary[kwargs["label"]] = {
                    "cases": [
                        {
                            "case_id": identifier,
                            "results": [
                                rubric_case(identifier, (3, 3, 3))["results"][0]
                                if criterion["name"] == "hr_rubric"
                                else {"name": criterion["name"], "passed": False}
                                for criterion in kwargs["criteria"]
                            ],
                        }
                        for identifier in sorted(kwargs["case_ids"])
                    ]
                }

            with (
                patch.object(scoring, "AIProjectClient", return_value=project),
                patch.object(scoring, "run_stage", side_effect=record_stage) as stage,
            ):
                scoring.score_dataset(
                    project_endpoint="https://example.test",
                    dataset_name="answers",
                    evaluation_name="quality",
                    settings=foundry.EvaluationSettings("judge", 0.75, 1, 60),
                    credential=Mock(),
                    dataset_path=path,
                    summary=summary,
                    summary_path=root / "report.json",
                    case_ids={"answer", "clarify"},
                    policy_case_ids=set(references.answers),
                )
            self.assertEqual(stage.call_args_list[0].kwargs["case_ids"], {"answer", "clarify"})
            self.assertEqual(stage.call_args_list[0].kwargs["inline_rows"], rows)
            self.assertEqual(stage.call_args_list[1].kwargs["case_ids"], {"answer"})
            self.assertEqual(
                summary["scoring_inputs"]["source"], "inline_pdf_reference_dataset"
            )
            query_field = "agent_query" if captured else "policy_query"
            self.assertEqual(summary["scoring_inputs"]["rubric_query_field"], query_field)
            rubric_stage = stage.call_args_list[0].kwargs
            self.assertEqual(
                rubric_stage["criteria"][0]["data_mapping"]["query"],
                "{{item." + query_field + "}}",
            )
            self.assertEqual(
                "agent_query" in rubric_stage["schema"]["item_schema"]["required"],
                captured,
            )
            self.assertEqual(
                [item["name"] for item in stage.call_args_list[1].kwargs["criteria"]],
                ["response_completeness"],
            )
            self.assertEqual(summary["assessment"]["failed"], 2)

    def test_missing_or_changed_policy_context_fails_before_scoring(self):
        for invalid in (None, '{"question":"Policy?","policy_context":"changed"}'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                references = load_policy_references(
                    policy_reference_fixture(root, self.cases), self.cases
                )
                rows = policy_dataset_rows(
                    [cases.evaluation_row(case, agent_result()) for case in self.cases],
                    references,
                )
                if invalid is None:
                    rows[0].pop("policy_query")
                else:
                    rows[0]["policy_query"] = invalid
                path = root / "responses.jsonl"
                runner._write_dataset_rows(path, rows)
                with (
                    patch.object(scoring, "AIProjectClient") as project,
                    self.assertRaisesRegex(ValueError, "captured question and context"),
                ):
                    scoring.score_dataset(
                        project_endpoint="https://example.test",
                        dataset_name="answers",
                        evaluation_name="quality",
                        settings=foundry.EvaluationSettings("judge", 0.75, 1, 60),
                        credential=Mock(),
                        dataset_path=path,
                        summary={},
                        summary_path=root / "report.json",
                        case_ids={"answer", "clarify"},
                        policy_case_ids={"answer"},
                    )
                project.assert_not_called()

    def test_partial_or_empty_captured_inputs_fail_before_policy_scoring(self):
        for invalid in ("missing", None, []):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                rows = [
                    {"case_id": "answer", "agent_query": [{"role": "user", "content": []}]},
                    {"case_id": "clarify"},
                ]
                if invalid != "missing":
                    rows[1]["agent_query"] = invalid
                path = root / "responses.jsonl"
                runner._write_dataset_rows(path, rows)
                with (
                    patch.object(scoring, "AIProjectClient") as project,
                    self.assertRaisesRegex(ValueError, "nonempty agent_query in every row"),
                ):
                    scoring.score_dataset(
                        project_endpoint="https://example.test",
                        dataset_name="answers",
                        evaluation_name="quality",
                        settings=foundry.EvaluationSettings("judge", 0.75, 1, 60),
                        credential=Mock(),
                        dataset_path=path,
                        summary={},
                        summary_path=root / "report.json",
                        case_ids={"answer", "clarify"},
                        policy_case_ids={"answer"},
                    )
                project.assert_not_called()


class NativeRubricTests(unittest.TestCase):
    def summary(self, item, threshold=0.75):
        return foundry.evaluation_summary(
            evaluation=EvalCreateResponse.model_construct(id="eval-native"),
            run=completed_run(
                passed=int(item.results[0].passed),
                failed=int(not item.results[0].passed),
            ),
            items=[item], expected_case_ids={"test"}, criterion_names={"hr_rubric"},
            threshold=threshold,
        )

    def test_native_registration_and_supported_recorded_input_mappings(self):
        project = Mock()
        project.evaluators.create_version.return_value = SimpleNamespace(
            name="native", version="2"
        )
        settings = foundry.EvaluationSettings("judge", 0.75, 1, 60)
        evaluator = foundry.create_rubric_evaluator(project, settings)
        requested = project.evaluators.create_version.call_args.kwargs
        definition = requested["evaluator_version"]["definition"]
        self.assertEqual(definition["type"], "rubric")
        self.assertNotIn("prompt_text", definition)
        self.assertNotIn("headers", requested)
        self.assertEqual(definition["pass_threshold"], 0.75)
        criterion = foundry.rubric_criterion(evaluator, settings)
        self.assertEqual(criterion["evaluator_version"], "2")
        self.assertEqual(
            criterion["initialization_parameters"], {"deployment_name": "judge"}
        )
        self.assertEqual(criterion["data_mapping"], {
            "query": "{{item.query}}", "response": "{{item.output_items}}",
            "tool_definitions": "{{item.tool_definitions}}",
        })
        captured_criterion = foundry.rubric_criterion(
            evaluator, settings, agent_inputs=True
        )
        self.assertEqual(
            captured_criterion["data_mapping"]["query"], "{{item.agent_query}}"
        )
        self.assertNotEqual(
            foundry.rubric_fingerprint(0.75), foundry.rubric_fingerprint(0.8)
        )

    def test_normalized_scores_and_dimension_explanations_are_preserved(self):
        for scores in ((1, 1, 1), (4, 4, 4), (5, 5, 5)):
            with self.subTest(scores=scores):
                summary = self.summary(rubric_item(scores=scores))
                self.assertEqual(summary["validation_errors"], [])
                result = summary["cases"][0]["results"][0]
                self.assertEqual(
                    [dimension["score"] for dimension in result["dimension_scores"]],
                    list(scores),
                )
                self.assertTrue(result["dimension_scores"][0]["reason"])

    def test_one_critical_dimension_fails_even_above_aggregate_threshold(self):
        item = rubric_item(scores=(5, 5, 1))
        self.assertGreater(item.results[0].score, 0.75)
        self.assertEqual(self.summary(item)["validation_errors"], [])
        item.results[0].passed = True
        self.assertTrue(self.summary(item)["validation_errors"])

    def test_application_floor_preserves_native_results_and_gates_both_profiles(self):
        for scores, passes in (
            ((4, 4, 4), True),
            ((5, 3, 3), True),
            ((5, 2, 5), False),
            ((5, 2, 4), False),
            ((5, 5, 2), False),
        ):
            with self.subTest(scores=scores):
                stage = self.summary(rubric_item(scores=scores))
                original = json.dumps(stage, sort_keys=True)
                self.assertEqual(stage["validation_errors"], [])
                self.assertTrue(stage["cases"][0]["results"][0]["passed"])
                assessment = foundry.rubric_assessment(stage)
                self.assertEqual(assessment["cases"][0]["passed"], passes)
                self.assertEqual(json.dumps(stage, sort_keys=True), original)
                for hosted in (False, True):
                    summary = {
                        "case_checks": [{"case_id": "test", "status": "passed"}],
                        "expected_behavior": stage,
                        "rubric_assessment": assessment,
                    }
                    if hosted:
                        summary["evaluation_plan"] = {
                            "behavior_case_ids": ["test"], "policy_answer_case_ids": [],
                        }
                        summary["assessment"] = scoring.policy_assessment(summary)
                        self.assertEqual(summary["assessment"]["passed"], int(passes))
                    with nullcontext() if passes else self.assertRaises(RuntimeError):
                        scoring.require_demo_pass(summary)

    def test_application_assessment_cannot_accept_missing_dimensions(self):
        stage = self.summary(rubric_item())
        del stage["cases"][0]["results"][0]["dimension_scores"]
        with self.assertRaisesRegex(ValueError, "missing rubric dimension"):
            foundry.rubric_assessment(stage)

    def test_incomplete_or_inconsistent_native_results_are_errors(self):
        for invalid in (
            "score", "nonfinite", "missing", "duplicate", "extra_residual",
            "weight", "skipped", "aggregate", "dimension",
        ):
            with self.subTest(invalid=invalid):
                item = rubric_item()
                dimensions = item.results[0].properties["dimension_scores"]
                if invalid == "score":
                    item.results[0].score = 4
                elif invalid == "nonfinite":
                    item.results[0].score = float("nan")
                elif invalid == "missing":
                    dimensions.pop()
                elif invalid == "duplicate":
                    dimensions[-1] = dict(dimensions[0])
                elif invalid == "extra_residual":
                    dimensions.append({**dimensions[-1], "id": "general_quality"})
                elif invalid == "weight":
                    dimensions[0]["weight"] += 1
                elif invalid == "skipped":
                    dimensions[0]["applicable"] = False
                elif invalid == "dimension":
                    dimensions[0]["score"] = float("nan")
                else:
                    item.results[0].score = 0.9
                self.assertTrue(self.summary(item)["validation_errors"])

    def test_new_and_historical_cli_thresholds_are_not_silently_interchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = ManualRunnerTests.arguments(Path(temporary))
            args.check_sdk = True
            parser = Mock()
            parser.error.side_effect = ValueError
            runner._validate_arguments(parser, args)
            args.behavior_threshold = 4
            with self.assertRaises(ValueError):
                runner._validate_arguments(parser, args)
            args.behavior_threshold = None
            args.rubric_threshold = 4
            with self.assertRaises(ValueError):
                runner._validate_arguments(parser, args)
            args.rubric_threshold = 0.75
            args.replay_cases = args.cases
            runner._validate_arguments(parser, args)
            self.assertEqual(args.behavior_threshold, 4)


class ManualRunnerTests(unittest.TestCase):
    def test_judge_deployment_is_independent_of_generation(self):
        with patch.dict(
            runner.os.environ,
            {
                "AZURE_AI_MODEL_DEPLOYMENT_NAME": "generation",
                "ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME": "judge",
            },
            clear=True,
        ):
            parser = argparse.ArgumentParser()
            runner._add_identity_arguments(parser)
        self.assertEqual(parser.parse_args([]).model_deployment, "judge")
        self.assertEqual(
            parser.parse_args(["--model-deployment", "explicit-judge"]).model_deployment,
            "explicit-judge",
        )

    def test_judge_deployment_keeps_the_legacy_environment_fallback(self):
        for configured in (None, ""):
            with self.subTest(configured=configured):
                environment = {"AZURE_AI_MODEL_DEPLOYMENT_NAME": "legacy-judge"}
                if configured is not None:
                    environment["ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME"] = configured
                with patch.dict(runner.os.environ, environment, clear=True):
                    parser = argparse.ArgumentParser()
                    runner._add_identity_arguments(parser)
                self.assertEqual(parser.parse_args([]).model_deployment, "legacy-judge")

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
            references=policy_reference_fixture(root, cases.load_cases(cases_path)),
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
            rubric_threshold=0.75,
            behavior_threshold=None,
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
            args.references = policy_reference_fixture(
                Path(temporary), cases.load_cases(args.cases)
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

    def test_pass_gate_rejects_failed_citation_checks(self):
        summary = {"case_checks": [{"status": "failed"}]}
        with self.assertRaisesRegex(RuntimeError, "answer/citation"):
            scoring.require_demo_pass(summary)

    def test_pass_gate_preserves_completeness_failures(self):
        summary = {
            "case_checks": [{"status": "passed"}],
            "expected_behavior": {"cases": [{"results": [{"passed": True}]}]},
            "policy_answers": {"cases": [{"results": [{"passed": False}]}]},
            "rubric_assessment": {"failed": 0},
            "assessment": {"failed": 1},
        }
        with self.assertRaisesRegex(RuntimeError, "native demo"):
            scoring.require_demo_pass(summary)

    def test_pass_gate_requires_the_native_assessment_in_both_profiles(self):
        for hosted in (False, True):
            with self.subTest(hosted=hosted):
                summary = {"case_checks": [{"status": "passed"}]}
                if hosted:
                    summary["assessment"] = {"failed": 0}
                with self.assertRaisesRegex(KeyError, "rubric_assessment"):
                    scoring.require_demo_pass(summary)

    def test_manual_runner_collects_and_scores_with_mocked_sdk_then_removes_dataset(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.arguments(Path(temporary))
            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            client.evals.create.side_effect = [
                SimpleNamespace(id="eval-behavior"),
                SimpleNamespace(id="eval-policy"),
            ]
            client.evals.runs.create.side_effect = [
                completed_run("run-behavior"),
                completed_run("run-policy"),
            ]
            client.evals.runs.output_items.list.side_effect = [
                [rubric_item()],
                [
                    scored_item(
                        "test",
                        ("response_completeness",),
                    )
                ],
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
            summary = json.loads(summary_file.read_text())
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["assessment"]["passed"], 1)
            self.assertNotIn("built_in", summary)
            self.assertEqual(
                summary["case_checks"],
                [
                    {
                        "case_id": "test",
                        "response_id": "response-test",
                        "status": "passed",
                        "failures": [],
                    }
                ],
            )
            uploaded_path = Path(
                project.datasets.upload_file.call_args.kwargs["file_path"]
            )
            self.assertFalse(uploaded_path.exists())
            self.assertEqual(client.evals.create.call_count, 2)
            requested = project.evaluators.create_version.call_args.kwargs
            definition = requested["evaluator_version"]["definition"]
            self.assertEqual(definition["type"], "rubric")
            self.assertEqual(
                summary["hr_score_format"], "native_weighted_rubric_0_to_1"
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
                "rubric_sha256": foundry.rubric_fingerprint(args.rubric_threshold),
                "judge_deployment": args.model_deployment,
                "rubric_threshold": args.rubric_threshold,
                "rubric_minimum_dimension_score": foundry.MIN_RUBRIC_DIMENSION_SCORE,
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
            rows = scoring.captured_dataset_rows(self.captured_summary())
            runner._write_dataset_rows(dataset_path, rows)
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
                "settings": foundry.EvaluationSettings("judge", 0.75, 1, 60),
                "credential": Mock(),
                "dataset_path": dataset_path,
                "summary": summary,
                "summary_path": root / "summary.json",
                "case_ids": {"test"},
            }

            def record_stage(**kwargs):
                if stage.call_count == 1:
                    raise RuntimeError("scoring interrupted")
                if kwargs["label"] == "expected_behavior":
                    summary["expected_behavior"] = {"cases": [rubric_case()]}

            with (
                patch.object(scoring, "AIProjectClient", return_value=project),
                patch.object(
                    scoring,
                    "run_stage",
                    side_effect=record_stage,
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
            for call in stage.call_args_list:
                self.assertEqual(call.kwargs["inline_rows"], rows)

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

            def record_stage(**kwargs):
                if kwargs["label"] == "expected_behavior":
                    summary["expected_behavior"] = {"cases": [rubric_case()]}

            with (
                patch.object(scoring, "AIProjectClient", return_value=project),
                patch.object(scoring, "run_stage", side_effect=record_stage) as stage,
            ):
                scoring.score_dataset(
                    project_endpoint="https://example.test",
                    dataset_name="answers",
                    evaluation_name="quality",
                    settings=foundry.EvaluationSettings("judge", 0.75, 1, 60),
                    credential=Mock(),
                    dataset_path=dataset_path,
                    summary=summary,
                    summary_path=root / "summary.json",
                    case_ids={"test"},
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
                    "rubric_query_field": "agent_query",
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
                            "passed" if checks_pass else "failed"
                        )
                        summary["expected_behavior"] = {
                            "cases": [
                                rubric_case(
                                    scores=(5, 5, 5) if behavior_pass else (3, 3, 3)
                                )
                            ]
                        }
                        summary["rubric_assessment"] = foundry.rubric_assessment(
                            summary["expected_behavior"]
                        )

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

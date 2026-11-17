"""Offline contracts for source-reviewed, saved-answer evaluation replays."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import run_foundry_evaluation as cli
import test_foundry_evaluation as existing_tests
from evaluation import cases, foundry, replay, runner
from test_foundry_evaluation import (
    agent_result,
    completed_run,
    scored_item,
)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference_fixture(root):
    args = existing_tests.ManualRunnerTests.arguments(root)
    args.behavior_threshold = 4
    args.replay_cases = root / "references.json"
    source = root / "source.pdf"
    source.write_bytes(b"Synthetic source bytes for an offline contract test")
    text = "At least two days."
    analysis = root / "analysis.json"
    analysis_hash = write_json(
        analysis,
        {
            "stringIndexType": "unicodeCodePoint",
            "content": text,
            "pages": [{"pageNumber": 1, "spans": [{"offset": 0, "length": len(text)}]}],
        },
    )
    original_case = cases.load_cases(args.cases)[0]
    clarification = {
        "id": "clarify",
        "query": "Which policy?",
        "hardRequirement": True,
    }
    case_hash = write_json(args.cases, {"cases": [original_case, clarification]})
    rubric_hash = hashlib.sha256(b"Recorded historical prompt fixture.").hexdigest()
    instructions = "Use the provided policy."
    captures = []
    for identifier, answer in (("bad", "More than two days."), ("good", text)):
        context = [{"content": text}]
        row = {
            **cases.evaluation_row(
                original_case,
                agent_result(response=answer),
                context=json.dumps(context),
            ),
            "generation_input": json.dumps(
                {"question": original_case["query"], "authorized_evidence": context}
            ),
        }
        clarify_row = {
            **cases.evaluation_row(
                clarification,
                agent_result(response="Which country?", response_id="clarify-response"),
                context="[]",
            ),
            "generation_input": None,
        }
        report = {
            "response_collection": "completed",
            "cases_sha256": case_hash,
            "project_endpoint": args.project_endpoint,
            "judge_deployment": args.model_deployment,
            "behavior_threshold": 4,
            "rubric_sha256": rubric_hash,
            "behavior_evaluator": {"name": "ask_hr_expected_behavior", "version": "9"},
            "agent": {
                "generation_instructions_sha256": hashlib.sha256(
                    instructions.encode()
                ).hexdigest()
            },
            "results": [row, clarify_row],
            **{
                stage: {
                    "evaluation_id": "old-eval-" + identifier,
                    "run_id": "old-run-" + identifier,
                    "cases": [
                        {"case_id": item["case_id"], "results": []}
                        for item in (row, clarify_row)
                    ],
                }
                for stage in ("built_in", "expected_behavior")
            },
        }
        report_path = root / (identifier + ".json")
        captures.append(
            {
                "id": identifier,
                "path": report_path.name,
                "sha256": write_json(report_path, report),
                "generation_instructions": instructions,
            }
        )
    manifest = {
        "schema_version": 1,
        "review": {
            "status": "approved",
            "basis": "Synthetic unit-test labels, not policy review",
        },
        "behavior_evaluator": {"name": "ask_hr_expected_behavior", "version": "9"},
        "sources": [
            {
                "id": "source",
                "path": source.name,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "analysis": {"path": analysis.name, "sha256": analysis_hash},
                "passages": [{"page": 1, "quote": text}],
            }
        ],
        "captures": captures,
        "references": [
            {
                "case_id": "test",
                "query": original_case["query"],
                "basis": "policy",
                "ground_truth": text,
                "source_ids": ["source"],
                "samples": [
                    {
                        "capture": identifier,
                        "expected": {"groundedness": identifier == "good"},
                        "reason": "Synthetic expected decision.",
                    }
                    for identifier in ("bad", "good")
                ],
            },
            {
                "case_id": "clarify",
                "query": clarification["query"],
                "basis": "application_contract",
                "ground_truth": "Ask which country applies.",
                "source_ids": [],
                "samples": [
                    {
                        "capture": "good",
                        "expected": {"behavioral_adherence": True},
                        "reason": "Clarification control.",
                    }
                ],
            },
        ],
    }
    write_json(args.replay_cases, manifest)
    return args, manifest


def load_fixture(args, **overrides):
    return replay.load_reference_replay(
        args.replay_cases,
        cases=cases.load_cases(args.cases),
        cases_sha256=hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        project_endpoint=args.project_endpoint,
        model_deployment=args.model_deployment,
        behavior_threshold=args.behavior_threshold,
        **overrides,
    )


class MaintainedReferenceTests(unittest.TestCase):
    def test_v2_preserves_sources_captures_labels_and_other_references(self):
        root = Path(__file__).parent
        original = json.loads(
            (root / "agent-evaluator-reference-cases.json").read_text(encoding="utf-8")
        )
        revised = json.loads(
            (root / "agent-evaluator-reference-cases-v2.json").read_text(
                encoding="utf-8"
            )
        )
        for field in ("schema_version", "behavior_evaluator", "sources", "captures"):
            with self.subTest(field=field):
                self.assertEqual(revised[field], original[field])
        original_cases = {item["case_id"]: item for item in original["references"]}
        revised_cases = {item["case_id"]: item for item in revised["references"]}
        self.assertEqual(original_cases.keys(), revised_cases.keys())
        comparison = "AGENT-EXPLICIT-COMPARISON"
        for case_id in original_cases.keys() - {comparison}:
            with self.subTest(case_id=case_id):
                self.assertEqual(revised_cases[case_id], original_cases[case_id])
        original_comparison = original_cases[comparison]
        revised_comparison = revised_cases[comparison]
        for field in ("case_id", "query", "basis", "samples"):
            self.assertEqual(revised_comparison[field], original_comparison[field])
        self.assertEqual(revised_comparison["source_ids"], ["HR-FI-003", "HR-HK-003"])
        unused_global_statement = (
            " Local policy takes precedence over the Global baseline of the local "
            "statutory minimum or 22 working days, whichever is greater."
        )
        self.assertEqual(
            original_comparison["ground_truth"],
            revised_comparison["ground_truth"] + unused_global_statement,
        )


class ReferenceReplayTests(unittest.TestCase):
    def test_documented_replay_uses_the_configured_judge(self):
        guide = (Path(__file__).resolve().parents[1] / "SHARING.md").read_text(
            encoding="utf-8"
        )
        section = guide.split("### Saved-answer reference replay", 1)[1]
        command = section.split("```powershell", 1)[1].split("```", 1)[0]
        tokens = command.replace("`\n", " ").split()
        self.assertEqual(tokens[1], r"scripts\run_foundry_evaluation.py")
        for generation in (None, "generation"):
            with (
                self.subTest(generation=generation),
                tempfile.TemporaryDirectory() as temporary,
            ):
                args, _ = reference_fixture(Path(temporary))
                environment = {
                    "FOUNDRY_PROJECT_ENDPOINT": args.project_endpoint,
                    "ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME": args.model_deployment,
                }
                if generation is not None:
                    environment["AZURE_AI_MODEL_DEPLOYMENT_NAME"] = generation
                argv = [
                    environment.get(token[5:]) if token.startswith("$env:") else token
                    for token in tokens[2:]
                ]
                with (
                    patch.dict(cli.os.environ, environment, clear=True),
                    patch(
                        "sys.argv",
                        ["run_foundry_evaluation.py", *[arg for arg in argv if arg]],
                    ),
                ):
                    parsed = cli.parse_args()
                self.assertTrue(parsed.prepare_replay)
                self.assertEqual(parsed.model_deployment, args.model_deployment)
                args.model_deployment = parsed.model_deployment
                self.assertEqual(len(load_fixture(args).rows), 3)

    def test_saved_answers_context_and_runtime_instructions_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, manifest = reference_fixture(root)
            result = load_fixture(args)
            self.assertEqual(len(result.rows), 3)
            for row in result.rows[:2]:
                capture_id, original_id = row["case_id"].split(":")
                report = json.loads((root / (capture_id + ".json")).read_text())
                original = next(
                    item for item in report["results"] if item["case_id"] == original_id
                )
                for field in (
                    "response",
                    "context",
                    "citations",
                    "tool_calls",
                    "response_id",
                ):
                    self.assertEqual(row[field], original[field])
                self.assertEqual(
                    row["agent_query"][0]["content"],
                    [{"type": "text", "text": "Use the provided policy."}],
                )
                self.assertEqual(
                    row["agent_query"][1]["content"],
                    [{"type": "text", "text": original["generation_input"]}],
                )
                self.assertEqual(row["ground_truth"], "At least two days.")
                self.assertNotIn("expected", row)
                self.assertNotIn("label_reason", row)
                self.assertEqual(
                    set(row),
                    set(
                        foundry.dataset_schema(references=True)["item_schema"][
                            "required"
                        ]
                    ),
                )
            for capture in manifest["captures"]:
                self.assertEqual(
                    hashlib.sha256((root / capture["path"]).read_bytes()).hexdigest(),
                    capture["sha256"],
                )

    def test_clarification_does_not_receive_fabricated_generation_instructions(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, _ = reference_fixture(Path(temporary))
            result = load_fixture(args)
            self.assertEqual(result.generation_case_ids, {"bad:test", "good:test"})
            self.assertEqual(
                result.rows[-1]["agent_query"],
                [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Which policy?"}],
                    }
                ],
            )
            self.assertEqual(
                result.provenance["task_adherence_not_evaluated"][0]["case_id"],
                "good:clarify",
            )

    def test_text_only_historical_capture_does_not_invent_tool_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, manifest = reference_fixture(root)
            old_rubric = hashlib.sha256(b"Previous reviewed HR rubric").hexdigest()
            for capture in manifest["captures"]:
                path = root / capture["path"]
                report = json.loads(path.read_text())
                report["rubric_sha256"] = old_rubric
                for row in report["results"]:
                    row.pop("output_items")
                capture["sha256"] = write_json(path, report)
            write_json(args.replay_cases, manifest)
            result = load_fixture(args)
            self.assertEqual(result.provenance["rubric_sha256"], old_rubric)
            for row in result.rows:
                self.assertEqual(
                    row["output_items"],
                    [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": row["response"]}],
                        }
                    ],
                )

    def test_replay_rejects_captures_with_different_rubrics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, manifest = reference_fixture(root)
            capture = manifest["captures"][0]
            path = root / capture["path"]
            report = json.loads(path.read_text())
            report["rubric_sha256"] = hashlib.sha256(b"Different rubric").hexdigest()
            capture["sha256"] = write_json(path, report)
            write_json(args.replay_cases, manifest)
            with self.assertRaisesRegex(ValueError, "rubric hashes differ"):
                load_fixture(args)

    def test_unreviewed_labels_cannot_be_scored(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, manifest = reference_fixture(Path(temporary))
            manifest["review"]["status"] = "pending"
            write_json(args.replay_cases, manifest)
            with self.assertRaisesRegex(ValueError, "human review"):
                load_fixture(args)
            self.assertEqual(len(load_fixture(args, require_review=False).rows), 3)

    def test_user_guidance_does_not_claim_human_adjudication_of_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, manifest = reference_fixture(Path(temporary))
            manifest["review"] = {
                "status": "guidance_confirmed",
                "basis": "Synthetic labels derived under user-confirmed guidance",
                "guidance": "Use country policy before Global policy for each topic.",
                "label_author": "assistant",
            }
            write_json(args.replay_cases, manifest)
            result = load_fixture(args)
            self.assertEqual(
                result.provenance["manifest"]["review"], manifest["review"]
            )
            for row in result.rows:
                self.assertNotIn("review", row)
            manifest["review"]["label_author"] = "human"
            write_json(args.replay_cases, manifest)
            with self.assertRaisesRegex(ValueError, "assistant-derived"):
                load_fixture(args)

    def test_changed_capture_or_wrong_prompt_is_rejected(self):
        for change in ("capture", "prompt"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                args, manifest = reference_fixture(root)
                if change == "capture":
                    with (root / "bad.json").open("a") as stream:
                        stream.write(" ")
                else:
                    manifest["captures"][0]["generation_instructions"] = (
                        "A different prompt."
                    )
                    write_json(args.replay_cases, manifest)
                with self.assertRaisesRegex(ValueError, "SHA-256|captured prompt"):
                    load_fixture(args)

    def test_reference_quote_must_appear_on_the_cited_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, manifest = reference_fixture(Path(temporary))
            manifest["sources"][0]["passages"][0]["quote"] = "Invented source text."
            write_json(args.replay_cases, manifest)
            with self.assertRaisesRegex(ValueError, "absent"):
                load_fixture(args)

    def test_duplicate_samples_and_nonboolean_expectations_are_rejected(self):
        for change in ("duplicate", "label"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as temporary,
            ):
                args, manifest = reference_fixture(Path(temporary))
                samples = manifest["references"][0]["samples"]
                if change == "duplicate":
                    samples.append(samples[0])
                else:
                    samples[0]["expected"]["groundedness"] = "false"
                write_json(args.replay_cases, manifest)
                with self.assertRaisesRegex(ValueError, "Duplicate|boolean"):
                    load_fixture(args)

    def test_false_passes_and_false_failures_are_reported_per_metric(self):
        summary = {
            "built_in": {
                "cases": [
                    {
                        "case_id": name,
                        "results": [
                            {
                                "name": "groundedness",
                                "passed": passed,
                                "score": 5 if passed else 1,
                                "reason": "Judge reason",
                            }
                        ],
                    }
                    for name, passed in (
                        ("bad", True),
                        ("good", False),
                        ("control", True),
                    )
                ]
            }
        }
        result = replay.compare_reference_judgments(
            summary,
            {
                name: {"groundedness": expected}
                for name, expected in (
                    ("bad", False),
                    ("good", True),
                    ("control", True),
                )
            },
        )
        self.assertFalse(result["passed"])
        self.assertEqual(
            result["metrics"]["groundedness"],
            {"agreement": 1, "false_pass": 1, "false_fail": 1},
        )
        self.assertEqual(len(result["comparisons"]), 3)
        with self.assertRaisesRegex(ValueError, "unscored"):
            replay.compare_reference_judgments(
                summary, {"missing": {"groundedness": True}}
            )

    def test_prepare_only_retains_inputs_without_calling_azure(self):
        with tempfile.TemporaryDirectory() as temporary:
            args, manifest = reference_fixture(Path(temporary))
            manifest["review"]["status"] = "pending"
            write_json(args.replay_cases, manifest)
            args.prepare_replay = True
            with (
                patch.object(cli, "AzureCliCredential") as credential,
                patch.object(cli, "score_reference_rows") as score,
            ):
                cli.score_replay_cases(args)
            credential.assert_not_called()
            score.assert_not_called()
            report = json.loads(
                next((args.output_dir / "results").glob("*.json")).read_text()
            )
            self.assertEqual(report["status"], "prepared")
            self.assertEqual(report["responses_regenerated"], 0)
            self.assertEqual(
                report["evaluator_input_format"], "foundry_text_content_parts"
            )
            self.assertTrue(Path(report["dataset_path"]).is_file())

    def test_scoring_failure_keeps_replay_inputs_and_source_reports_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, manifest = reference_fixture(root)
            with (
                patch.object(cli, "AzureCliCredential"),
                patch.object(cli, "invoke_agent") as invoke,
                patch.object(
                    cli,
                    "score_reference_rows",
                    side_effect=RuntimeError("Scoring failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "Scoring failed"),
            ):
                cli.score_replay_cases(args)
            invoke.assert_not_called()
            report = json.loads(
                next((args.output_dir / "results").glob("*.json")).read_text()
            )
            self.assertEqual(report["status"], "incomplete")
            self.assertTrue(Path(report["dataset_path"]).is_file())
            for capture in manifest["captures"]:
                self.assertEqual(
                    hashlib.sha256((root / capture["path"]).read_bytes()).hexdigest(),
                    capture["sha256"],
                )


class ReferenceFoundryContractTests(unittest.TestCase):
    def test_native_input_mappings_and_legacy_defaults(self):
        legacy = foundry.built_in_criteria("judge")
        self.assertEqual(len(legacy), 4)
        self.assertNotIn(
            "ground_truth", foundry.dataset_schema()["item_schema"]["properties"]
        )
        criteria = {
            item["name"]: item
            for item in foundry.built_in_criteria("judge", references=True)
        }
        self.assertEqual(
            criteria["response_completeness"]["data_mapping"],
            {"ground_truth": "{{item.ground_truth}}", "response": "{{item.response}}"},
        )
        self.assertEqual(
            criteria["groundedness"]["data_mapping"]["context"], "{{item.context}}"
        )
        self.assertEqual(
            criteria["groundedness"]["data_mapping"]["query"], "{{item.agent_query}}"
        )
        self.assertEqual(
            criteria["intent_resolution"]["data_mapping"]["query"],
            "{{item.agent_query}}",
        )
        self.assertEqual(
            criteria["relevance"]["data_mapping"]["query"], "{{item.agent_query}}"
        )
        for name in ("relevance", "groundedness", "intent_resolution"):
            self.assertEqual(
                criteria[name]["data_mapping"]["response"], "{{item.output_items}}"
            )
        self.assertEqual(
            criteria["coherence"]["data_mapping"]["response"], "{{item.response}}"
        )
        self.assertEqual(
            foundry.task_adherence_criterion("judge")["data_mapping"]["response"],
            "{{item.output_items}}",
        )

    def test_captured_context_does_not_require_or_fabricate_ground_truth(self):
        schema = foundry.dataset_schema(agent_inputs=True)["item_schema"]
        self.assertIn("agent_query", schema["required"])
        self.assertNotIn("ground_truth", schema["properties"])
        criteria = {
            item["name"]: item
            for item in foundry.built_in_criteria("judge", agent_inputs=True)
        }
        self.assertEqual(len(criteria), 4)
        for name in ("relevance", "groundedness", "intent_resolution"):
            with self.subTest(name=name):
                self.assertEqual(
                    criteria[name]["data_mapping"]["query"], "{{item.agent_query}}"
                )

    def test_binary_zero_is_a_valid_failure_not_an_execution_error(self):
        for score, passed in ((0, False), (1, True), (False, False), (True, True)):
            with self.subTest(score=score):
                summary = foundry.evaluation_summary(
                    evaluation=SimpleNamespace(id="eval"),
                    run=completed_run(passed=int(passed), failed=int(not passed)),
                    items=[
                        scored_item(
                            "case", ("task_adherence",), score=score, passed=passed
                        )
                    ],
                    expected_case_ids={"case"},
                    criterion_names={"task_adherence"},
                )
                self.assertEqual(summary["validation_errors"], [])
        for score, passed in ((2, True), (0.5, False), (0, True), (None, False)):
            with self.subTest(score=score, passed=passed):
                errors = foundry._validate_scores(
                    "case",
                    [{"name": "task_adherence", "score": score, "passed": passed}],
                    {"task_adherence"},
                    None,
                )
                self.assertTrue(errors)

    def test_inline_replay_uses_captured_items_not_an_agent_target(self):
        client = Mock()
        client.evals.create.return_value = SimpleNamespace(id="eval")
        client.evals.runs.create.return_value = completed_run()
        client.evals.runs.output_items.list.return_value = [
            scored_item("captured", ("groundedness",))
        ]
        rows = [{"case_id": "captured"}]
        schema = foundry.dataset_schema(references=True)
        summary = {
            "agent": {"name": "ask-hr", "version": "captured"},
            "dataset_version": "replay",
            "dataset_id": "azureai://lineage-only",
        }
        with tempfile.TemporaryDirectory() as temporary:
            runner.run_stage(
                client=client,
                name="replay",
                criteria=[{"name": "groundedness"}],
                settings=foundry.EvaluationSettings("judge", 4, 1, 60),
                summary=summary,
                summary_path=Path(temporary) / "summary.json",
                case_ids={"captured"},
                label="built_in",
                inline_rows=rows,
                schema=schema,
            )
        self.assertEqual(summary["built_in"]["validation_errors"], [])
        self.assertEqual(
            client.evals.create.call_args.kwargs["data_source_config"], schema
        )
        source = client.evals.runs.create.call_args.kwargs["data_source"]["source"]
        self.assertEqual(
            source, {"type": "file_content", "content": [{"item": rows[0]}]}
        )

    def test_catalog_versions_and_existing_hr_judge_are_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args, _ = reference_fixture(root)
            dataset = load_fixture(args)
            project = MagicMock()
            project.__enter__.return_value = project
            definitions = [
                *foundry.built_in_criteria("judge", references=True),
                foundry.task_adherence_criterion("judge"),
            ]
            project.evaluators.list.return_value = [
                SimpleNamespace(name=item["evaluator_name"], version="2")
                for item in definitions
            ]
            project.evaluators.get_version.return_value = SimpleNamespace(
                name="ask_hr_expected_behavior",
                version="9",
                definition={"prompt_text": "Previously captured rubric."},
            )
            summary = {
                "rubric_sha256": hashlib.sha256(
                    b"Previously captured rubric."
                ).hexdigest(),
                "dataset_version": "replay",
                "agent": {"name": "ask-hr", "version": "captured"},
                "behavior_evaluator": {
                    "name": "ask_hr_expected_behavior",
                    "version": "9",
                },
            }
            with (
                patch.object(runner, "AIProjectClient", return_value=project),
                patch.object(runner, "run_stage") as stage,
            ):
                runner.score_reference_rows(
                    project_endpoint=args.project_endpoint,
                    evaluation_name="Replay",
                    settings=foundry.EvaluationSettings("judge", 4, 1, 60),
                    credential=Mock(),
                    rows=dataset.rows,
                    generation_case_ids=dataset.generation_case_ids,
                    summary=summary,
                    summary_path=root / "result.json",
                )
            self.assertEqual(stage.call_count, 3)
            task = stage.call_args_list[-1].kwargs
            self.assertEqual(task["case_ids"], {"bad:test", "good:test"})
            self.assertEqual(task["criteria"][0]["evaluator_version"], "2")
            project.evaluators.create_version.assert_not_called()
            project.datasets.upload_file.assert_not_called()
            project.evaluators.get_version.assert_called_once_with(
                name="ask_hr_expected_behavior", version="9"
            )
            summary["rubric_sha256"] = hashlib.sha256(b"Changed rubric.").hexdigest()
            with (
                patch.object(runner, "AIProjectClient", return_value=project),
                patch.object(runner, "run_stage") as stage,
                self.assertRaisesRegex(ValueError, "recorded rubric"),
            ):
                runner.score_reference_rows(
                    project_endpoint=args.project_endpoint,
                    evaluation_name="Replay",
                    settings=foundry.EvaluationSettings("judge", 4, 1, 60),
                    credential=Mock(),
                    rows=dataset.rows,
                    generation_case_ids=dataset.generation_case_ids,
                    summary=summary,
                    summary_path=root / "result.json",
                )
            stage.assert_not_called()


if __name__ == "__main__":
    unittest.main()

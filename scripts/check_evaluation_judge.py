#!/usr/bin/env python3
"""Check a frozen HR grader against predeclared PDF-backed good/bad examples."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
from evaluation.agent import AgentResult
from evaluation.cases import evaluation_row
from evaluation.foundry import (
    DEFAULT_RUBRIC_THRESHOLD,
    MIN_RUBRIC_DIMENSION_SCORE,
    EvaluationSettings,
    built_in_criteria,
    create_rubric_evaluator,
    dataset_schema,
    rubric_assessment,
    rubric_criterion,
    rubric_definition,
    rubric_fingerprint,
)
from evaluation.references import (
    REFERENCE_INPUT_CONTRACT,
    load_policy_references,
    policy_evaluation_query,
)
from evaluation.runner import run_stage, write_summary

ROOT = Path(__file__).resolve().parents[1]


def load_checks(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Verify source hashes and frozen grading rules before constructing inputs.

    Parameters
    ----------
    path : pathlib.Path
        Policy-reference manifest with grouped positive/negative ``checks``.
        Expected decisions and their reasons stay in local report metadata.

    Returns
    -------
    tuple of (list of dict, dict)
        Judge input rows and separate reference/expected-decision provenance.
        These authored responses are not represented as agent captures.
    """
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        manifest.get("rubric_sha256") != rubric_fingerprint(DEFAULT_RUBRIC_THRESHOLD)
        or manifest.get("minimum_dimension_score") != MIN_RUBRIC_DIMENSION_SCORE
        or manifest.get("input_contract") != REFERENCE_INPUT_CONTRACT
    ):
        raise ValueError("Grading rules differ from the frozen judge-check definition")
    cases = [
        {"id": item["case_id"], "query": item["query"], "hardRequirement": True}
        for item in manifest["references"]
    ]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Judge-check question IDs must be unique")
    references = load_policy_references(path, cases)
    rows, expectations = [], {}
    for reference in manifest["references"]:
        group = reference["group"]
        if group not in {"new_question", "regression"}:
            raise ValueError("Judge checks must distinguish new questions from regressions")
        for check in reference["checks"]:
            identifier = check["id"]
            if (
                not isinstance(identifier, str) or not identifier.strip()
                or identifier in expectations
                or type(check["expected_pass"]) is not bool
                or not isinstance(check["response"], str) or not check["response"].strip()
                or not isinstance(check["reason"], str) or not check["reason"].strip()
            ):
                raise ValueError("Judge checks require unique IDs, answers and boolean labels")
            row = evaluation_row(
                {"id": identifier, "query": reference["query"]},
                AgentResult(
                    response=check["response"], references=[], tool_calls=[],
                    response_id="synthetic-" + identifier,
                ),
                context=json.dumps(reference["passages"], ensure_ascii=False),
            )
            row.update(
                ground_truth=references.answers[reference["case_id"]],
                response_language=reference["response_language"],
            )
            row["policy_query"] = policy_evaluation_query(row)
            rows.append(row)
            expectations[identifier] = {
                "group": group,
                "expected_pass": check["expected_pass"],
                "reason": check["reason"],
            }
    if {item["expected_pass"] for item in expectations.values()} != {True, False}:
        raise ValueError("Judge checks require both correct and deliberately incorrect answers")
    return rows, {
        "reference_answers": references.provenance,
        "expected_decisions": expectations,
    }


def compare_checks(
    assessment: dict[str, Any], expectations: dict[str, Any]
) -> dict[str, Any]:
    """
    Compare application decisions to labels without changing any judge score.

    Parameters
    ----------
    assessment : dict
        Validated per-case application decisions returned by ``rubric_assessment``.
    expectations : dict
        Predeclared group, expected pass/fail, and source-based reason per case.

    Returns
    -------
    dict
        Agreement, false-pass and false-fail counts, separated into new-question
        and regression groups. A deliberately incorrect answer failing is an
        agreement, not a failed judge check.
    """
    cases = assessment["cases"]
    if len(cases) != len(expectations) or {case["case_id"] for case in cases} != expectations.keys():
        raise ValueError("Judge results must cover every predeclared check exactly once")
    comparisons = []
    groups = {}
    for case in cases:
        expected = expectations[case["case_id"]]
        actual = case["passed"]
        if type(actual) is not bool:
            raise ValueError("Judge decisions must be boolean")
        outcome = (
            "agreement" if actual == expected["expected_pass"]
            else "false_pass" if actual else "false_fail"
        )
        counts = groups.setdefault(
            expected["group"], {"agreement": 0, "false_pass": 0, "false_fail": 0}
        )
        counts[outcome] += 1
        comparisons.append({
            "case_id": case["case_id"], **expected, "actual_pass": actual, "outcome": outcome,
        })
    return {
        "passed": all(item["outcome"] == "agreement" for item in comparisons),
        "groups": groups,
        "cases": comparisons,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checks", type=Path, default=ROOT / "tests" / "agent-quality-judge-checks.json")
    parser.add_argument("--project-endpoint", default=os.environ.get("FOUNDRY_PROJECT_ENDPOINT"))
    parser.add_argument("--model-deployment", default=os.environ.get("ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME"))
    parser.add_argument("--subscription", default=os.environ.get("AZURE_SUBSCRIPTION_ID"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--compare-groundedness", action="store_true")
    args = parser.parse_args()
    if not args.prepare_only and (not args.project_endpoint or not args.model_deployment):
        parser.error("Specify the Foundry project and judge model deployment")
    rows, provenance = load_checks(args.checks)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    inputs = args.output_dir / "inputs.jsonl"
    inputs.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    report_path = args.output_dir / "report.json"
    summary = {
        "status": "prepared",
        "purpose": "frozen_judge_spot_check",
        "evaluation_scope": {
            "response_source": "authored_examples",
            "dataset_role": provenance["reference_answers"]["manifest"].get(
                "dataset_role", "post_freeze_new_questions_and_separate_regressions"
            ),
            "interpretation": (
                "Checks reference-based correctness grading, not agent performance "
                "or source-only grounding. Review status is recorded per reference "
                "manifest; this is not independent HR or statistical validation."
            ),
        },
        "scoring_inputs": {
            "contract": REFERENCE_INPUT_CONTRACT,
            "rubric_query_field": "policy_query",
            "rubric_response_field": "response",
            "reference_usage": "pdf_derived_expected_facts_not_additional_policy",
            "expected_decisions_in_judge_inputs": False,
        },
        "dataset_version": datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
        "agent": {"name": "ask-hr", "version": "not_invoked"},
        "project_endpoint": args.project_endpoint,
        "judge_deployment": args.model_deployment,
        "inputs_sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(),
        "rubric_sha256": rubric_fingerprint(DEFAULT_RUBRIC_THRESHOLD),
        "rubric_definition": rubric_definition(DEFAULT_RUBRIC_THRESHOLD),
        "scoring_sdk_versions": {
            name: version(name) for name in ("azure-ai-projects", "openai")
        },
        **provenance,
    }
    write_summary(report_path, summary)
    if args.prepare_only:
        print(f"Prepared unscored examples: {report_path}")
        return
    settings = EvaluationSettings(args.model_deployment, DEFAULT_RUBRIC_THRESHOLD, 10, 1800)
    try:
        summary["status"] = "incomplete"
        with (
            AzureCliCredential(subscription=args.subscription, process_timeout=120) as credential,
            AIProjectClient(
                endpoint=args.project_endpoint, credential=credential,
                allow_preview=True, connection_timeout=30, read_timeout=120,
            ) as project,
            project.get_openai_client(timeout=60, max_retries=0) as client,
        ):
            evaluator = create_rubric_evaluator(project, settings)
            summary["behavior_evaluator"] = {"name": evaluator.name, "version": evaluator.version}
            run_stage(
                client=client, name="Ask HR frozen judge spot check",
                criteria=[rubric_criterion(evaluator, settings, policy_answers=True)],
                settings=settings, summary=summary, summary_path=report_path,
                case_ids={row["case_id"] for row in rows}, label="expected_behavior",
                inline_rows=rows, schema=dataset_schema(policy_answers=True),
            )
            summary["rubric_assessment"] = rubric_assessment(summary["expected_behavior"])
            summary["judge_comparison"] = compare_checks(
                summary["rubric_assessment"], provenance["expected_decisions"]
            )
            write_summary(report_path, summary)
            if args.compare_groundedness:
                groundedness = next(
                    item for item in built_in_criteria(args.model_deployment)
                    if item["name"] == "groundedness"
                )
                run_stage(
                    client=client, name="Ask HR separate generic grounding comparison",
                    criteria=[groundedness], settings=settings, summary=summary,
                    summary_path=report_path, case_ids={row["case_id"] for row in rows},
                    label="groundedness_diagnostic", inline_rows=rows,
                    schema=dataset_schema(policy_answers=True),
                )
                summary["groundedness_diagnostic_scope"] = (
                    "Source-only diagnostic: no reference answers are mapped into "
                    "this grader. Its scores are "
                    "not compared to reference-based correctness labels."
                )
            summary["status"] = "completed"
    finally:
        write_summary(report_path, summary)
    print(f"Judge-check report: {report_path}")
    print(json.dumps(summary["judge_comparison"]["groups"]))
    if not summary["judge_comparison"]["passed"]:
        raise RuntimeError("The frozen grader disagreed with predeclared labels; review the saved reasons")


if __name__ == "__main__":
    main()

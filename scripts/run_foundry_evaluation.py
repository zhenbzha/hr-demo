#!/usr/bin/env python3
"""Collect hosted-agent responses or score saved answers with Foundry."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential
from evaluation.agent import endpoint_version_status, invoke_agent
from evaluation.cases import (
    evaluation_row,
    expected_behavior,
    load_cases,
    validate_case_result,
)
from evaluation.foundry import (
    DEFAULT_RUBRIC_THRESHOLD,
    MIN_RUBRIC_DIMENSION_SCORE,
    EvaluationSettings,
    rubric_fingerprint,
)
from evaluation.references import load_policy_references, policy_dataset_rows
from evaluation.runner import (
    captured_dataset_rows,
    check_evaluation_sdk,
    require_demo_pass,
    score_dataset,
    write_summary,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_dataset_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write evaluation rows as UTF-8 JSONL to an existing output directory."""
    content = "".join(
        json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
        for row in rows
    )
    path.write_text(content, encoding="utf-8")


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model-deployment",
        default=os.environ.get("ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME")
        or os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME"),
        help=(
            "Judge deployment, or set ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME. "
            "Falls back to AZURE_AI_MODEL_DEPLOYMENT_NAME for existing callers."
        ),
    )
    for option, variable, fallback in (
        ("project-endpoint", "FOUNDRY_PROJECT_ENDPOINT", None),
        ("subscription", "AZURE_SUBSCRIPTION_ID", None),
        ("agent-endpoint", "AGENT_ASK_HR_RESPONSES_ENDPOINT", None),
        ("agent-name", "ASK_HR_AGENT_NAME", "ask-hr"),
        ("agent-version", "ASK_HR_AGENT_VERSION", None),
        ("user-id", "ASK_HR_EVAL_USER_ID", None),
        ("knowledge-base", "ASK_HR_KNOWLEDGE_BASE", None),
        ("dataset-name", "ASK_HR_EVAL_DATASET_NAME", "ask-hr-quality"),
        ("evaluation-name", "ASK_HR_EVALUATION_NAME", "Ask HR quality evaluation"),
    ):
        parser.add_argument(
            f"--{option}",
            default=os.environ.get(variable, fallback),
            help=f"Or set {variable}.",
        )


def _validate_arguments(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    collecting = not args.check_sdk and args.benchmark_report is None
    required = ["project_endpoint"]
    if not args.check_sdk:
        required += ["model_deployment", "dataset_name", "evaluation_name"]
    if collecting:
        required += [
            "agent_endpoint",
            "agent_name",
            "agent_version",
            "user_id",
        ]
    if missing := [name for name in required if not getattr(args, name)]:
        parser.error("Missing CLI/environment configuration: " + ", ".join(missing))
    for name in ("poll_interval", "timeout", "request_timeout"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{name} must be finite and positive")
    if not math.isfinite(args.max_retry_seconds) or args.max_retry_seconds < 0:
        parser.error("max_retry_seconds must be finite and nonnegative")
    if not math.isfinite(args.rubric_threshold) or not 0 <= args.rubric_threshold <= 1:
        parser.error("rubric_threshold must be finite and between 0 and 1")
    if not args.cases.is_file():
        parser.error(f"Cases file does not exist: {args.cases}")
    if collecting and not args.references.is_file():
        parser.error(f"Policy reference file does not exist: {args.references}")
    if args.benchmark_report is not None and not args.benchmark_report.is_file():
        parser.error(f"Benchmark report does not exist: {args.benchmark_report}")
    if collecting:
        try:
            UUID(args.user_id)
            endpoint_version_status(
                args.agent_endpoint, args.agent_name, args.agent_version
            )
        except ValueError as error:
            parser.error(str(error))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    _add_identity_arguments(parser)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--benchmark-report",
        type=Path,
        help="Score captured local-pipeline responses without invoking the hosted agent.",
    )
    mode.add_argument(
        "--check-sdk",
        action="store_true",
        help="Check installed evaluator SDK capabilities without Azure calls.",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "tests" / "agent-quality-cases.json",
    )
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / ".foundry")
    parser.add_argument(
        "--references",
        type=Path,
        default=PROJECT_ROOT / "tests" / "agent-quality-references.json",
        help="PDF-backed expected answers for hosted policy-answer evaluation.",
    )
    parser.add_argument("--poll-interval", type=float, default=10)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--request-timeout", type=float, default=300)
    parser.add_argument("--max-retry-seconds", type=float, default=300)
    parser.add_argument(
        "--rubric-threshold", type=float, default=DEFAULT_RUBRIC_THRESHOLD,
        help="Native HR rubric passing score, 0-1. Default: 0.75.",
    )
    parser.add_argument(
        "--require-behavior-pass",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Native runs: require citation checks, a native rubric pass, every "
            "dimension >= 3, and applicable policy-answer checks. Default: manual review."
        ),
    )
    parser.add_argument(
        "--retain-dataset", action=argparse.BooleanOptionalAction, default=False
    )
    args = parser.parse_args()
    _validate_arguments(parser, args)
    return args


def collect_responses(
    args: argparse.Namespace,
    cases: list[dict[str, Any]],
    credential: TokenCredential,
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Ask the agent each test question and prepare rows for scoring.

    Parameters
    ----------
    args : argparse.Namespace
        Command-line settings returned by ``parse_args``.
    cases : list of dict
        Test questions and expectations returned by ``load_cases``.
    credential : TokenCredential
        Entra credential for Foundry and delegated Search access.
    summary : dict
        Report to update with per-case checks. ``collecting_case``
        identifies the current case if response collection fails.

    Returns
    -------
    list of dict
        One row per response, containing the question, answer, citations,
        supporting policy text, and expected behavior. Includes answers
        whose citation checks failed.
    """
    rows = []
    summary["case_checks"] = []
    for case in cases:
        summary["collecting_case"] = case["id"]
        result = invoke_agent(
            endpoint=args.agent_endpoint,
            agent_name=args.agent_name,
            credential=credential,
            user_id=args.user_id,
            query=case["query"],
            timeout=args.request_timeout,
            max_retry_seconds=args.max_retry_seconds,
        )
        checks = validate_case_result(case, result)
        summary["case_checks"].append(
            {
                "case_id": case["id"],
                "response_id": result.response_id,
                "status": checks.status,
                "failures": checks.failures,
            }
        )
        rows.append(evaluation_row(case, result))
        print(f"Collected {case['id']}: {checks.status}")
    summary.pop("collecting_case")
    return rows


def score_benchmark_report(args: argparse.Namespace) -> None:
    """
    Validate a saved benchmark and score its captured answers with Foundry.

    Require matching cases, rubric, and scoring options. Reuse the captured
    responses without invoking an agent. Scoring results overwrite the report,
    including on failure; preserve a separate copy before rescoring.

    Parameters
    ----------
    args : argparse.Namespace
        Validated CLI settings. ``benchmark_report`` identifies an
        incomplete scoring report whose response collection completed;
        ``cases``, project, judge, threshold, and dataset settings must
        agree with that capture.
    """
    cases = load_cases(args.cases)
    summary = json.loads(args.benchmark_report.read_text(encoding="utf-8"))
    expected_metadata = {
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "rubric_sha256": rubric_fingerprint(args.rubric_threshold),
        "judge_deployment": args.model_deployment,
        "rubric_threshold": args.rubric_threshold,
        "rubric_minimum_dimension_score": MIN_RUBRIC_DIMENSION_SCORE,
        "project_endpoint": args.project_endpoint,
    }
    if (
        summary.get("status") != "incomplete"
        or summary.get("response_collection") != "completed"
        or any(summary.get(key) != value for key, value in expected_metadata.items())
    ):
        raise ValueError(
            "Captured benchmark is incomplete or its cases/rubric/scoring configuration differ"
        )
    results = summary["results"]
    case_map = {case["id"]: case for case in cases}
    identifiers = [result["case_id"] for result in results]
    if len(identifiers) != len(case_map) or set(identifiers) != set(case_map):
        raise ValueError(
            "Captured benchmark does not uniquely cover all evaluation cases"
        )
    for result in results:
        case = case_map[result["case_id"]]
        if result["query"] != case["query"] or result[
            "expected_behavior"
        ] != expected_behavior(case):
            raise ValueError(
                "Captured query or expected behavior differs from the reviewed case"
            )
    rows = captured_dataset_rows(summary)
    summary["scoring_complete"] = False
    try:
        with (
            AzureCliCredential(subscription=args.subscription, process_timeout=120) as credential,
            tempfile.TemporaryDirectory(prefix="ask-hr-scoring-") as temporary,
        ):
            dataset_path = Path(temporary) / "responses.jsonl"
            _write_dataset_rows(dataset_path, rows)
            score_dataset(
                project_endpoint=args.project_endpoint,
                dataset_name=args.dataset_name,
                evaluation_name=args.evaluation_name,
                settings=EvaluationSettings(
                    args.model_deployment,
                    args.rubric_threshold,
                    args.poll_interval,
                    args.timeout,
                ),
                credential=credential,
                dataset_path=dataset_path,
                summary=summary,
                summary_path=args.benchmark_report,
                case_ids=set(case_map),
                request_timeout=args.request_timeout,
            )
            summary["scoring_complete"] = True
            summary["status"] = "completed"
            if args.require_behavior_pass:
                require_demo_pass(summary)
    finally:
        write_summary(args.benchmark_report, summary)
    print(f"Scored captured benchmark: {args.benchmark_report}")


def main() -> None:
    args = parse_args()
    check_evaluation_sdk(args.project_endpoint)
    if args.check_sdk:
        print("Foundry evaluation SDK capabilities are available.")
        return
    if args.benchmark_report is not None:
        score_benchmark_report(args)
        return
    cases = load_cases(args.cases)
    references = load_policy_references(args.references, cases)
    version = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    summary_path = args.output_dir / "results" / f"evaluation-summary-{version}.json"
    summary = {
        "status": "incomplete",
        "dataset_version": version,
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "rubric_sha256": rubric_fingerprint(args.rubric_threshold),
        "judge_deployment": args.model_deployment,
        "rubric_threshold": args.rubric_threshold,
        "rubric_minimum_dimension_score": MIN_RUBRIC_DIMENSION_SCORE,
        "reference_answers": references.provenance,
        "evaluation_scope": {
            "response_source": "hosted_agent_endpoint",
            "dataset_role": (
                "calibration_set"
                if args.cases.resolve() == PROJECT_ROOT / "tests" / "agent-quality-cases.json"
                else "custom_case_set"
            ),
            "interpretation": (
                "Sample-set results from the authenticated agent endpoint; "
                "not independent generalization or web-app validation."
            ),
        },
        "agent": {
            "name": args.agent_name,
            "version": args.agent_version,
            "version_verification": endpoint_version_status(
                args.agent_endpoint, args.agent_name, args.agent_version
            ),
            "knowledge_base_label": args.knowledge_base,
        },
    }
    try:
        with (
            AzureCliCredential(subscription=args.subscription, process_timeout=120) as credential,
            tempfile.TemporaryDirectory(prefix="ask-hr-evaluation-") as temporary,
        ):
            rows = collect_responses(args, cases, credential, summary)
            rows = policy_dataset_rows(rows, references)
            dataset_dir = (
                args.output_dir / "datasets" if args.retain_dataset else Path(temporary)
            )
            dataset_dir.mkdir(parents=True, exist_ok=True)
            dataset_path = dataset_dir / f"evaluation-{version}.jsonl"
            _write_dataset_rows(dataset_path, rows)
            summary["dataset_sha256"] = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
            score_dataset(
                project_endpoint=args.project_endpoint,
                dataset_name=args.dataset_name,
                evaluation_name=args.evaluation_name,
                settings=EvaluationSettings(
                    args.model_deployment,
                    args.rubric_threshold,
                    args.poll_interval,
                    args.timeout,
                ),
                credential=credential,
                dataset_path=dataset_path,
                summary=summary,
                summary_path=summary_path,
                case_ids={case["id"] for case in cases},
                request_timeout=args.request_timeout,
                policy_case_ids=set(references.answers),
            )
            summary["status"] = "completed"
            if args.require_behavior_pass:
                require_demo_pass(summary)
    finally:
        write_summary(summary_path, summary)
    print(f"Evaluation report: {summary_path}")
    assessment = summary["assessment"]
    print(
        f"Native demo sample-set checks: {assessment['passed']} passed, "
        f"{assessment['failed']} failed (PDF-fact v6)."
    )
    print(f"Test scope: {summary['evaluation_scope']}")
    print(
        "Reference-based correctness: HR rubric with a per-dimension floor. "
        "Source-only Groundedness is not part of this suite; no cases are excluded."
    )


if __name__ == "__main__":
    main()

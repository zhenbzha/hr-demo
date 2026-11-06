#!/usr/bin/env python3
"""Measure the local HR pipeline and evaluate its answers with Foundry."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential, get_bearer_token_provider
from evaluation.agent import AgentResult
from evaluation.cases import evaluation_row, load_cases, validate_case_result
from evaluation.foundry import (
    DEFAULT_RUBRIC_THRESHOLD,
    EvaluationSettings,
    rubric_fingerprint,
)
from evaluation.runner import (
    captured_dataset_rows,
    check_evaluation_sdk,
    score_dataset,
    write_summary,
)
from ingestion.azure_http import (
    SEARCH_API_VERSION,
    SEARCH_SCOPE,
    authorization_headers,
    request_json,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = PROJECT_ROOT / "ask-hr" / "src" / "ask-hr"
sys.path.insert(0, str(AGENT_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-base-name", required=True)
    parser.add_argument("--knowledge-source-name", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "tests" / "agent-quality-cases.json",
    )
    parser.add_argument(
        "--judge-model-deployment",
        default=os.environ.get("ASK_HR_EVAL_MODEL_DEPLOYMENT_NAME")
        or os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME"),
    )
    parser.add_argument(
        "--rubric-threshold", type=float, default=DEFAULT_RUBRIC_THRESHOLD,
        help="Native HR rubric passing score, 0-1. Default: 0.75.",
    )
    parser.add_argument("--poll-interval", type=float, default=10)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--request-timeout", type=float, default=300)
    parser.add_argument(
        "--evaluation-python",
        type=Path,
        help="Use a separate Python environment for Foundry scoring without changing the agent runtime.",
    )
    parser.add_argument(
        "--require-behavior-pass",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Fail on low HR behavior scores or failed citation checks.",
    )
    args = parser.parse_args()
    if not args.judge_model_deployment:
        parser.error("Set --judge-model-deployment or AZURE_AI_MODEL_DEPLOYMENT_NAME")
    for name in ("poll_interval", "timeout", "request_timeout"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{name} must be finite and positive")
    if (
        not math.isfinite(args.rubric_threshold)
        or not 0 <= args.rubric_threshold <= 1
    ):
        parser.error("rubric_threshold must be between 0 and 1")
    if not args.cases.is_file():
        parser.error(f"Cases file does not exist: {args.cases}")
    if args.evaluation_python is not None and not args.evaluation_python.is_file():
        parser.error(f"Evaluation Python does not exist: {args.evaluation_python}")
    return args


def configure_pipeline(args: argparse.Namespace) -> str:
    project_endpoint = (
        os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
        or os.environ.get("AZURE_AIPROJECT_ENDPOINT")
    )
    if not project_endpoint:
        raise RuntimeError("Set FOUNDRY_PROJECT_ENDPOINT before running the benchmark")
    os.environ["FOUNDRY_PROJECT_ENDPOINT"] = project_endpoint
    for name in (
        "AZURE_AI_MODEL_DEPLOYMENT_NAME",
        "AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME",
        "AZURE_SEARCH_ENDPOINT",
    ):
        if not os.environ.get(name):
            raise RuntimeError(f"Set {name} before running the benchmark")
    os.environ["AZURE_SEARCH_KNOWLEDGE_BASE_NAME"] = args.knowledge_base_name
    os.environ["AZURE_SEARCH_KNOWLEDGE_SOURCE_NAME"] = args.knowledge_source_name
    web_base_url = os.environ.get("ASK_HR_WEB_BASE_URL") or os.environ.get(
        "SERVICE_WEB_URI"
    )
    if not web_base_url:
        raise RuntimeError("The Ask HR web base URL is unavailable")
    os.environ["ASK_HR_WEB_BASE_URL"] = web_base_url
    os.environ.setdefault(
        "ASK_HR_SUPPORTED_REGIONS_JSON",
        '{"india":"India","finland":"Finland","hongkong":"Hong Kong"}',
    )
    os.environ.setdefault("ASK_HR_GLOBAL_REGION_KEY", "global")
    return project_endpoint


def knowledge_binding(
    endpoint: str,
    knowledge_base: str,
    knowledge_source: str,
    credential: TokenCredential,
) -> dict[str, Any]:
    """Resolve the knowledge base, source, and index used by the benchmark.

    Raise ValueError unless the base uses only the specified Search source.
    """
    definitions = {}
    for collection, name in (
        ("knowledgebases", knowledge_base),
        ("knowledgesources", knowledge_source),
    ):
        _, _, definitions[collection] = request_json(
            "GET",
            f"{endpoint.rstrip('/')}/{collection}/{quote(name, safe='')}"
            f"?api-version={SEARCH_API_VERSION}",
            headers=authorization_headers(credential, SEARCH_SCOPE),
        )
    base = definitions["knowledgebases"]
    source = definitions["knowledgesources"]
    if [item["name"] for item in base.get("knowledgeSources", [])] != [
        knowledge_source
    ]:
        raise ValueError(
            "The benchmark KB must use exactly the selected knowledge source"
        )
    index_name = source.get("searchIndexParameters", {}).get("searchIndexName")
    if (
        source.get("kind") != "searchIndex"
        or not isinstance(index_name, str)
        or not index_name
    ):
        raise ValueError("The benchmark knowledge source must identify a Search index")
    return {
        "endpoint": endpoint,
        "knowledgeBase": knowledge_base,
        "knowledgeSource": knowledge_source,
        "indexName": index_name,
        "definitions": definitions,
    }


async def run_case(
    *,
    pipeline: Any,
    case: dict[str, Any],
    user_search_token: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    decision = await pipeline.classify_regions(case["query"])
    classifier_seconds = time.perf_counter() - started
    search_seconds = 0.0
    generation_seconds = 0.0
    references: list[dict[str, Any]] = []
    retrieval_regions: list[str] = []
    retrieval_evidence: list[dict[str, Any]] = []
    compact_evidence: list[dict[str, Any]] = []
    generation_input: str | None = None
    generation_response_schema: dict[str, Any] | None = None

    if decision.status != "retrieve":
        answer = decision.message or ""
    else:
        retrieval_regions = [*decision.regions, pipeline.GLOBAL_REGION]
        search_started = time.perf_counter()
        retrieval_evidence = await pipeline.retrieve_authorized_evidence(
            question=case["query"],
            regions=retrieval_regions,
            user_search_token=user_search_token,
        )
        search_seconds = time.perf_counter() - search_started
        references, compact_evidence = pipeline.prepare_evidence(retrieval_evidence)
        if not any(item.get("references") for item in compact_evidence):
            answer = pipeline.NO_ACCESSIBLE_EVIDENCE
        else:
            generation_input = pipeline.build_generation_input(
                question=case["query"],
                previous_questions=[],
                local_regions=decision.regions,
                compact_evidence=compact_evidence,
            )
            generation_response_schema = pipeline.build_answer_response_format(
                references
            ).model_json_schema()
            generation_started = time.perf_counter()
            answer, references = await pipeline.generate_answer(
                generation_input, references
            )
            generation_seconds = time.perf_counter() - generation_started
    total_seconds = time.perf_counter() - started
    if not answer:
        raise RuntimeError("The pipeline returned an empty answer")

    # Match the Responses adapter's structured citation output.
    source_references = (
        []
        if answer == pipeline.NO_ACCESSIBLE_EVIDENCE
        else [
            reference["sourceData"]
            for reference in pipeline.unique_references(references)
        ]
    )
    agent_result = AgentResult(
        response=answer,
        references=source_references,
        response_id="local-" + uuid4().hex,
        tool_calls=[],
    )
    checks = validate_case_result(case, agent_result)
    return {
        **evaluation_row(
            case,
            agent_result,
            context=json.dumps(compact_evidence, ensure_ascii=False)
            if compact_evidence
            else None,
        ),
        "diagnostics": {
            "classification": {
                "regions": decision.regions,
                "status": decision.status,
            },
            "retrieval_regions": retrieval_regions,
        },
        "retrieval_evidence": retrieval_evidence,
        "generation_input": generation_input,
        "generation_response_schema": generation_response_schema,
        "response_id_origin": "local_execution_not_hosted_response",
        "classifierSeconds": classifier_seconds,
        "searchSeconds": search_seconds,
        "generationSeconds": generation_seconds,
        "totalSeconds": total_seconds,
        "checks": {
            "case_id": case["id"],
            "response_id": agent_result.response_id,
            "status": checks.status,
            "failures": checks.failures,
        },
    }


def latency_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {}
    for field in (
        "classifierSeconds",
        "searchSeconds",
        "generationSeconds",
        "totalSeconds",
    ):
        values = [item[field] for item in results]
        summary[field] = {
            "samples": len(values),
            "median": statistics.median(values),
            "p95": statistics.quantiles(values, n=100, method="inclusive")[94]
            if len(values) > 1
            else values[0],
        }
    return summary


def quality_summary(summary: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, list[dict[str, Any]]] = {}
    for stage in ("built_in", "expected_behavior"):
        for case in summary[stage]["cases"]:
            for result in case["results"]:
                metrics.setdefault(result["name"], []).append(result)
    return {
        name: {
            "cases": len(results),
            "passed": sum(result["passed"] for result in results),
            "failed": sum(not result["passed"] for result in results),
            "meanScore": statistics.mean(result["score"] for result in results),
            "minScore": min(result["score"] for result in results),
        }
        for name, results in metrics.items()
    }


def evaluation_command(args: argparse.Namespace, project_endpoint: str) -> list[str]:
    if args.evaluation_python is None:
        raise ValueError(
            "An explicit evaluation Python is required for a separate runtime"
        )
    return [
        str(args.evaluation_python.resolve()),
        str(PROJECT_ROOT / "scripts" / "run_foundry_evaluation.py"),
        "--project-endpoint",
        project_endpoint,
        "--model-deployment",
        args.judge_model_deployment,
        "--cases",
        str(args.cases.resolve()),
        "--dataset-name",
        "ask-hr-pipeline-benchmark",
        "--evaluation-name",
        args.label,
        "--rubric-threshold",
        str(args.rubric_threshold),
        "--poll-interval",
        str(args.poll_interval),
        "--timeout",
        str(args.timeout),
        "--request-timeout",
        str(args.request_timeout),
    ]


def score_in_separate_runtime(
    args: argparse.Namespace, project_endpoint: str, summary: dict[str, Any]
) -> None:
    command = [
        *evaluation_command(args, project_endpoint),
        "--benchmark-report",
        str(args.output.resolve()),
    ]
    try:
        subprocess.run(command, check=True)
    finally:
        # Reload saved run IDs and partial scores even if the scorer fails.
        summary.update(json.loads(args.output.read_text(encoding="utf-8")))


async def collect_responses(
    *,
    pipeline: Any,
    args: argparse.Namespace,
    cases: list[dict[str, Any]],
    token_provider: Callable[[], str],
    summary: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        summary["collecting_case"] = case["id"]
        write_summary(args.output, summary)
        async with asyncio.timeout(args.request_timeout):
            token = await asyncio.to_thread(token_provider)
            result = await run_case(
                pipeline=pipeline, case=case, user_search_token=token
            )
        summary["results"].append(result)
        summary["case_checks"].append(result["checks"])
        rows.append(
            {
                field: result[field]
                for field in (
                    "case_id",
                    "query",
                    "response",
                    "context",
                    "citations",
                    "expected_behavior",
                    "tool_calls",
                    "tool_definitions",
                    "response_id",
                )
            }
        )
        write_summary(args.output, summary)
        print(
            f"Collected {case['id']}: {result['checks']['status']} "
            f"({result['totalSeconds']:.3f}s)",
            flush=True,
        )
    summary.pop("collecting_case")
    summary["latency"] = latency_summary(summary["results"])
    summary["passed"] = sum(
        check["status"] == "passed" for check in summary["case_checks"]
    )
    summary["failed"] = sum(
        check["status"] == "failed" for check in summary["case_checks"]
    )
    summary["response_collection"] = "completed"
    write_summary(args.output, summary)
    return rows


async def main_async(args: argparse.Namespace) -> None:
    project_endpoint = configure_pipeline(args)
    cases = load_cases(args.cases)
    if args.evaluation_python is None:
        check_evaluation_sdk(project_endpoint)
    else:
        subprocess.run(
            [*evaluation_command(args, project_endpoint), "--check-sdk"], check=True
        )
    import ask_hr_pipeline as pipeline
    from permission_contract import validated_search_endpoint

    source_hashes = {
        name: hashlib.sha256((AGENT_ROOT / name).read_bytes()).hexdigest()
        for name in (
            "ask_hr_pipeline.py",
            "permission_contract.py",
            "region_classification.py",
        )
    }
    summary: dict[str, Any] = {
        "status": "incomplete",
        "label": args.label,
        "started_at": datetime.now(UTC).isoformat(),
        "dataset_version": datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "rubric_sha256": rubric_fingerprint(args.rubric_threshold),
        "judge_deployment": args.judge_model_deployment,
        "project_endpoint": project_endpoint,
        "rubric_threshold": args.rubric_threshold,
        "execution_scope": (
            "Local production classification, live authorized Foundry IQ retrieval, "
            "and answer generation; excludes hosted HTTP transport and PDF rendering."
        ),
        "latency_scope": "Pipeline execution only; excludes Foundry evaluator scoring.",
        "agent": {
            "name": "ask-hr",
            "version": "local-" + source_hashes["ask_hr_pipeline.py"][:16],
            "source_hashes": source_hashes,
            "generation_model": os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
            "generation_reasoning_effort": pipeline.GENERATION_REASONING_EFFORT,
            "generation_response_schema_template": pipeline.PolicyAnswer.model_json_schema(),
            "classification_model": os.environ[
                "AZURE_AI_CLASSIFIER_MODEL_DEPLOYMENT_NAME"
            ],
            "generation_instructions": pipeline.GENERATION_INSTRUCTIONS,
            "classification_instructions": pipeline.CLASSIFICATION_INSTRUCTIONS,
            "generation_instructions_sha256": hashlib.sha256(
                pipeline.GENERATION_INSTRUCTIONS.encode()
            ).hexdigest(),
            "classification_instructions_sha256": hashlib.sha256(
                pipeline.CLASSIFICATION_INSTRUCTIONS.encode()
            ).hexdigest(),
        },
        "sdk_versions": {
            name: importlib.metadata.version(name)
            for name in ("agent-framework-core", "azure-ai-projects", "openai")
        },
        "case_checks": [],
        "results": [],
    }
    try:
        with AzureCliCredential() as credential:
            summary["binding"] = knowledge_binding(
                validated_search_endpoint(),
                args.knowledge_base_name,
                args.knowledge_source_name,
                credential,
            )
            rows = await collect_responses(
                pipeline=pipeline,
                args=args,
                cases=cases,
                token_provider=get_bearer_token_provider(credential, SEARCH_SCOPE),
                summary=summary,
            )
            rows = captured_dataset_rows(summary)
            if args.evaluation_python is not None:
                score_in_separate_runtime(args, project_endpoint, summary)
            else:
                score_collected_rows(args, project_endpoint, credential, rows, summary)
            summary["quality"] = quality_summary(summary)
            summary["status"] = "completed"
            summary["completed_at"] = datetime.now(UTC).isoformat()
            if args.require_behavior_pass and (
                summary["failed"]
                or summary["quality"]["hr_rubric"]["failed"]
            ):
                raise RuntimeError(
                    "Some agent requirements failed; see the benchmark report"
                )
    finally:
        write_summary(args.output, summary)
    print(f"Performance and agent evaluation report: {args.output}")


def score_collected_rows(
    args: argparse.Namespace,
    project_endpoint: str,
    credential: TokenCredential,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    with tempfile.TemporaryDirectory(prefix="ask-hr-benchmark-") as temporary:
        dataset_path = Path(temporary) / "responses.jsonl"
        dataset_path.write_text(
            "".join(
                json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                for row in rows
            ),
            encoding="utf-8",
        )
        score_dataset(
            project_endpoint=project_endpoint,
            dataset_name="ask-hr-pipeline-benchmark",
            evaluation_name=args.label,
            settings=EvaluationSettings(
                args.judge_model_deployment,
                args.rubric_threshold,
                args.poll_interval,
                args.timeout,
            ),
            credential=credential,
            dataset_path=dataset_path,
            summary=summary,
            summary_path=args.output,
            case_ids={row["case_id"] for row in rows},
            request_timeout=args.request_timeout,
            agent_inputs=True,
        )


def main() -> None:
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()

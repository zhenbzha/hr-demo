"""Submit Foundry scoring runs and save their results and diagnostics."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import TestingCriterionAzureAIEvaluator
from azure.core.credentials import TokenCredential
from azure.identity import AzureCliCredential
from json_output import write_json
from openai import OpenAI
from openai.types.eval_create_params import DataSourceConfigCustom

from .agent import build_captured_agent_query, captured_response_items
from .foundry import (
    MIN_RUBRIC_DIMENSION_SCORE,
    EvaluationSettings,
    behavior_criterion,
    built_in_criteria,
    create_rubric_evaluator,
    dataset_schema,
    evaluation_summary,
    policy_answer_criteria,
    rubric_assessment,
    rubric_criterion,
    rubric_definition,
    rubric_fingerprint,
    start_run,
    task_adherence_criterion,
    wait_for_run,
)
from .references import policy_evaluation_query


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    write_json(path, summary)


def captured_dataset_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Build evaluation rows from a saved benchmark report.

    Parameters
    ----------
    summary : dict
        Report containing saved results and agent details. Model requests
        require their original system instructions and matching SHA-256.
        The report is updated with labels describing the scoring inputs.

    Returns
    -------
    list of dict
        Rows containing saved answers and conversation messages.
        Where a model request was not saved, messages contain the question.

    Raises
    ------
    ValueError
        Saved instructions or evidence are invalid or inconsistent.
    """
    results = summary["results"]
    metadata = summary.get("agent", {})
    instructions = metadata.get("generation_instructions")
    if any(result.get("generation_input") is not None for result in results) and (
        not isinstance(instructions, str)
        or hashlib.sha256(instructions.encode()).hexdigest()
        != metadata.get("generation_instructions_sha256")
    ):
        raise ValueError("Captured generation instructions differ from their hash")
    fields = dataset_schema()["item_schema"]["required"]
    rows = []
    for result in results:
        row = {field: result[field] for field in fields if field != "output_items"}
        row["output_items"] = captured_response_items(result)
        row["agent_query"] = build_captured_agent_query(result, instructions)
        rows.append(row)
    summary["evaluator_input_format"] = "foundry_text_content_parts"
    summary["evaluator_query_source"] = (
        "Verified generation instructions and actual model input when captured; "
        "user question only when no generation input was captured."
    )
    return rows


def check_evaluation_sdk(project_endpoint: str) -> None:
    """
    Check that the local SDK provides the Foundry evaluator catalog.

    Parameters
    ----------
    project_endpoint : str
        HTTPS Foundry project endpoint used to construct a local client.
        This check inspects the installed SDK only.

    Raises
    ------
    RuntimeError
        The installed SDK lacks the evaluator catalog.
    """
    with (
        AzureCliCredential() as credential,
        AIProjectClient(
            endpoint=project_endpoint, credential=credential, allow_preview=True
        ) as project,
    ):
        if not hasattr(project, "evaluators"):
            raise RuntimeError(
                "This environment's azure-ai-projects lacks the evaluator catalog. "
                "Use the project's evaluation environment (azure-ai-projects>=2.8.0), "
                "or pass --evaluation-python to the pipeline benchmark."
            )


def run_stage(
    *,
    client: OpenAI,
    name: str,
    criteria: list[TestingCriterionAzureAIEvaluator],
    settings: EvaluationSettings,
    summary: dict[str, Any],
    summary_path: Path,
    case_ids: set[str],
    label: str,
    inline_rows: list[dict[str, Any]],
    schema: DataSourceConfigCustom | None = None,
) -> None:
    """
    Submit one evaluation stage and save its progress and results.

    Valid low scores are included in the saved report for review.

    Parameters
    ----------
    client : OpenAI
        Authenticated Foundry OpenAI client.
    name : str
        Display name for the evaluation and its run.
    criteria : list of TestingCriterionAzureAIEvaluator
        Evaluator definitions and their dataset field mappings.
    settings : EvaluationSettings
        Judge configuration, polling interval, and timeout.
    summary : dict
        Mutable report with agent name/version and dataset version.
    summary_path : pathlib.Path
        Local JSON report rewritten as this stage progresses.
    case_ids : set of str
        IDs of all cases submitted for scoring.
    label : str
        Report key for this stage. ``expected_behavior`` enables
        validation against the configured HR behavior threshold.
    inline_rows : list of dict
        Answer rows to submit directly.
    schema : DataSourceConfigCustom or None, default=None
        Schema describing the submitted rows. Defaults to ``dataset_schema()``.

    Raises
    ------
    RuntimeError
        Results are incomplete or unscorable. Their diagnostics
        are saved before this error is raised.
    """
    evaluation, run = start_run(
        client,
        name=name,
        criteria=criteria,
        metadata={
            "agent": summary["agent"]["name"],
            "agent_version": summary["agent"]["version"],
            "dataset_version": summary["dataset_version"],
        },
        inline_rows=inline_rows,
        schema=schema,
    )
    summary[label] = {
        "evaluation_id": evaluation.id,
        "run_id": run.id,
        "status": run.status,
    }
    write_summary(summary_path, summary)
    run = wait_for_run(client, evaluation.id, run, settings)
    items = list(
        client.evals.runs.output_items.list(run_id=run.id, eval_id=evaluation.id)
    )
    summary[label] = evaluation_summary(
        evaluation=evaluation,
        run=run,
        items=items,
        expected_case_ids=case_ids,
        criterion_names={criterion["name"] for criterion in criteria},
        threshold=settings.pass_threshold if label == "expected_behavior" else None,
    )
    summary[label]["output_items"] = [
        json.loads(item.model_dump_json(exclude_none=True)) for item in items
    ]
    write_summary(summary_path, summary)
    if summary[label]["validation_errors"]:
        raise RuntimeError(
            f"{label} returned incomplete/unscorable results; see {summary_path}"
        )


def score_reference_rows(
    *,
    project_endpoint: str,
    evaluation_name: str,
    settings: EvaluationSettings,
    credential: TokenCredential,
    rows: list[dict[str, Any]],
    generation_case_ids: set[str],
    summary: dict[str, Any],
    summary_path: Path,
    request_timeout: float = 60,
) -> None:
    """
    Score saved answers using their recorded HR evaluator version.

    Submit the saved answers to the built-in quality evaluators and the
    previously registered HR evaluator. Built-in scoring includes comparison
    with reviewed reference answers. Scoring creates remote Foundry runs.

    Parameters
    ----------
    project_endpoint : str
        HTTPS Foundry project endpoint for scoring.
    evaluation_name : str
        Display-name prefix for the replay's scoring stages.
    settings : EvaluationSettings
        Judge deployment, HR threshold, and polling limits.
    credential : TokenCredential
        Entra credential with project/model/evaluation access.
    rows : list of dict
        Saved answers with reviewed reference text (``ground_truth``)
        and recorded conversation messages (``agent_query``).
    generation_case_ids : set of str
        IDs of rows with recorded generation requests.
        These rows also receive Task Adherence scoring.
    summary : dict
        Report to update, including agent and dataset details and the
        ``behavior_evaluator`` name/version recorded with the saved answers.
    summary_path : pathlib.Path
        Local JSON report updated with run IDs, scores, and errors.
    request_timeout : float, default=60
        Maximum seconds for each HTTP request, capped at 60.
        Polling is controlled by ``settings.timeout``.
    """
    summary["scoring_sdk_versions"] = {
        name: version(name) for name in ("azure-ai-projects", "openai")
    }
    criteria = built_in_criteria(settings.model_deployment, references=True)
    task_criteria = [task_adherence_criterion(settings.model_deployment)]
    with (
        AIProjectClient(
            endpoint=project_endpoint,
            credential=credential,
            allow_preview=True,
            connection_timeout=30,
            read_timeout=120,
        ) as project,
        project.get_openai_client(
            timeout=min(request_timeout, 60), max_retries=0
        ) as client,
    ):
        required = {
            criterion["evaluator_name"] for criterion in criteria + task_criteria
        }
        catalog = {
            evaluator.name: evaluator
            for evaluator in project.evaluators.list(type="builtin", limit=20)
            if evaluator.name in required
        }
        if missing := required - catalog.keys():
            raise RuntimeError(
                f"Required Foundry evaluators are unavailable: {sorted(missing)}"
            )
        summary["evaluator_catalog"] = {
            name: {"name": evaluator.name, "version": evaluator.version}
            for name, evaluator in catalog.items()
        }
        for criterion in criteria + task_criteria:
            criterion["evaluator_version"] = catalog[
                criterion["evaluator_name"]
            ].version
        selected = summary["behavior_evaluator"]
        evaluator = project.evaluators.get_version(
            name=selected["name"], version=selected["version"]
        )
        prompt = evaluator.definition.get("prompt_text")
        if (
            not isinstance(prompt, str)
            or hashlib.sha256(prompt.encode()).hexdigest() != summary["rubric_sha256"]
        ):
            raise ValueError(
                "The pinned HR evaluator does not match the recorded rubric"
            )
        behavior = [behavior_criterion(evaluator, settings)]
        summary["criteria"] = {
            "built_in": criteria,
            "expected_behavior": behavior,
            "task_adherence": task_criteria,
        }
        write_summary(summary_path, summary)
        schema = dataset_schema(references=True)
        stages = [
            ("built_in", criteria, rows),
            ("expected_behavior", behavior, rows),
        ]
        generation_rows = [row for row in rows if row["case_id"] in generation_case_ids]
        if generation_rows:
            stages.append(("task_adherence", task_criteria, generation_rows))
        for label, stage_criteria, stage_rows in stages:
            print(f"Scoring {label}: {len(stage_rows)} captured answers", flush=True)
            run_stage(
                client=client,
                name=evaluation_name + " - " + label,
                criteria=stage_criteria,
                settings=settings,
                summary=summary,
                summary_path=summary_path,
                case_ids={row["case_id"] for row in stage_rows},
                label=label,
                inline_rows=stage_rows,
                schema=schema,
            )


def score_dataset(
    *,
    project_endpoint: str,
    dataset_name: str,
    evaluation_name: str,
    settings: EvaluationSettings,
    credential: TokenCredential,
    dataset_path: Path,
    summary: dict[str, Any],
    summary_path: Path,
    case_ids: set[str],
    request_timeout: float = 60,
    policy_case_ids: set[str] | None = None,
) -> None:
    """
    Submit collected answers for built-in and HR-specific Foundry scoring.

    Register or reuse a dataset in Foundry for lineage, submit its local rows
    inline to scoring runs, create an HR evaluator version, and update the
    report. Uploaded data is retained under Foundry's retention settings.
    Deleting the local JSONL file leaves the uploaded data in Foundry.

    Parameters
    ----------
    project_endpoint : str
        HTTPS Foundry project endpoint.
    dataset_name : str
        Name used to register or find the dataset in Foundry.
    evaluation_name : str
        Display-name prefix for the two scoring stages.
    settings : EvaluationSettings
        Judge deployment, native HR rubric threshold, and polling limits.
    credential : TokenCredential
        Entra credential with dataset, evaluation, and model access.
    dataset_path : pathlib.Path
        Local UTF-8 JSONL file containing prepared answer rows.
    summary : dict
        Mutable report with ``dataset_version`` and agent name/version.
        An existing ``dataset_id`` requests verification/reuse of that
        registered version; otherwise a dataset is uploaded.
    summary_path : pathlib.Path
        Local report rewritten with dataset/run IDs and results.
    case_ids : set of str
        IDs of all cases submitted for scoring.
    request_timeout : float, default=60
        OpenAI-client request timeout in seconds, capped at 60.
    policy_case_ids : set of str or None, default=None
        Enable the PDF-reference profile for this predeclared subset of cases.
        All cases receive HR behavior and source-faithfulness scoring; only
        this subset receives Response Completeness. None selects the benchmark
        diagnostic profile, which requires recorded ``agent_query``
        conversations in every row. The PDF-reference profile also uses these
        conversations when supplied; if any row supplies one, all must do so.
    """
    definition = rubric_definition(settings.pass_threshold)
    fingerprint = rubric_fingerprint(settings.pass_threshold)
    if summary.get("rubric_sha256", fingerprint) != fingerprint:
        raise ValueError("Scoring rubric differs from the declared native definition")
    if summary.get(
        "rubric_minimum_dimension_score", MIN_RUBRIC_DIMENSION_SCORE
    ) != MIN_RUBRIC_DIMENSION_SCORE:
        raise ValueError("Scoring dimension floor differs from the declared demo policy")
    summary["rubric_sha256"] = fingerprint
    summary["rubric_threshold"] = settings.pass_threshold
    summary["rubric_minimum_dimension_score"] = MIN_RUBRIC_DIMENSION_SCORE
    summary["rubric_definition"] = definition
    summary["hr_score_format"] = "native_weighted_rubric_0_to_1"
    dataset_bytes = dataset_path.read_bytes()
    inline_rows = [
        json.loads(line) for line in dataset_bytes.decode("utf-8").splitlines()
    ]
    agent_inputs = policy_case_ids is None or any(
        isinstance(row, dict) and "agent_query" in row for row in inline_rows
    )
    if agent_inputs and (not inline_rows or any(
        not isinstance(row, dict)
        or not isinstance(row.get("agent_query"), list)
        or not row["agent_query"]
        for row in inline_rows
    )):
        raise ValueError("Captured-input scoring requires nonempty agent_query in every row")
    summary["scoring_inputs"] = {
        "source": (
            "inline_pdf_reference_dataset"
            if policy_case_ids is not None
            else "inline_verified_captured_agent_query"
        ),
        "rubric_query_field": (
            "agent_query" if agent_inputs
            else "policy_query" if policy_case_ids is not None
            else "query"
        ),
        "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "registered_dataset_usage": "capture_lineage_only",
    }
    if policy_case_ids is not None:
        identifiers = [row["case_id"] for row in inline_rows]
        if (
            len(identifiers) != len(case_ids)
            or set(identifiers) != case_ids
            or not policy_case_ids <= case_ids
        ):
            raise ValueError("Policy scoring must retain every declared case exactly once")
        for row in inline_rows:
            if row.get("policy_query") != policy_evaluation_query(row):
                raise ValueError("Policy query must preserve the captured question and context")
            truth = row.get("ground_truth")
            if not isinstance(truth, str) or bool(truth.strip()) != (
                row["case_id"] in policy_case_ids
            ):
                raise ValueError("Reference answers do not match the declared policy cases")
        summary["evaluation_profile"] = "pdf_reference_native_demo_v4"
        summary["evaluation_plan"] = {
            "behavior_case_ids": sorted(case_ids),
            "policy_answer_case_ids": sorted(policy_case_ids),
            "clarification_or_no_policy_case_ids": sorted(case_ids - policy_case_ids),
            "gating_policy_metrics": ["response_completeness"],
            "source_faithfulness": {
                "evaluator": "hr_rubric",
                "dimension": "policy_scope_and_evidence",
                "minimum_score": MIN_RUBRIC_DIMENSION_SCORE,
            },
            "retired_evaluators": [{
                "name": "builtin.groundedness",
                "replacement": "hr_rubric.policy_scope_and_evidence",
                "reason": (
                    "Generic judge conflicts with the demo contract on routine "
                    "boilerplate and translated boundaries. The replacement is "
                    "calibrated on source-backed positive and negative controls."
                ),
            }],
            "scope_basis": (
                "Selected from test expectations before grading, not from answers or scores."
            ),
            "scored_contract": (
                "Fixed native rubric with a dimension-score floor, deterministic "
                "citation checks, and PDF-reference completeness. The rubric's "
                "policy_scope_and_evidence dimension gates factual support. Generic "
                "Groundedness is retired from this profile, not silently passed. No per-case "
                "free-text assertion evaluator or internal execution verification."
            ),
        }
    summary["scoring_sdk_versions"] = {
        name: version(name) for name in ("azure-ai-projects", "openai")
    }
    with (
        AIProjectClient(
            endpoint=project_endpoint,
            credential=credential,
            allow_preview=True,
            connection_timeout=30,
            read_timeout=120,
        ) as project,
        project.get_openai_client(
            timeout=min(request_timeout, 60), max_retries=0
        ) as client,
    ):
        if not hasattr(project, "evaluators"):
            raise RuntimeError(
                "The scoring SDK has no evaluator catalog; run --check-sdk first"
            )
        if dataset_id := summary.get("dataset_id"):
            dataset = project.datasets.get(
                name=dataset_name,
                version=summary["dataset_version"],
            )
            if dataset.id != dataset_id:
                raise ValueError(
                    "The selected dataset version differs from the captured report"
                )
        else:
            dataset = project.datasets.upload_file(
                name=dataset_name,
                version=summary["dataset_version"],
                file_path=str(dataset_path),
            )
        if not dataset.id:
            raise RuntimeError("Foundry returned no dataset identifier")
        summary["dataset_id"] = dataset.id
        schema = dataset_schema(
            agent_inputs=agent_inputs, policy_answers=policy_case_ids is not None
        )
        if policy_case_ids is None:
            run_stage(
                client=client,
                name=evaluation_name + " - built-in",
                criteria=built_in_criteria(
                    settings.model_deployment, agent_inputs=agent_inputs
                ),
                settings=settings,
                summary=summary,
                summary_path=summary_path,
                case_ids=case_ids,
                label="built_in",
                inline_rows=inline_rows,
                schema=schema,
            )
        evaluator = create_rubric_evaluator(project, settings)
        summary["behavior_evaluator"] = {
            "name": evaluator.name,
            "version": evaluator.version,
            "type": "rubric",
        }
        run_stage(
            client=client,
            name=evaluation_name + " - expected behavior",
            criteria=[rubric_criterion(
                evaluator, settings, agent_inputs=agent_inputs,
                policy_answers=policy_case_ids is not None,
            )],
            settings=settings,
            summary=summary,
            summary_path=summary_path,
            case_ids=case_ids,
            label="expected_behavior",
            inline_rows=inline_rows,
            schema=schema,
        )
        summary["rubric_assessment"] = rubric_assessment(summary["expected_behavior"])
        write_summary(summary_path, summary)
        if policy_case_ids is not None:
            if policy_case_ids:
                run_stage(
                    client=client,
                    name=evaluation_name + " - policy answers",
                    criteria=policy_answer_criteria(settings.model_deployment),
                    settings=settings,
                    summary=summary,
                    summary_path=summary_path,
                    case_ids=policy_case_ids,
                    label="policy_answers",
                    inline_rows=[
                        row for row in inline_rows if row["case_id"] in policy_case_ids
                    ],
                    schema=schema,
                )
            summary["assessment"] = policy_assessment(summary)
            write_summary(summary_path, summary)


def require_demo_pass(summary: dict[str, Any]) -> None:
    """
    Require the completed report's native demo checks to pass.

    Parameters
    ----------
    summary : dict
        Completed report with ``case_checks`` and ``rubric_assessment``.
        Hosted reports also contain a combined ``assessment`` covering the
        applicable policy-answer checks; that assessment must pass too.
        Call only when the caller's optional pass gate is enabled.

    Raises
    ------
    RuntimeError
        Citation checks or an application assessment failed. Raw Foundry
        results and application assessments are not modified.
    """
    if any(case["status"] != "passed" for case in summary["case_checks"]):
        raise RuntimeError(
            "Some answer/citation requirements failed; see the evaluation results"
        )
    if summary["rubric_assessment"]["failed"] or (
        "assessment" in summary and summary["assessment"]["failed"]
    ):
        raise RuntimeError("Some native demo checks failed; see the application assessment")


def policy_assessment(summary: dict[str, Any]) -> dict[str, Any]:
    """
    Combine the limited native demo checks, not arbitrary HR assertions.

    Parameters
    ----------
    summary : dict
        Completed PDF-reference stages, application ``rubric_assessment``,
        predeclared case plan, and citation checks. Scoring-stage coverage
        must already be validated.

    Returns
    -------
    dict
        Per-case pass/fail and failed requirements. Clarification/no-policy
        cases must pass their HR and citation checks; they never pass by omission.
        Source faithfulness is gated by the calibrated native rubric and its
        minimum score per dimension. Generic Groundedness is not part of this
        profile; legacy/extra metrics are rejected rather than ignored.

    Raises
    ------
    ValueError
        Citation or scoring results do not cover the declared case sets.
    """
    plan = summary["evaluation_plan"]
    expected_ids = set(plan["behavior_case_ids"])
    policy_ids = set(plan["policy_answer_case_ids"])
    checks = summary["case_checks"]
    behavior = summary["rubric_assessment"]["cases"]
    policy = summary.get("policy_answers", {}).get("cases", [])
    for items, expected in (
        (checks, expected_ids), (behavior, expected_ids), (policy, policy_ids),
    ):
        identifiers = [item["case_id"] for item in items]
        if len(identifiers) != len(expected) or set(identifiers) != expected:
            raise ValueError("HR assessment has missing or duplicate case results")
    failures = {
        item["case_id"]: [] if item["status"] == "passed" else ["citation_checks"]
        for item in checks
    }
    for case in behavior:
        failures[case["case_id"]].extend(case["failed_requirements"])
    for case in policy:
        metrics = {result["name"]: result for result in case["results"]}
        if (
            len(case["results"]) != 1
            or set(metrics) != {"response_completeness"}
            or any(type(result.get("passed")) is not bool for result in metrics.values())
        ):
            raise ValueError("HR assessment has missing, duplicate, or invalid policy metrics")
        if not metrics["response_completeness"]["passed"]:
            failures[case["case_id"]].append("response_completeness")
    results = [
        {
            "case_id": identifier,
            "case_type": (
                "policy_answer" if identifier in policy_ids else "clarification_or_no_policy"
            ),
            "passed": not failed,
            "failed_requirements": failed,
        }
        for identifier, failed in failures.items()
    ]
    return {
        "profile": "pdf_reference_native_demo_v4",
        "definition": (
            "Limited native demo acceptance: fixed rubric plus per-dimension floor "
            "and citation checks; policy answers also require Response Completeness. "
            "The calibrated policy_scope_and_evidence dimension gates source "
            "faithfulness; generic Groundedness is retired from this profile. "
            "Not exhaustive HR verification or evaluation of "
            "per-case free-text assertions."
        ),
        "passed": sum(case["passed"] for case in results),
        "failed": sum(not case["passed"] for case in results),
        "cases": results,
    }

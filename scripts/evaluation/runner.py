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
from openai import OpenAI
from openai.types.eval_create_params import DataSourceConfigCustom

from .agent import build_captured_agent_query
from .foundry import (
    BEHAVIOR_PROMPT,
    EvaluationSettings,
    behavior_criterion,
    built_in_criteria,
    create_behavior_evaluator,
    dataset_schema,
    evaluation_summary,
    start_run,
    task_adherence_criterion,
    wait_for_run,
)


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def captured_dataset_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Prepare local capture rows using verified instructions and actual inputs."""
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
        row = {field: result[field] for field in fields}
        row["agent_query"] = build_captured_agent_query(result, instructions)
        rows.append(row)
    summary["evaluator_input_format"] = "foundry_text_content_parts"
    summary["evaluator_query_source"] = (
        "Verified generation instructions and actual model input when captured; "
        "user question only when no generation input was captured."
    )
    return rows


def check_evaluation_sdk(project_endpoint: str) -> None:
    """Check that the local SDK provides the Foundry evaluator catalog.

    Raise RuntimeError if it is missing. This check makes no Azure requests.
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
    inline_rows: list[dict[str, Any]] | None = None,
    schema: DataSourceConfigCustom | None = None,
) -> None:
    evaluation, run = start_run(
        client,
        name=name,
        dataset_id=summary["dataset_id"] if inline_rows is None else None,
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
        threshold=settings.behavior_threshold if label == "expected_behavior" else None,
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
    """Score an immutable replay with documented inputs and a pinned HR judge."""
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
        if evaluator.definition.get("prompt_text") != BEHAVIOR_PROMPT:
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
    agent_inputs: bool = False,
) -> None:
    inline_rows = None
    if agent_inputs:
        dataset_bytes = dataset_path.read_bytes()
        inline_rows = [
            json.loads(line) for line in dataset_bytes.decode("utf-8").splitlines()
        ]
        if not inline_rows or any(
            not isinstance(row, dict) or "agent_query" not in row for row in inline_rows
        ):
            raise ValueError("Captured-input scoring requires agent_query in every row")
        summary["scoring_inputs"] = {
            "source": "inline_verified_captured_agent_query",
            "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
            "registered_dataset_usage": "capture_lineage_only",
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
            schema=dataset_schema(agent_inputs=agent_inputs),
        )
        evaluator = create_behavior_evaluator(project)
        summary["behavior_evaluator"] = {
            "name": evaluator.name,
            "version": evaluator.version,
        }
        run_stage(
            client=client,
            name=evaluation_name + " - expected behavior",
            criteria=[behavior_criterion(evaluator, settings)],
            settings=settings,
            summary=summary,
            summary_path=summary_path,
            case_ids=case_ids,
            label="expected_behavior",
            inline_rows=inline_rows,
            schema=dataset_schema(agent_inputs=agent_inputs),
        )

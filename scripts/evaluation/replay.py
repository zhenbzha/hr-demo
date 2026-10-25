"""Prepare source-reviewed, immutable answer replays and judge comparisons."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import build_captured_agent_query
from .cases import expected_behavior
from .foundry import BEHAVIOR_PROMPT, dataset_schema

REPLAY_METRICS = {
    "relevance",
    "groundedness",
    "intent_resolution",
    "coherence",
    "response_completeness",
    "behavioral_adherence",
    "task_adherence",
}


@dataclass
class ReferenceReplay:
    rows: list[dict[str, Any]]
    generation_case_ids: set[str]
    expectations: dict[str, dict[str, bool]]
    provenance: dict[str, Any]


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _verified_file(root: Path, item: dict[str, Any], label: str) -> Path:
    path = (root / _text(item.get("path"), label + " path")).resolve()
    expected = _text(item.get("sha256"), label + " SHA-256")
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"{label} content differs from its recorded SHA-256: {path}")
    return path


def _verify_sources(root: Path, sources: list[dict[str, Any]]) -> set[str]:
    identifiers: set[str] = set()
    for source in sources:
        identifier = _text(source.get("id"), "Source ID")
        if identifier in identifiers:
            raise ValueError(f"Duplicate reference source: {identifier}")
        identifiers.add(identifier)
        _verified_file(root, source, identifier)
        analysis_path = _verified_file(
            root, source["analysis"], identifier + " analysis"
        )
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        if analysis.get("stringIndexType") != "unicodeCodePoint":
            raise ValueError(
                f"{identifier}: reference verification requires Unicode code-point spans"
            )
        pages = {
            page["pageNumber"]: "".join(
                analysis["content"][span["offset"] : span["offset"] + span["length"]]
                for span in page["spans"]
            )
            for page in analysis["pages"]
        }
        for passage in source["passages"]:
            quote = _text(passage.get("quote"), identifier + " source quote")
            if quote not in pages[passage["page"]]:
                raise ValueError(
                    f"{identifier}: reference quote is absent from its stated PDF page"
                )
    return identifiers


def load_reference_replay(
    path: Path,
    *,
    cases: list[dict[str, Any]],
    cases_sha256: str,
    project_endpoint: str,
    model_deployment: str,
    behavior_threshold: float,
    require_review: bool = True,
) -> ReferenceReplay:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported reference-replay schema version")
    review = manifest.get("review", {})
    if review.get("status") not in {
        "pending",
        "approved",
        "guidance_confirmed",
    } or not review.get("basis"):
        raise ValueError("Reference review status and basis are required")
    if review["status"] == "guidance_confirmed":
        _text(review.get("guidance"), "User-confirmed reference guidance")
        if review.get("label_author") != "assistant":
            raise ValueError(
                "Guidance-confirmed references must identify assistant-derived labels"
            )
    if require_review and review["status"] == "pending":
        raise ValueError(
            "References need human review or user-confirmed labeling guidance before scoring"
        )
    root = path.resolve().parent
    source_ids = _verify_sources(root, manifest["sources"])
    case_map = {case["id"]: case for case in cases}
    captures: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    expected_metadata = {
        "cases_sha256": cases_sha256,
        "project_endpoint": project_endpoint,
        "judge_deployment": model_deployment,
        "behavior_threshold": behavior_threshold,
        "rubric_sha256": hashlib.sha256(BEHAVIOR_PROMPT.encode()).hexdigest(),
        "response_collection": "completed",
        "behavior_evaluator": manifest["behavior_evaluator"],
    }
    for capture in manifest["captures"]:
        identifier = _text(capture.get("id"), "Capture ID")
        if identifier in captures:
            raise ValueError(f"Duplicate capture: {identifier}")
        report_path = _verified_file(root, capture, identifier)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if any(report.get(key) != value for key, value in expected_metadata.items()):
            raise ValueError(
                f"{identifier}: captured cases, rubric or scoring configuration differ"
            )
        instructions = _text(
            capture.get("generation_instructions"), "Generation instructions"
        )
        if (
            hashlib.sha256(instructions.encode()).hexdigest()
            != report["agent"]["generation_instructions_sha256"]
        ):
            raise ValueError(
                f"{identifier}: supplied instructions differ from the captured prompt"
            )
        results = {row["case_id"]: row for row in report["results"]}
        if len(results) != len(report["results"]):
            raise ValueError(f"{identifier}: captured case IDs are not unique")
        captures[identifier] = (capture, report)

    rows: list[dict[str, Any]] = []
    generations: set[str] = set()
    expectations: dict[str, dict[str, bool]] = {}
    samples = []
    reference_ids: set[str] = set()
    fields = dataset_schema()["item_schema"]["required"]
    for reference in manifest["references"]:
        original_id = _text(reference.get("case_id"), "Reference case ID")
        if original_id in reference_ids:
            raise ValueError(f"Duplicate reference case: {original_id}")
        reference_ids.add(original_id)
        case = case_map[original_id]
        if reference["query"] != case["query"]:
            raise ValueError(
                f"{original_id}: reference question differs from the recorded case"
            )
        truth = _text(reference.get("ground_truth"), original_id + " ground truth")
        basis = reference.get("basis")
        if basis not in {"policy", "application_contract"}:
            raise ValueError(f"{original_id}: unknown reference basis")
        references = reference["source_ids"]
        if (basis == "policy" and not references) or set(references) - source_ids:
            raise ValueError(
                f"{original_id}: policy reference sources are missing or unknown"
            )
        for sample in reference["samples"]:
            capture_id = sample["capture"]
            capture, report = captures[capture_id]
            original = next(
                row for row in report["results"] if row["case_id"] == original_id
            )
            if original["query"] != case["query"] or original[
                "expected_behavior"
            ] != expected_behavior(case):
                raise ValueError(
                    f"{capture_id}/{original_id}: captured question or rubric differs"
                )
            identifier = capture_id + ":" + original_id
            if identifier in expectations:
                raise ValueError(f"Duplicate replay sample: {identifier}")
            expected = sample["expected"]
            if (
                not expected
                or set(expected) - REPLAY_METRICS
                or any(type(value) is not bool for value in expected.values())
            ):
                raise ValueError(
                    f"{identifier}: expected results must name metrics with boolean labels"
                )
            row = {field: original[field] for field in fields}
            row["case_id"] = identifier
            row["ground_truth"] = truth
            generation_input = original.get("generation_input")
            row["agent_query"] = build_captured_agent_query(
                original, capture["generation_instructions"]
            )
            if generation_input is not None:
                generations.add(identifier)
            else:
                if "task_adherence" in expected:
                    raise ValueError(
                        f"{identifier}: no captured generation turn for Task Adherence"
                    )
            rows.append(row)
            expectations[identifier] = expected
            samples.append(
                {
                    "case_id": identifier,
                    "source_case_id": original_id,
                    "capture": capture_id,
                    "response_id": original["response_id"],
                    "response_sha256": hashlib.sha256(
                        original["response"].encode()
                    ).hexdigest(),
                    "context_sha256": hashlib.sha256(
                        original["context"].encode()
                    ).hexdigest(),
                    "generation_instructions_sha256": report["agent"][
                        "generation_instructions_sha256"
                    ]
                    if generation_input is not None
                    else None,
                    "expected": expected,
                    "label_reason": _text(
                        sample.get("reason"), identifier + " label reason"
                    ),
                    "previous_scores": {
                        stage: {
                            "evaluation_id": report[stage]["evaluation_id"],
                            "run_id": report[stage]["run_id"],
                            "case": next(
                                item
                                for item in report[stage]["cases"]
                                if item["case_id"] == original_id
                            ),
                        }
                        for stage in ("built_in", "expected_behavior")
                    },
                }
            )
    if not rows:
        raise ValueError("No reference replay samples were selected")
    return ReferenceReplay(
        rows=rows,
        generation_case_ids=generations,
        expectations=expectations,
        provenance={
            "manifest": manifest,
            "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "samples": samples,
            "task_adherence_not_evaluated": [
                {
                    "case_id": row["case_id"],
                    "reason": (
                        "Generation did not run. The saved employee-facing message is not "
                        "the classifier's structured model response; do not fabricate a turn "
                        "or evaluate it against the generation prompt."
                    ),
                }
                for row in rows
                if row["case_id"] not in generations
            ],
        },
    )


def compare_reference_judgments(
    summary: dict[str, Any], expectations: dict[str, dict[str, bool]]
) -> dict[str, Any]:
    observed: dict[tuple[str, str], dict[str, Any]] = {}
    for stage in ("built_in", "expected_behavior", "task_adherence"):
        for case in summary.get(stage, {}).get("cases", []):
            for result in case["results"]:
                key = (case["case_id"], result["name"])
                if key in observed:
                    raise ValueError(f"Duplicate judge result in replay: {key}")
                observed[key] = result
    comparisons = []
    counts: dict[str, dict[str, int]] = {}
    for case_id, metrics in expectations.items():
        for metric, expected in metrics.items():
            result = observed.get((case_id, metric))
            if result is None or type(result["passed"]) is not bool:
                raise ValueError(f"{case_id}/{metric}: reviewed result is unscored")
            passed = result["passed"]
            outcome = (
                "agreement"
                if passed == expected
                else "false_pass"
                if passed
                else "false_fail"
            )
            totals = counts.setdefault(
                metric, {"agreement": 0, "false_pass": 0, "false_fail": 0}
            )
            totals[outcome] += 1
            comparisons.append(
                {
                    "case_id": case_id,
                    "metric": metric,
                    "expected_passed": expected,
                    "actual_passed": passed,
                    "score": result["score"],
                    "outcome": outcome,
                    "reason": result["reason"],
                }
            )
    return {
        "scope": (
            "Known-answer diagnostic against recorded reference labels, not independent "
            "or held-out agent validation. Reference review identifies label provenance."
        ),
        "reference_review": summary.get("reference_review"),
        "passed": all(item["outcome"] == "agreement" for item in comparisons),
        "metrics": counts,
        "comparisons": comparisons,
    }

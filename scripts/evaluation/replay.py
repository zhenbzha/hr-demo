"""Load saved answers and reviewed references for evaluator checks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import build_captured_agent_query, captured_response_items
from .cases import expected_behavior
from .foundry import dataset_schema

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
    """
    Saved answers, reference answers, and expected evaluation results.

    Attributes
    ----------
    rows : list of dict
        Saved answers and policy text, with reviewed reference answers.
    generation_case_ids : set of str
        IDs of samples with saved requests to the answer-generation model.
    expectations : dict
        Expected pass/fail results grouped by sample ID and metric.
        Used only for local comparison with the returned scores.
    provenance : dict
        Source files, hashes, review status, and details of which
        samples have saved generation requests.
    """

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


def _analysis_page_text(analysis: dict[str, Any]) -> dict[int, str]:
    """Collect each page's text using its recorded character offsets."""
    pages = {}
    for page in analysis["pages"]:
        text_parts = []
        for span in page["spans"]:
            start = span["offset"]
            end = start + span["length"]
            text_parts.append(analysis["content"][start:end])
        pages[page["pageNumber"]] = "".join(text_parts)
    return pages


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
        pages = _analysis_page_text(analysis)
        for passage in source["passages"]:
            quote = _text(passage.get("quote"), identifier + " source quote")
            if quote not in pages[passage["page"]]:
                raise ValueError(
                    f"{identifier}: reference quote is absent from its stated PDF page"
                )
    return identifiers


def _previous_case_scores(
    report: dict[str, Any], case_id: str,
) -> dict[str, dict[str, Any]]:
    """Collect a saved case's scores and run IDs from both scoring stages."""
    scores = {}
    for stage_name in ("built_in", "expected_behavior"):
        stage = report[stage_name]
        case_scores = next(
            case for case in stage["cases"] if case["case_id"] == case_id
        )
        scores[stage_name] = {
            "evaluation_id": stage["evaluation_id"],
            "run_id": stage["run_id"],
            "case": case_scores,
        }
    return scores


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
    """
    Load saved answers and verify them against reviewed source material.

    Read the reference file, PDFs, saved analysis, and captured responses.
    Verify file hashes, quoted passages, model requests, cases, and scoring
    settings before building evaluation rows.

    Parameters
    ----------
    path : pathlib.Path
        JSON file describing the reference answers and their source files.
        Paths to PDFs, saved analysis, and captured responses are relative
        to this file's directory.
    cases : list of dict
        Original test questions and expectations returned by ``load_cases``.
    cases_sha256 : str
        SHA-256 hash of the case file used to collect the saved responses.
    project_endpoint : str
        Foundry project used for the saved evaluation.
    model_deployment : str
        Judge deployment used for the saved evaluation.
    behavior_threshold : float
        HR behavior score threshold used for the saved evaluation.
    require_review : bool, default=True
        Require completed review of the reference answers. Set to False
        only when preparing inputs for review.

    Returns
    -------
    ReferenceReplay
        Rows to score, IDs eligible for instruction-adherence scoring,
        expected pass/fail labels, and source-verification details.
    """
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
        "response_collection": "completed",
        "behavior_evaluator": manifest["behavior_evaluator"],
    }
    rubric_sha256 = None
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
        recorded_rubric = _text(report.get("rubric_sha256"), "Captured rubric SHA-256")
        if rubric_sha256 is not None and recorded_rubric != rubric_sha256:
            raise ValueError("Captured rubric hashes differ")
        rubric_sha256 = recorded_rubric
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
            row = {field: original[field] for field in fields if field != "output_items"}
            row["output_items"] = captured_response_items(original)
            row["case_id"] = identifier
            row["ground_truth"] = truth
            generation_input = original.get("generation_input")
            row["agent_query"] = build_captured_agent_query(
                original, capture["generation_instructions"]
            )
            if "response_language" in original:
                row["response_language"] = original["response_language"]
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
                    "previous_scores": _previous_case_scores(report, original_id),
                }
            )
    if not rows:
        raise ValueError("No reference replay samples were selected")
    return ReferenceReplay(
        rows=rows,
        generation_case_ids=generations,
        expectations=expectations,
        provenance={
            "rubric_sha256": rubric_sha256,
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
    """
    Compare evaluator pass/fail results with the reviewed expectations.

    Parameters
    ----------
    summary : dict
        Replay report containing scored ``built_in``,
        ``expected_behavior``, and optional ``task_adherence`` stages.
    expectations : dict
        Expected pass/fail results from ``ReferenceReplay``, grouped by
        sample ID and metric. Only labeled metrics are compared.

    Returns
    -------
    dict
        Agreement, false-pass, and false-fail counts for each metric,
        plus individual comparisons. ``passed`` is True when all labeled
        results agree.

    Raises
    ------
    ValueError
        A required score is missing or unscored, or a result is duplicated.
    """
    observed: dict[tuple[str, str], dict[str, Any]] = {}
    for stage in ("built_in", "english_coherence", "expected_behavior", "task_adherence"):
        for case in summary.get(stage, {}).get("cases", []):
            for result in case["results"]:
                key = (case["case_id"], result["name"])
                if key in observed:
                    raise ValueError(f"Duplicate judge result in replay: {key}")
                observed[key] = result
    comparisons = []
    skipped = []
    coherence_not_applicable = {
        item["case_id"]
        for item in summary.get("coherence_applicability", {}).get("not_applicable", [])
    }
    counts: dict[str, dict[str, int]] = {}
    for case_id, metrics in expectations.items():
        for metric, expected in metrics.items():
            if metric == "coherence" and case_id in coherence_not_applicable:
                skipped.append({"case_id": case_id, "metric": metric, "status": "not_applicable"})
                continue
            result = observed.get((case_id, metric))
            if result is None or type(result["passed"]) is not bool:
                raise ValueError(f"{case_id}/{metric}: reviewed result is unscored")
            passed = result["passed"]
            if passed == expected:
                outcome = "agreement"
            elif passed:
                outcome = "false_pass"
            else:
                outcome = "false_fail"
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
        "passed": bool(comparisons) and all(
            item["outcome"] == "agreement" for item in comparisons
        ),
        "not_applicable": skipped,
        "metrics": counts,
        "comparisons": comparisons,
    }

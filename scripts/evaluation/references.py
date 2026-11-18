"""Load PDF-backed answer references without consulting generated answers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cases import expected_behavior

REFERENCE_INPUT_CONTRACT = "pdf_source_answer_text_v1"


@dataclass(frozen=True)
class PolicyReferences:
    """
    Source-checked answer references and their evaluation scope.

    Attributes
    ----------
    cases : dict
        All test cases, keyed by case ID, including clarification/no-policy cases.
    answers : dict
        Expected answer text for policy-answer cases only.
    provenance : dict
        Reference-file hash, source PDF hashes, passages, and review declaration.
        Assistant source checking is not independent human or HR approval.
    """

    cases: dict[str, dict[str, Any]]
    answers: dict[str, str]
    provenance: dict[str, Any]


def load_policy_references(
    path: Path, cases: list[dict[str, Any]]
) -> PolicyReferences:
    """
    Verify reference coverage, questions, source hashes, and source citations.

    Parameters
    ----------
    path : pathlib.Path
        JSON reference file. PDF paths are relative to its directory.
    cases : list of dict
        Validated test cases. Cases expecting zero citations exercise
        clarification/no-policy behavior; all other cases require references.

    Returns
    -------
    PolicyReferences
        Verified references and their complete provenance.

    Raises
    ------
    ValueError
        References are missing, duplicated, stale, or inconsistent with the
        questions, source PDFs, or declared policy requirements.
    """
    content = path.read_bytes()
    manifest = json.loads(content)
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported policy-reference schema version")
    if "interpretations" in manifest:
        raise ValueError(
            "Policy references must use PDF facts, not evaluation-only interpretations"
        )
    review = manifest.get("review", {})
    if review.get("status") not in {
        "assistant_source_checked", "human_reviewed",
    } or not review.get("basis"):
        raise ValueError("Policy references require an explicit review declaration")
    sources = {}
    for source in manifest["sources"]:
        identifier = source["document_id"]
        pdf = (path.parent / source["path"]).resolve()
        if identifier in sources:
            raise ValueError(f"Duplicate reference source: {identifier}")
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Reference PDF changed; review its answers: {pdf}")
        sources[identifier] = source

    case_map = {case["id"]: case for case in cases}
    policy_ids = {
        identifier for identifier, case in case_map.items()
        if case.get("expectedCitationCount") != 0
    }
    answers = {}
    for reference in manifest["references"]:
        identifier = reference["case_id"]
        if identifier not in policy_ids or identifier in answers:
            raise ValueError(f"Unexpected or duplicate policy reference: {identifier}")
        case = case_map[identifier]
        if reference["query"] != case["query"]:
            raise ValueError(f"{identifier}: reference question changed")
        truth = reference["ground_truth"]
        if not isinstance(truth, str) or not truth.strip():
            raise ValueError(f"{identifier}: reference answer must be nonempty text")
        passages = reference["passages"]
        if not passages or any(
            passage["document_id"] not in sources
            or type(passage["page"]) is not int
            or passage["page"] < 1
            or not isinstance(passage["quote"], str)
            or not passage["quote"].strip()
            for passage in passages
        ):
            raise ValueError(f"{identifier}: invalid reference source passages")
        required_sources = {
            document_id
            for field in (
                "expectedDocumentIds",
                "expectedPrimaryDocumentIds",
                "expectedSupportingDocumentIds",
            )
            for document_id in case.get(field, [])
        }
        if required_sources - {passage["document_id"] for passage in passages}:
            raise ValueError(f"{identifier}: reference omits a required policy source")
        answers[identifier] = truth
    if answers.keys() != policy_ids:
        raise ValueError(
            f"Missing policy references: {sorted(policy_ids - answers.keys())}"
        )
    return PolicyReferences(
        cases=case_map,
        answers=answers,
        provenance={
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(content).hexdigest(),
            "manifest": manifest,
        },
    )


def policy_evaluation_query(row: dict[str, Any]) -> str:
    """
    Separate the original question, source context, and reference answer key.

    Parameters
    ----------
    row : dict
        Captured ``query`` and ``context``, plus ``ground_truth`` derived from
        the source PDFs. Evaluation-only policy interpretations are rejected.

    Returns
    -------
    str
        Evaluation-only JSON for reference-based correctness. Reference fields
        are not inserted into source context or represented as agent inputs.
    """
    if "reference_interpretations" in row:
        raise ValueError(
            "Policy grading must preserve the captured question and context "
            "without evaluation-only interpretations"
        )
    return json.dumps(
        {
            "question": row["query"],
            "policy_context": row["context"],
            "reference_answer": row["ground_truth"],
        },
        ensure_ascii=False,
    )


def policy_dataset_rows(
    rows: list[dict[str, Any]], references: PolicyReferences
) -> list[dict[str, Any]]:
    """
    Attach independent reference answers without altering captured responses.

    Parameters
    ----------
    rows : list of dict
        Captured questions, answers, context, citations, and expected behavior.
    references : PolicyReferences
        References verified before response collection or rescoring.

    Returns
    -------
    list of dict
        Copies with PDF-derived ``ground_truth`` and a ``policy_query``
        separating the question, unchanged source context, and expected answer.
        Non-policy cases have an empty reference and no completeness scoring.
        Original questions, conversations, and responses are preserved.

    Raises
    ------
    ValueError
        Rows do not exactly cover the cases, or their questions/requirements
        differ from the selected test cases.
    """
    identifiers = [row["case_id"] for row in rows]
    if len(identifiers) != len(references.cases) or set(identifiers) != references.cases.keys():
        raise ValueError("Captured rows must uniquely cover all reference cases")
    prepared = []
    for row in rows:
        case = references.cases[row["case_id"]]
        if (
            row["query"] != case["query"]
            or row["expected_behavior"] != expected_behavior(case)
        ):
            raise ValueError(f"{case['id']}: captured question or requirements changed")
        item = {
            **row,
            "ground_truth": references.answers.get(row["case_id"], ""),
        }
        item["policy_query"] = policy_evaluation_query(item)
        prepared.append(item)
    return prepared

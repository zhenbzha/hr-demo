"""Load evaluation cases and check observed responses against requirements."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentResult, captured_response_items

UNSCORED_ASSERTION_FIELDS = {
    "expectedBehavior",
    "assertions",
    "expectedPrimaryDocumentIds",
    "expectedSupportingDocumentIds",
}
LIST_FIELDS = {
    "expectedDocumentIds",
    "forbiddenRegions",
    "forbiddenDocumentIds",
    "allowedCitationRegions",
    "requiredCitationFields",
    "workbookRefs",
}
CASE_FIELDS = LIST_FIELDS | {
    "id",
    "query",
    "hardRequirement",
    "expectedCitationCount",
}


@dataclass
class CaseChecks:
    """
    Results of the rule-based checks for one response.

    ``status`` is ``failed`` if an answer/citation check fails and
    ``passed`` otherwise.

    Attributes
    ----------
    failures : list of str
        Observed violations, such as a missing expected source.
    """

    failures: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.failures:
            return "failed"
        return "passed"


def _validate_case(case: Any) -> None:
    if isinstance(case, dict) and case.keys() & UNSCORED_ASSERTION_FIELDS:
        raise ValueError(
            "Native demo cases cannot declare unscored free-text assertions or "
            "primary/supporting evidence roles; use the fixed rubric, PDF references "
            "and expectedDocumentIds."
        )
    if not isinstance(case, dict) or case.keys() - CASE_FIELDS:
        raise ValueError("Evaluation case contains unknown/unsupported fields")
    for name in ("id", "query"):
        if not isinstance(case.get(name), str) or not case[name].strip():
            raise ValueError(f"Case {name} must be a nonempty string")
    if type(case.get("hardRequirement")) is not bool:
        raise ValueError(f"{case['id']}: hardRequirement must be a boolean")
    for name in LIST_FIELDS & case.keys():
        if not isinstance(case[name], list) or any(
            not isinstance(value, str) or not value.strip() for value in case[name]
        ):
            raise ValueError(
                f"{case['id']}: {name} must be an array of nonempty strings"
            )
    if "expectedCitationCount" in case and (
        type(case["expectedCitationCount"]) is not int
        or case["expectedCitationCount"] < 0
    ):
        raise ValueError("expectedCitationCount must be a nonnegative integer")


def load_cases(path: Path) -> list[dict[str, Any]]:
    """
    Validate a case file and select the cases this evaluator will run.

    Parameters
    ----------
    path : pathlib.Path
        JSON file with a top-level ``cases`` array. Each case requires
        ``id``, ``query``, and a boolean ``hardRequirement``. Optional
        expectations name source IDs, citation fields, or regions. Native
        cases cannot declare free-text assertions or primary/supporting roles
        that the deterministic checks do not verify.

    Returns
    -------
    list of dict
        Cases marked ``hardRequirement: true``, in file order.
        All entries in the file are validated before selecting cases.

    Raises
    ------
    ValueError
        Case fields are unsupported/malformed, IDs are duplicated,
        or no hard-requirement cases remain.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("Evaluation dataset must contain a cases array")
    for case in payload["cases"]:
        _validate_case(case)
    identifiers = [case["id"] for case in payload["cases"]]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Evaluation case IDs must be unique")
    cases = [case for case in payload["cases"] if case["hardRequirement"]]
    if not cases:
        raise ValueError("No hard-requirement evaluation cases were found")
    return cases


def expected_behavior(case: dict[str, Any]) -> str:
    """
    Preserve citation expectations to verify saved capture integrity.

    Parameters
    ----------
    case : dict
        Validated case with expected/forbidden source IDs or regions,
        and citation requirements.

    Returns
    -------
    str
        Serialized citation metadata.
        The native rubric does not consume this field; its scoring requirements
        are defined in the registered rubric and separate citation checks.
    """
    instructions = []
    for key, label in (
        ("expectedDocumentIds", "Use evidence from documents"),
        ("forbiddenRegions", "Do not cite regions"),
        ("allowedCitationRegions", "Cited regions must be limited to"),
        ("forbiddenDocumentIds", "Do not cite documents"),
        ("requiredCitationFields", "Structured citations must contain"),
    ):
        if case.get(key):
            instructions.append(label + ": " + ", ".join(case[key]))
    return "\n".join(instructions)


def evaluation_row(
    case: dict[str, Any], result: AgentResult, *, context: str | None = None
) -> dict[str, Any]:
    """
    Build a Foundry dataset row from a case and its observed response.

    Parameters
    ----------
    case : dict
        Test question and expectations returned by ``load_cases``.
    result : AgentResult
        Agent answer, source references, original response items, and response ID.
    context : str or None, default=None
        Policy text provided to the model when it generated the answer.
        None uses the text from ``result``'s cited passages.
        An explicitly supplied empty string remains empty.

    Returns
    -------
    dict
        Question, answer, structured output items, policy text, citations,
        expected behavior, tool calls, and tool definitions, with IDs linking
        the row to its case and agent response.
    """
    row = {
        "case_id": case["id"],
        "query": case["query"],
        "response": result.response,
        "context": result.context if context is None else context,
        "citations": result.citations,
        "expected_behavior": expected_behavior(case),
        "tool_calls": result.tool_calls,
        "tool_definitions": [],
        "response_id": result.response_id,
    }
    if result.output_items is not None:
        row["output_items"] = result.output_items
    row["output_items"] = captured_response_items(row)
    return row


def _check_evidence(
    case: dict[str, Any], result: AgentResult, checks: CaseChecks
) -> None:
    document_ids = {reference.get("document_id") for reference in result.references}
    regions = {reference.get("region") for reference in result.references}
    expected = {
        document_id
        for key in (
            "expectedDocumentIds",
            "expectedPrimaryDocumentIds",
            "expectedSupportingDocumentIds",
        )
        for document_id in case.get(key, [])
    }
    if missing := expected - document_ids:
        checks.failures.append(f"Missing expected documents: {sorted(missing)}")
    if forbidden := document_ids & set(case.get("forbiddenDocumentIds", [])):
        checks.failures.append(f"Forbidden documents: {sorted(forbidden)}")
    if forbidden := regions & set(case.get("forbiddenRegions", [])):
        checks.failures.append(f"Forbidden regions: {sorted(forbidden)}")
    allowed_regions = set(case.get("allowedCitationRegions", []))
    if allowed_regions and regions - allowed_regions:
        checks.failures.append(
            "Citations are outside the allowed policy regions"
        )
    if (
        "expectedCitationCount" in case
        and len(result.references) != case["expectedCitationCount"]
    ):
        checks.failures.append(
            f"Expected {case['expectedCitationCount']} citations, got {len(result.references)}"
        )


def _check_citations(
    case: dict[str, Any], result: AgentResult, checks: CaseChecks
) -> None:
    required_fields = case.get("requiredCitationFields", [])
    if required_fields and not result.references:
        checks.failures.append("Required citations were not returned")
    for index, reference in enumerate(result.references):
        missing = [
            name for name in required_fields if reference.get(name) in (None, "", [])
        ]
        if missing:
            checks.failures.append(f"Citation {index} missing fields: {missing}")
        if "page_start" in reference or "page_end" in reference:
            start, end = reference.get("page_start"), reference.get("page_end")
            if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                checks.failures.append(
                    f"Citation {index} has an invalid physical page range"
                )


def validate_case_result(case: dict[str, Any], result: AgentResult) -> CaseChecks:
    """
    Check the source references and citations returned in an agent response.

    Parameters
    ----------
    case : dict
        Validated case defining expected/forbidden documents or regions,
        and citation count/fields.
    result : AgentResult
        Parsed agent response and its citations.

    Returns
    -------
    CaseChecks
        Observed answer/citation failures.
    """
    checks = CaseChecks()
    _check_evidence(case, result, checks)
    _check_citations(case, result, checks)
    return checks

"""Load evaluation cases and check observed responses against requirements."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentResult

LIST_FIELDS = {
    "expectedBehavior",
    "assertions",
    "expectedDocumentIds",
    "expectedPrimaryDocumentIds",
    "expectedSupportingDocumentIds",
    "forbiddenRegions",
    "forbiddenDocumentIds",
    "expectedRetrievalRegions",
    "requiredCitationFields",
    "workbookRefs",
}
CASE_FIELDS = LIST_FIELDS | {
    "id",
    "query",
    "hardRequirement",
    "expectedRegionClassification",
    "expectedRetrieval",
    "expectedCitationCount",
}


@dataclass
class CaseChecks:
    failures: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return (
            "failed" if self.failures else "unverified" if self.unverified else "passed"
        )


def _validate_case(case: Any) -> None:
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
    if "expectedRetrieval" in case and type(case["expectedRetrieval"]) is not bool:
        raise ValueError("expectedRetrieval must be a boolean")
    if "expectedCitationCount" in case and (
        type(case["expectedCitationCount"]) is not int
        or case["expectedCitationCount"] < 0
    ):
        raise ValueError("expectedCitationCount must be a nonnegative integer")
    classification = case.get("expectedRegionClassification")
    if "expectedRegionClassification" in case:
        if not isinstance(classification, dict) or classification.keys() != {
            "regions",
            "status",
        }:
            raise ValueError(
                "expectedRegionClassification requires regions and status, not an inferred confidence"
            )
        if not isinstance(classification["status"], str) or classification[
            "status"
        ] not in {
            "retrieve",
            "clarify",
            "unsupported",
        }:
            raise ValueError("Invalid expected classifier status")
        if not isinstance(classification["regions"], list) or any(
            not isinstance(region, str) for region in classification["regions"]
        ):
            raise ValueError(
                "Expected classification regions must be an array of strings"
            )


def load_cases(path: Path) -> list[dict[str, Any]]:
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
    """Build the answer and citation rubric for an evaluation case."""
    instructions = [*case.get("expectedBehavior", []), *case.get("assertions", [])]
    for key, label in (
        ("expectedDocumentIds", "Use evidence from documents"),
        ("expectedPrimaryDocumentIds", "Primary policy evidence must come from"),
        ("expectedSupportingDocumentIds", "Supporting policy evidence must include"),
        ("forbiddenRegions", "Do not cite regions"),
        ("forbiddenDocumentIds", "Do not cite documents"),
        ("requiredCitationFields", "Structured citations must contain"),
    ):
        if case.get(key):
            instructions.append(label + ": " + ", ".join(case[key]))
    return "\n".join(instructions)


def evaluation_row(
    case: dict[str, Any], result: AgentResult, *, context: str | None = None
) -> dict[str, Any]:
    """Build a Foundry dataset row from a case and its observed response.

    Use the context override when supplied; otherwise use citation text.
    """
    return {
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
    allowed_regions = set(case.get("expectedRetrievalRegions", []))
    if allowed_regions and regions - allowed_regions:
        checks.failures.append(
            "Returned evidence is outside the allowed retrieval regions"
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


def _check_internal_behavior(
    case: dict[str, Any], result: AgentResult, checks: CaseChecks
) -> None:
    diagnostics = result.diagnostics or {}
    if "expectedRegionClassification" in case:
        actual = diagnostics.get("classification")
        if actual is None:
            checks.unverified.append(
                "Classifier decision is not exposed by this endpoint"
            )
        elif (
            not isinstance(actual, dict)
            or not isinstance(actual.get("status"), str)
            or not isinstance(actual.get("regions"), list)
            or any(not isinstance(region, str) for region in actual["regions"])
        ):
            checks.failures.append("Invalid classifier diagnostics")
        elif (
            actual["status"] != case["expectedRegionClassification"]["status"]
            or set(actual["regions"])
            != set(case["expectedRegionClassification"]["regions"])
        ):
            checks.failures.append(
                "Observed classifier decision does not match the expected status/regions"
            )
    if "expectedRetrieval" in case or "expectedRetrievalRegions" in case:
        retrieval_regions = diagnostics.get("retrieval_regions")
        if retrieval_regions is None:
            checks.unverified.append(
                "Retrieval calls are not exposed; citations alone cannot prove whether retrieval ran"
            )
        elif not isinstance(retrieval_regions, list) or any(
            not isinstance(region, str) for region in retrieval_regions
        ):
            checks.failures.append("Invalid retrieval-region diagnostics")
        else:
            if (
                "expectedRetrieval" in case
                and bool(retrieval_regions) != case["expectedRetrieval"]
            ):
                checks.failures.append(
                    "Observed retrieval activity differs from the case requirement"
                )
            if "expectedRetrievalRegions" in case and set(retrieval_regions) != set(
                case["expectedRetrievalRegions"]
            ):
                checks.failures.append(
                    "Observed retrieval scopes differ from the required scopes"
                )


def validate_case_result(case: dict[str, Any], result: AgentResult) -> CaseChecks:
    """Return deterministic check results, including unverified requirements."""
    checks = CaseChecks()
    _check_evidence(case, result, checks)
    _check_citations(case, result, checks)
    _check_internal_behavior(case, result, checks)
    return checks

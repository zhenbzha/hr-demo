#!/usr/bin/env python3
"""Run hard Ask HR quality cases against the deployed Responses endpoint."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = PROJECT_ROOT / "tests" / "agent-quality-cases.json"
OUTPUT_PATH = (
    PROJECT_ROOT / "output" / "agent-quality" / "remote-results.json"
)


def executable(name: str) -> str:
    path = shutil.which(f"{name}.cmd") or shutil.which(name)
    if not path:
        raise FileNotFoundError(f"{name} was not found on PATH")
    return path


def run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed: {' '.join(command)}\n{result.stderr}"
        )
    return result.stdout.strip()


def deployed_endpoint() -> str:
    return run(
        [
            executable("azd"),
            "env",
            "get-value",
            "AGENT_ASK_HR_RESPONSES_ENDPOINT",
        ],
        cwd=PROJECT_ROOT / "ask-hr",
    )


def access_token() -> str:
    return run(
        [
            executable("az"),
            "account",
            "get-access-token",
            "--scope",
            "https://ai.azure.com/.default",
            "--query",
            "accessToken",
            "-o",
            "tsv",
        ]
    )


def invoke(endpoint: str, token: str, query: str) -> str:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {
                "model": "ask-hr",
                "input": query,
                "store": False,
            }
        ).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        payload = json.loads(response.read().decode("utf-8"))
    texts = [
        content["text"]
        for item in payload.get("output", [])
        for content in item.get("content", [])
        if content.get("type") == "output_text" and content.get("text")
    ]
    answer = "\n\n".join(texts).strip()
    if not answer:
        raise RuntimeError(f"No output text for query: {query}")
    return answer


def contains_all(*values: str) -> Callable[[str], None]:
    def validate(answer: str) -> None:
        missing = [value for value in values if value not in answer]
        if missing:
            raise AssertionError(f"Missing required text: {missing}")

    return validate


def clarification(answer: str) -> None:
    if "Which employment region" not in answer:
        raise AssertionError("Agent did not ask for region clarification")
    if "Source:" in answer or "來源" in answer:
        raise AssertionError("Ambiguous query returned policy evidence")


def unsupported_uk(answer: str) -> None:
    lowered = answer.casefold()
    if (
        "no active" not in lowered
        or "policy" not in lowered
        or "uk" not in lowered
    ):
        raise AssertionError("Agent did not reject unsupported UK policy")
    for forbidden in ("Finland HR", "India HR", "HongKong HR"):
        if forbidden in answer:
            raise AssertionError(f"Agent substituted forbidden source {forbidden}")


def employee_facing_language(answer: str) -> None:
    lowered = answer.casefold()
    forbidden = (
        "indexed policy",
        "indexed hr policies",
        "indexed document",
        "retrieved evidence",
        "search index",
        "vector index",
        "document chunks",
        "policy corpus",
    )
    matches = [phrase for phrase in forbidden if phrase in lowered]
    if matches:
        raise AssertionError(
            f"Response exposed implementation language: {matches}"
        )


def no_evidence(answer: str) -> None:
    lowered = answer.casefold()
    if not any(
        phrase in lowered
        for phrase in (
            "does not specify",
            "does not cover",
            "not covered",
            "not enough information",
            "cannot confirm",
            "cannot identify",
            "can't identify",
            "no company policy",
        )
    ):
        raise AssertionError("Agent did not clearly state the policy gap")
    if "source:" in lowered or "來源" in answer:
        raise AssertionError("No-evidence answer cited an unrelated policy")


VALIDATORS: dict[str, Callable[[str], None]] = {
    "AGENT-REGION-CLARIFY": clarification,
    "AGENT-REGION-TYPO": contains_all(
        "Hong Kong",
        "HongKong HR Payroll MPF and Benefits zh-HK",
    ),
    "AGENT-LOCAL-PRECEDENCE": contains_all(
        "Finland local policy",
        "Finland HR Leave and Holidays",
    ),
    "AGENT-GLOBAL-SUPPLEMENT": contains_all(
        "Global",
        "Global HR Policy Handbook",
    ),
    "AGENT-EXPLICIT-COMPARISON": contains_all(
        "Finland HR Leave and Holidays",
        "HongKong HR Working Hours Leave and Holidays zh-HK",
    ),
    "AGENT-MULTILINGUAL-FI": contains_all(
        "Finland HR Employment Contracts and Termination",
    ),
    "AGENT-MULTILINGUAL-HK": contains_all(
        "HongKong HR Working Hours Leave and Holidays zh-HK",
    ),
    "AGENT-NO-EVIDENCE": no_evidence,
    "AGENT-CITATIONS": contains_all(
        "India HR POSH Equality and Workplace Conduct",
        "p.",
    ),
    "AGENT-EEM-NO-EVIDENCE": clarification,
    "AGENT-FI-TIME-ATTENDANCE": contains_all(
        "Finland HR Working Hours Pay and Benefits",
    ),
    "AGENT-FI-PAYROLL": contains_all(
        "Finland HR Working Hours Pay and Benefits",
    ),
    "AGENT-UNSUPPORTED-UK": unsupported_uk,
    "AGENT-OPERATIVE-DEADLINE": contains_all(
        "India HR POSH Equality and Workplace Conduct",
        "90 days",
    ),
    "AGENT-PERFORMANCE-GUIDE": contains_all(
        "Global HR Policy Handbook",
    ),
    "AGENT-REGION-LABEL": contains_all(
        "Hong Kong",
        "HongKong HR Working Hours Leave and Holidays zh-HK",
    ),
}


def main() -> None:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    endpoint = deployed_endpoint()
    token = access_token()
    results = []
    failures = []

    for case in cases:
        if not case.get("hardRequirement"):
            continue
        case_id = case["id"]
        validator = VALIDATORS.get(case_id)
        if validator is None:
            failures.append(
                {"id": case_id, "error": "No executable validator"}
            )
            continue
        try:
            answer = invoke(endpoint, token, case["query"])
            employee_facing_language(answer)
            validator(answer)
            results.append(
                {
                    "id": case_id,
                    "query": case["query"],
                    "answer": answer,
                    "passed": True,
                }
            )
            print(f"PASS {case_id}")
        except Exception as error:
            failure = {
                "id": case_id,
                "query": case["query"],
                "error": str(error),
                "passed": False,
            }
            failures.append(failure)
            print(f"FAIL {case_id}: {error}")

    output = {
        "agent": "ask-hr",
        "passed": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if failures:
        raise RuntimeError(
            f"{len(failures)} hosted-agent quality cases failed"
        )
    print(f"All {len(results)} hosted-agent quality cases passed")


if __name__ == "__main__":
    main()

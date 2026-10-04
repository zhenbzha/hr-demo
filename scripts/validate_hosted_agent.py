#!/usr/bin/env python3
"""Run hard Ask HR quality cases against the deployed Responses endpoint."""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = PROJECT_ROOT / "tests" / "agent-quality-cases.json"
OUTPUT_PATH = (
    PROJECT_ROOT / "output" / "agent-quality" / "remote-results.json"
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class RateLimitError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


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


@dataclass
class AgentResponse:
    answer: str
    references: list[dict]

    @property
    def validation_text(self) -> str:
        return self.answer + "\n" + json.dumps(
            self.references,
            ensure_ascii=False,
        )


def invoke(endpoint: str, token: str, query: str) -> AgentResponse:
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
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code != 429:
            raise
        retry_after = error.headers.get("Retry-After")
        raise RateLimitError(
            error.read().decode("utf-8", errors="replace"),
            float(retry_after) if retry_after else None,
        ) from error
    texts = [
        content["text"]
        for item in payload.get("output", [])
        for content in item.get("content", [])
        if content.get("type") == "output_text" and content.get("text")
    ]
    answer = "\n\n".join(texts).strip()
    if not answer:
        error = payload.get("error")
        error_message = (
            str(error.get("message", ""))
            if isinstance(error, dict)
            else str(error or "")
        )
        if "rate limit" in error_message.casefold():
            raise RateLimitError(
                f"responseId={payload.get('id')}; {error_message}"
            )
        output_summary = [
            {
                "type": item.get("type"),
                "status": item.get("status"),
                "contentTypes": [
                    content.get("type")
                    for content in item.get("content", [])
                ],
            }
            for item in payload.get("output", [])
        ]
        raise RuntimeError(
            "No output text for query "
            f"{query!r}; responseId={payload.get('id')}; "
            f"error={payload.get('error')}; output={output_summary}"
        )
    references: list[dict] = []
    for item in payload.get("output", []):
        if item.get("type") != "function_call_output":
            continue
        output = item.get("output")
        if not isinstance(output, str):
            continue
        try:
            tool_result = json.loads(output)
        except json.JSONDecodeError:
            continue
        for evidence in tool_result.get("evidence", []):
            references.extend(
                reference.get("sourceData")
                for reference in evidence.get("result", {}).get(
                    "references",
                    [],
                )
                if reference.get("sourceData")
            )
    return AgentResponse(answer=answer, references=references)


def contains_all(*values: str) -> Callable[[AgentResponse], None]:
    def validate(result: AgentResponse) -> None:
        missing = [
            value for value in values if value not in result.validation_text
        ]
        if missing:
            raise AssertionError(f"Missing required text: {missing}")

    return validate


def clarification(result: AgentResponse) -> None:
    answer = result.answer
    if "Which employment region" not in answer:
        raise AssertionError("Agent did not ask for region clarification")
    if result.references or "Reference:" in answer:
        raise AssertionError("Ambiguous query returned policy evidence")


def unsupported_uk(result: AgentResponse) -> None:
    answer = result.answer
    lowered = answer.casefold()
    if (
        "no active" not in lowered
        or "policy" not in lowered
        or "uk" not in lowered
    ):
        raise AssertionError("Agent did not reject unsupported UK policy")
    for forbidden in ("Finland HR", "India HR", "HongKong HR"):
        if forbidden in result.validation_text:
            raise AssertionError(f"Agent substituted forbidden source {forbidden}")


def employee_facing_language(result: AgentResponse) -> None:
    answer = result.answer
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


def no_evidence(result: AgentResponse) -> None:
    answer = result.answer
    lowered = answer.casefold()
    if not any(
        phrase in lowered
        for phrase in (
            "does not specify",
            "does not cover",
            "do not cover",
            "not covered",
            "not enough information",
            "cannot confirm",
            "cannot identify",
            "can't identify",
            "no company policy",
        )
    ):
        raise AssertionError("Agent did not clearly state the policy gap")
    if result.references or "Reference:" in answer:
        raise AssertionError("No-evidence answer cited an unrelated policy")


def rich_citations(result: AgentResponse) -> None:
    reference_ids = {
        match.group(1)
        for match in re.finditer(
            r"(?im)^Reference:\s*([a-f0-9]{20,})\s*$",
            result.answer,
        )
    }
    source_ids = {
        reference.get("chunk_id")
        for reference in result.references
        if reference.get("chunk_id")
    }
    if not reference_ids:
        raise AssertionError("Agent did not emit stable reference IDs")
    if not reference_ids.issubset(source_ids):
        raise AssertionError("Agent emitted a reference not returned by retrieval")
    for reference_id in reference_ids:
        source = next(
            reference
            for reference in result.references
            if reference.get("chunk_id") == reference_id
        )
        for field in (
            "document_id",
            "document_title",
            "page_start",
            "page_end",
            "document_path",
            "annotation_path",
            "chunk_text",
        ):
            if source.get(field) in (None, ""):
                raise AssertionError(
                    f"Reference {reference_id} is missing {field}"
                )


VALIDATORS: dict[str, Callable[[AgentResponse], None]] = {
    "AGENT-REGION-CLARIFY": clarification,
    "AGENT-REGION-TYPO": contains_all(
        "Hong Kong",
        "HongKong HR Payroll MPF and Benefits zh-HK",
    ),
    "AGENT-LOCAL-PRECEDENCE": contains_all(
        "Finland",
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
    "AGENT-CITATIONS": rich_citations,
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
            deadline = time.monotonic() + 5 * 60
            backoff_seconds = 15.0
            while True:
                try:
                    response = invoke(endpoint, token, case["query"])
                    break
                except RateLimitError as error:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Rate limit did not clear within five minutes: "
                            f"{error}"
                        ) from error
                    delay = error.retry_after or backoff_seconds
                    delay = min(delay, 60.0) + random.uniform(0, 1)
                    print(
                        f"RATE_LIMIT {case_id}: waiting {delay:.1f}s "
                        "before retry"
                    )
                    time.sleep(delay)
                    backoff_seconds = min(backoff_seconds * 2, 60.0)
            employee_facing_language(response)
            validator(response)
            results.append(
                {
                    "id": case_id,
                    "query": case["query"],
                    "answer": response.answer,
                    "references": response.references,
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

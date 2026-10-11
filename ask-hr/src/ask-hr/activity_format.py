from __future__ import annotations

from typing import Any


def format_activity_answer(
    answer: str,
    citations: list[dict[str, Any]],
) -> str:
    if not citations:
        return answer
    lines = [answer, "", "Sources:"]
    for citation in citations:
        label = str(citation.get("label") or "Source")
        location = citation.get("location")
        url = citation.get("url")
        suffix = f" - {location}" if isinstance(location, str) and location else ""
        if isinstance(url, str) and url:
            lines.append(f"- [{label}{suffix}]({url})")
        else:
            lines.append(f"- {label}{suffix}")
    return "\n".join(lines)

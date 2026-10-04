from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field


REGION_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class RegionDecision(BaseModel):
    status: Literal["retrieve", "clarify", "unsupported"]
    regions: list[str] = Field(default_factory=list)
    message: str | None = None


def load_supported_regions(raw_value: str) -> dict[str, str]:
    parsed = json.loads(raw_value)
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError(
            "ASK_HR_SUPPORTED_REGIONS_JSON must be a non-empty JSON object"
        )

    supported_regions: dict[str, str] = {}
    for key, display_name in parsed.items():
        if (
            not isinstance(key, str)
            or not REGION_KEY_PATTERN.fullmatch(key)
            or not isinstance(display_name, str)
            or not display_name.strip()
        ):
            raise ValueError(
                "Supported regions must map safe filter keys to display names"
            )
        supported_regions[key] = display_name.strip()
    return supported_regions


def validate_region_decision(
    decision: RegionDecision,
    supported_regions: dict[str, str],
) -> RegionDecision:
    normalized_regions = list(dict.fromkeys(decision.regions))
    invalid_regions = [
        region
        for region in normalized_regions
        if region not in supported_regions
    ]
    if invalid_regions:
        raise ValueError(
            f"Classifier returned unsupported region keys: {invalid_regions}"
        )

    if decision.status == "retrieve":
        if not normalized_regions:
            raise ValueError(
                "Classifier must return at least one region for retrieval"
            )
        return decision.model_copy(
            update={"regions": normalized_regions, "message": None}
        )

    if normalized_regions:
        raise ValueError(
            "Classifier must not return regions without retrieval"
        )
    if not decision.message or not decision.message.strip():
        raise ValueError(
            "Classifier must return a localized message when retrieval is skipped"
        )
    return decision.model_copy(
        update={"regions": [], "message": decision.message.strip()}
    )

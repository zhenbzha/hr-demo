import json
import unittest

from pydantic import ValidationError

from region_classification import (
    RegionDecision,
    load_supported_regions,
    validate_region_decision,
)


class RegionClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.supported = {
            "india": "India",
            "finland": "Finland",
            "hongkong": "Hong Kong",
        }

    def test_loads_configured_regions(self) -> None:
        self.assertEqual(
            load_supported_regions(json.dumps(self.supported)),
            self.supported,
        )

    def test_rejects_unsafe_filter_key(self) -> None:
        with self.assertRaises(ValueError):
            load_supported_regions('{"hong kong": "Hong Kong"}')

    def test_normalizes_retrieval_regions(self) -> None:
        decision = validate_region_decision(
            RegionDecision(
                status="retrieve",
                regions=["hongkong", "hongkong"],
            ),
            self.supported,
        )
        self.assertEqual(decision.regions, ["hongkong"])
        self.assertIsNone(decision.message)

    def test_rejects_unconfigured_region(self) -> None:
        with self.assertRaises(ValueError):
            validate_region_decision(
                RegionDecision(status="retrieve", regions=["sweden"]),
                self.supported,
            )

    def test_requires_localized_non_retrieval_message(self) -> None:
        with self.assertRaises(ValueError):
            validate_region_decision(
                RegionDecision(status="clarify"),
                self.supported,
            )

    def test_rejects_invalid_status(self) -> None:
        with self.assertRaises(ValidationError):
            RegionDecision(status="unknown")


if __name__ == "__main__":
    unittest.main()

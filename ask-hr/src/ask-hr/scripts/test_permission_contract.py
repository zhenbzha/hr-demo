from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault(
    "AZURE_SEARCH_ENDPOINT",
    "https://example.search.windows.net",
)
os.environ.setdefault("AZURE_SEARCH_KNOWLEDGE_BASE_NAME", "ask-hr-kb")
os.environ.setdefault("AZURE_SEARCH_KNOWLEDGE_SOURCE_NAME", "ask-hr-ks")

import permission_contract


class PermissionContractTests(unittest.TestCase):
    def test_missing_delegated_token_fails_closed(self) -> None:
        context = SimpleNamespace(
            client_headers={},
            platform_context=SimpleNamespace(user_id_key="user-id"),
        )
        with self.assertRaisesRegex(
            PermissionError,
            "delegated Azure AI Search token",
        ):
            permission_contract.require_security_context(context)

    def test_missing_trusted_user_fails_closed(self) -> None:
        context = SimpleNamespace(
            client_headers={
                "x-client-search-authorization": "delegated-token"
            },
            platform_context=SimpleNamespace(user_id_key=None),
        )
        with self.assertRaisesRegex(
            PermissionError,
            "trusted represented-user identity",
        ):
            permission_contract.require_security_context(context)

    def test_security_context_returns_separate_values(self) -> None:
        context = SimpleNamespace(
            client_headers={
                "x-client-search-authorization": "delegated-token"
            },
            platform_context=SimpleNamespace(user_id_key="user-id"),
        )
        self.assertEqual(
            permission_contract.require_security_context(context),
            ("delegated-token", "user-id"),
        )

    def test_search_headers_separate_service_and_user_tokens(self) -> None:
        headers = permission_contract.build_search_headers(
            "service-token",
            "delegated-token",
        )
        expected_authorization = "Bearer" + " " + "service-token"
        self.assertEqual(
            headers["Authorization"],
            expected_authorization,
        )
        self.assertEqual(
            headers["x-ms-query-source-authorization"],
            "delegated-token",
        )

    def test_search_token_never_enters_request_body(self) -> None:
        with patch.dict(
            os.environ,
            {"AZURE_SEARCH_KNOWLEDGE_SOURCE_NAME": "ask-hr-ks"},
        ):
            body = permission_contract.build_search_body(
                "Question",
                "india",
            )
        serialized = str(body)
        self.assertNotIn("delegated-token", serialized)
        self.assertEqual(
            body["knowledgeSourceParams"][0]["filterAddOn"],
            "region eq 'india'",
        )

    def test_search_reranks_passages_without_llm_query_planning(self) -> None:
        body = permission_contract.build_search_body(
            "How much annual leave applies?",
            "finland",
        )
        self.assertEqual(
            body["knowledgeSourceParams"][0]["resultsProcessing"],
            "rerank",
        )
        self.assertEqual(
            body["knowledgeSourceParams"][0]["rerankerThreshold"],
            1.5,
        )
        self.assertEqual(
            body["retrievalReasoningEffort"],
            {"kind": "minimal"},
        )

    def test_search_uses_knowledge_base_output_budgets(self) -> None:
        body = permission_contract.build_search_body("Question", "finland")
        self.assertNotIn("maxOutputDocuments", body)
        self.assertNotIn("maxOutputSizeInTokens", body)
        self.assertNotIn("maxOutputSize", body)

    def test_search_endpoint_is_fixed_https_origin(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AZURE_SEARCH_ENDPOINT":
                    "https://example.search.windows.net/"
            },
        ):
            self.assertEqual(
                permission_contract.validated_search_endpoint(),
                "https://example.search.windows.net",
            )

        with patch.dict(
            os.environ,
            {"AZURE_SEARCH_ENDPOINT": "http://malicious.example"},
        ), self.assertRaisesRegex(ValueError, "fixed HTTPS origin"):
            permission_contract.validated_search_endpoint()

    def test_compact_evidence_excludes_activity_and_tokens(self) -> None:
        compact = permission_contract.compact_search_evidence(
            [
                {
                    "scope": "india",
                    "result": {
                        "activity": [{"sensitive": "metadata"}],
                        "references": [
                            {
                                "sourceData": {
                                    "chunk_id": "india-leave-2",
                                    "document_title": "India Leave",
                                    "region": "india",
                                    "page_start": 2,
                                    "page_end": 2,
                                    "chunk_text": "Authorized policy text",
                                    "annotation_path": "internal/path",
                                }
                            }
                        ],
                    },
                }
            ]
        )
        serialized = str(compact)
        self.assertNotIn("activity", serialized)
        self.assertNotIn("annotation_path", serialized)
        self.assertEqual(
            compact[0]["references"][0]["content"],
            "Authorized policy text",
        )
        self.assertEqual(compact[0]["references"][0]["source_id"], "india-leave-2")

    def test_policy_content_without_a_source_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "source ID"):
            permission_contract.compact_search_evidence(
                [
                    {
                        "scope": "finland",
                        "result": {
                            "references": [
                                {"sourceData": {"chunk_text": "Policy text"}}
                            ]
                        },
                    }
                ]
            )

    def test_compact_evidence_keeps_all_valid_chunks_in_each_scope(self) -> None:
        evidence = []
        expected = []
        for region, count in (("finland", 12), ("global", 6)):
            references = [
                {
                    "sourceData": {
                        "chunk_id": f"{region}-{index}",
                        "document_title": f"{region} policy",
                        "region": region,
                        "page_start": index + 1,
                        "page_end": index + 1,
                        "chunk_text": f"Policy content {index}",
                    }
                }
                for index in range(count)
            ]
            evidence.append(
                {
                    "scope": region,
                    "result": {
                        "references": [
                            None,
                            *references[:5],
                            {"sourceData": {"chunk_text": ""}},
                            *references[5:],
                        ]
                    },
                }
            )
            expected.append(
                [f"Policy content {index}" for index in range(count)]
            )

        compact = permission_contract.compact_search_evidence(evidence)
        self.assertEqual(
            [group["scope"] for group in compact], ["finland", "global"]
        )
        self.assertEqual(
            [
                [reference["content"] for reference in group["references"]]
                for group in compact
            ],
            expected,
        )


if __name__ == "__main__":
    unittest.main()

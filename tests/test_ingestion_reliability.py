"""Mocked failure-path tests: no Azure credentials or network access are needed."""

import base64
import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import index_hr_documents as ingestion_cli
import validate_document_retrieval as retrieval
from azure.core import MatchConditions
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.filedatalake import DataLakeFileClient
from ingestion import analysis_cache, azure_http, search_index, sources, state, storage
from ingestion.page import ContentBlock, Page
from ingestion.search_schema import index_schema
from ingestion.semantic import build_search_documents
from test_ingestion_contracts import (
    buffered_sender_factory,
    embedding_client,
    http_response,
    search_key_schema,
)


def successful_response(payload: dict) -> Mock:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status, response.headers = 200, {}
    response.read.return_value = json.dumps(payload).encode()
    return response


def acknowledgement(key: str, *, status: bool = True, status_code: int = 200) -> dict:
    return {"key": key, "status": status, "statusCode": status_code}


def local_pdf(
    root: Path,
    relative: str = "global/policy.pdf",
    content: bytes = b"%PDF-local-fixture",
) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def ingestion_arguments(folder: Path, *, skip_adls: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        document_intelligence_endpoint="https://example.cognitiveservices.azure.com",
        output_dir=folder / "output",
        openai_endpoint="https://example.openai.azure.com",
        embedding_deployment="embedding",
        embedding_dimensions=2,
        skip_adls_upload=skip_adls,
        storage_dfs_endpoint=None if skip_adls else "https://example.dfs.core.windows.net",
        storage_file_system=None if skip_adls else "docs",
        search_endpoint="https://example.search.windows.net",
        index_name="hr",
        recreate_index=False,
    )


class HttpTests(unittest.TestCase):
    def test_zero_retry_budget_still_makes_one_request(self):
        with patch(
            "urllib.request.urlopen", return_value=successful_response({"ok": True})
        ) as send:
            self.assertEqual(
                azure_http.request_json(
                    "POST", "https://example.test", max_retry_seconds=0
                )[2],
                {"ok": True},
            )
        send.assert_called_once()

    def test_tokens_are_reacquired_for_a_retried_request(self):
        headers = Mock(
            side_effect=[{"Authorization": "first"}, {"Authorization": "second"}]
        )
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=[TimeoutError("read timeout"), successful_response({})],
            ) as send,
            patch.object(azure_http.time, "sleep"),
        ):
            azure_http.request_json("GET", "https://example.test", headers=headers)
        self.assertEqual(headers.call_count, 2)
        self.assertEqual(send.call_args.args[0].get_header("Authorization"), "second")

    def test_transport_failure_does_not_retry_with_zero_budget(self):
        with (
            patch(
                "urllib.request.urlopen", side_effect=TimeoutError("read timeout")
            ) as send,
            self.assertRaisesRegex(RuntimeError, "transport failure"),
        ):
            azure_http.request_json("GET", "https://example.test", max_retry_seconds=0)
        send.assert_called_once()

    def test_invalid_retry_after_uses_bounded_backoff(self):
        with self.assertLogs(azure_http.logger, "WARNING"):
            delay = azure_http.retry_delay({"Retry-After": "invalid"}, 2)
        self.assertGreaterEqual(delay, 2)
        self.assertLessEqual(delay, 3)

    def test_millisecond_retry_header(self):
        self.assertEqual(azure_http.retry_delay({"retry-after-ms": "1500"}, 1), 1.5)

    def test_non_object_json_response_is_rejected(self):
        with (
            patch("urllib.request.urlopen", return_value=successful_response([])),
            self.assertRaisesRegex(RuntimeError, "other than an object"),
        ):
            azure_http.request_json("GET", "https://example.test")


class SearchSynchronizationTests(unittest.TestCase):
    def test_query_embeddings_use_the_same_validated_embedding_contract(self):
        client = embedding_client(
            {
                "model": "text-embedding-3-large",
                "data": [{"index": 0, "embedding": [0.5, 0.5]}],
            }
        )
        vector = retrieval.embed_query(
            client=client,
            deployment="embedding",
            dimensions=2,
            query="Policy?",
        )
        self.assertEqual(vector, [0.5, 0.5])
        client.close.assert_not_called()
        client.__exit__.assert_not_called()

    def test_retrieval_cases_share_one_caller_owned_embedding_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            args = SimpleNamespace(
                cases=folder / "cases.json",
                output=folder / "results.json",
                search_endpoint="https://example.search.windows.net",
                index_name="hr",
                openai_endpoint="https://example.openai.azure.com",
                embedding_deployment="embedding",
                embedding_dimensions=2,
            )
            args.cases.write_text(
                json.dumps({
                    "cases": [
                        {"id": identifier}
                        for identifier in sorted(retrieval.RUNNABLE_CASE_IDS)[:2]
                    ]
                }),
                encoding="utf-8",
            )
            credential = MagicMock()
            credential.__enter__.return_value = credential
            credential.__exit__.return_value = False
            client = embedding_client({})
            with (
                patch.object(retrieval, "parse_args", return_value=args),
                patch.object(retrieval, "AzureCliCredential", return_value=credential),
                patch.object(retrieval, "AzureOpenAI", return_value=client) as create,
                patch.object(
                    retrieval, "run_case",
                    side_effect=lambda **kwargs: {"id": kwargs["case"]["id"]},
                ) as run,
                patch.object(
                    retrieval, "request_json",
                    side_effect=AssertionError("Unexpected query outside a retrieval case"),
                ) as extra_query,
                patch("builtins.print"),
            ):
                retrieval.main()
            create.assert_called_once()
            self.assertEqual(run.call_count, 2)
            extra_query.assert_not_called()
            report = json.loads(args.output.read_text(encoding="utf-8"))
            self.assertEqual(report["passed"], 2)
            self.assertEqual(
                [result["id"] for result in report["results"]],
                sorted(retrieval.RUNNABLE_CASE_IDS)[:2],
            )
            for call in run.call_args_list:
                self.assertIs(call.kwargs["embedding_client"], client)
            client.__exit__.assert_called_once()
            credential.__exit__.assert_called_once()

    def test_mutable_schema_changes_are_applied_without_deleting_the_index(self):
        schema = index_schema(
            index_name="hr",
            openai_endpoint="https://example.openai.azure.com",
            embedding_deployment="embedding",
            dimensions=2,
        )
        existing = json.loads(json.dumps(schema))
        schema["fields"][2]["retrievable"] = False
        schema["semantic"]["configurations"][0]["rankingOrder"] = "BoostedRerankerScore"
        with (
            patch(
                "requests.Session.send",
                side_effect=lambda request, **kwargs: http_response(
                    request, existing if request.method == "GET" else {}
                ),
            ) as send,
            SearchIndexClient(
                "https://example.search.windows.net",
                AzureKeyCredential("unit-test"),
                api_version=azure_http.SEARCH_API_VERSION,
            ) as client,
        ):
            action = search_index.plan_index_update(
                client=client, schema=schema, recreate=False,
            )
            self.assertEqual(action, "update")
            search_index.create_index(client=client, schema=schema, action=action)
        sent_fields = {
            field["name"]: field
            for field in json.loads(send.call_args.args[0].body)["fields"]
        }
        self.assertFalse(sent_fields["document_title"]["retrievable"])
        self.assertEqual(
            json.loads(send.call_args.args[0].body)["semantic"], schema["semantic"],
        )
        self.assertEqual([call.args[0].method for call in send.call_args_list], ["GET", "PUT"])

    def test_immutable_field_changes_require_rebuilding(self):
        schema = index_schema(
            index_name="hr",
            openai_endpoint="https://example.openai.azure.com",
            embedding_deployment="embedding",
            dimensions=2,
        )
        for field_name, property_name, value in (
            ("chunk_id", "sortable", False),
            ("document_id", "filterable", False),
            ("document_id", "facetable", False),
            ("document_title", "searchable", False),
            ("page_start", "type", "Edm.Int64"),
        ):
            with self.subTest(field=field_name, property=property_name):
                existing = json.loads(json.dumps(schema))
                field = next(field for field in existing["fields"] if field["name"] == field_name)
                field[property_name] = value
                with (
                    patch(
                        "requests.Session.send",
                        side_effect=lambda request, existing=existing, **kwargs: http_response(
                            request, existing if request.method == "GET" else {},
                            status=204 if request.method == "DELETE" else 200,
                        ),
                    ) as send,
                    SearchIndexClient(
                        "https://example.search.windows.net",
                        AzureKeyCredential("unit-test"),
                        api_version=azure_http.SEARCH_API_VERSION,
                    ) as client,
                ):
                    action = search_index.plan_index_update(
                        client=client,
                        schema=schema,
                        recreate=False,
                    )
                    self.assertEqual(action, "rebuild")
                    search_index.create_index(client=client, schema=schema, action=action)
                self.assertEqual(
                    [call.args[0].method for call in send.call_args_list], ["GET", "DELETE", "PUT"],
                )

    def test_field_additions_backfill_and_field_removals_rebuild(self):
        schema = index_schema(
            index_name="hr", openai_endpoint="https://example.openai.azure.com",
            embedding_deployment="embedding", dimensions=2,
        )
        client = Mock()
        existing = json.loads(json.dumps(schema))
        existing["fields"].pop()
        client.get_index.return_value.as_dict.return_value = existing
        self.assertEqual(
            search_index.plan_index_update(client=client, schema=schema, recreate=False),
            "backfill",
        )
        existing = json.loads(json.dumps(schema))
        existing["fields"].append({"name": "legacy", "type": "Edm.String"})
        client.get_index.return_value.as_dict.return_value = existing
        self.assertEqual(
            search_index.plan_index_update(client=client, schema=schema, recreate=False),
            "rebuild",
        )
        client.delete_index.assert_not_called()
        client.create_or_update_index.assert_not_called()

    def test_service_defaults_field_order_and_etag_do_not_require_rebuilding(self):
        schema = index_schema(
            index_name="hr", openai_endpoint="https://example.openai.azure.com",
            embedding_deployment="embedding", dimensions=2,
        )
        existing = json.loads(json.dumps(schema))
        for field in existing["fields"]:
            scalar = field["type"] != "Collection(Edm.Single)"
            for name, value in (
                ("key", False), ("stored", True), ("searchable", False),
                ("filterable", scalar), ("sortable", scalar), ("facetable", scalar),
            ):
                field.setdefault(name, value)
            field["synonymMaps"] = []
        existing["fields"].reverse()
        existing["@odata.etag"] = "service-etag"
        client = Mock()
        client.get_index.return_value.as_dict.return_value = existing
        self.assertEqual(
            search_index.plan_index_update(client=client, schema=schema, recreate=False),
            "update",
        )

    def test_missing_index_creates_and_service_errors_never_trigger_deletion(self):
        client = Mock()
        client.get_index.side_effect = ResourceNotFoundError("missing")
        self.assertEqual(
            search_index.plan_index_update(client=client, schema={"name": "hr"}, recreate=False),
            "create",
        )
        client.get_index.side_effect = HttpResponseError("forbidden")
        with self.assertRaisesRegex(HttpResponseError, "forbidden"):
            search_index.plan_index_update(client=client, schema={"name": "hr"}, recreate=False)
        client.create_or_update_index.side_effect = HttpResponseError("invalid schema")
        with self.assertRaisesRegex(HttpResponseError, "invalid schema"):
            search_index.create_index(client=client, schema={"name": "hr"}, action="update")
        client.delete_index.assert_not_called()

    def test_invalid_replacement_does_not_read_or_mutate_the_index(self):
        for documents in (
            [{"chunk_id": "one", "document_id": "other-pdf"}],
            [{"chunk_id": "one", "document_id": "pdf"}] * 2,
        ):
            with (
                self.subTest(documents=documents),
                patch.object(search_index, "_document_chunk_ids") as read,
                patch.object(search_index, "_write_actions") as write,
                self.assertRaisesRegex(ValueError, "unique chunk IDs"),
            ):
                search_index.synchronize_document(
                    client=Mock(), sender_factory=Mock(),
                    document_id="pdf", documents=documents,
                )
            read.assert_not_called()
            write.assert_not_called()

    def test_upload_failure_never_deletes_previous_chunks(self):
        with (
            patch.object(
                search_index, "_document_chunk_ids", return_value={"old"}
            ),
            patch.object(
                search_index,
                "upload_documents",
                side_effect=RuntimeError("upload failed"),
            ),
            patch.object(search_index, "_write_actions") as write,
            self.assertRaisesRegex(RuntimeError, "upload failed"),
        ):
            search_index.synchronize_document(
                client=Mock(),
                sender_factory=Mock(),
                document_id="pdf",
                documents=[{"chunk_id": "new", "document_id": "pdf"}],
            )
        write.assert_not_called()

    def test_lookup_failure_does_not_upload_or_delete(self):
        with (
            patch.object(
                search_index, "_document_chunk_ids", side_effect=TimeoutError("lookup failed")
            ),
            patch.object(search_index, "upload_documents") as upload,
            patch.object(search_index, "_write_actions") as write,
            self.assertRaisesRegex(TimeoutError, "lookup failed"),
        ):
            search_index.synchronize_document(
                client=Mock(),
                sender_factory=Mock(),
                document_id="pdf",
                documents=[{"chunk_id": "new", "document_id": "pdf"}],
            )
        upload.assert_not_called()
        write.assert_not_called()

    def test_only_obsolete_keys_are_deleted_after_upload_without_readback(self):
        events = []
        client = Mock()
        documents = [
            {"chunk_id": key, "document_id": "pdf"} for key in ("new", "same")
        ]
        with (
            patch.object(
                search_index,
                "_document_chunk_ids",
                side_effect=lambda *args: events.append("read") or {"old", "same"},
            ) as read,
            patch.object(
                search_index,
                "upload_documents",
                side_effect=lambda **kwargs: events.append("upload"),
            ) as upload,
            patch.object(
                search_index,
                "_write_actions",
                side_effect=lambda *args: events.append(args[1]),
            ),
        ):
            removed = search_index.synchronize_document(
                client=client,
                sender_factory=Mock(),
                document_id="pdf",
                documents=documents,
            )
        read.assert_called_once_with(client, "pdf")
        client.search.assert_not_called()
        self.assertEqual(upload.call_args.kwargs["documents"], documents)
        self.assertEqual(removed, 1)
        self.assertEqual(
            events,
            [
                "read",
                "upload",
                [{"@search.action": "delete", "chunk_id": "old"}],
            ],
        )

    def test_deletion_only_removes_the_pdf_without_uploading(self):
        with (
            patch.object(
                search_index, "_document_chunk_ids", return_value={"old"},
            ) as read,
            patch.object(search_index, "upload_documents") as upload,
            patch.object(search_index, "_write_actions") as write,
        ):
            removed = search_index.synchronize_document(
                client=Mock(),
                sender_factory=Mock(),
                document_id="deleted-pdf",
                documents=[],
            )
        self.assertEqual(read.call_args.args[1], "deleted-pdf")
        self.assertEqual(removed, 1)
        upload.assert_not_called()
        self.assertEqual(
            write.call_args.args[1], [{"@search.action": "delete", "chunk_id": "old"}],
        )

    def test_partial_batch_retries_only_failed_documents(self):
        responses = iter(
            [
                (
                    {
                        "value": [
                            acknowledgement("one"),
                            acknowledgement("two", status=False, status_code=503),
                        ]
                    },
                    207,
                ),
                ({"value": [acknowledgement("two")]}, 200),
            ]
        )
        submitted = []

        def send(request, **kwargs):
            if request.method == "GET":
                return http_response(request, search_key_schema())
            submitted.append(json.loads(request.body)["value"])
            payload, status = next(responses)
            return http_response(request, payload, status)

        with patch("requests.Session.send", side_effect=send):
            search_index.upload_documents(
                sender_factory=buffered_sender_factory(),
                documents=[{"chunk_id": "one"}, {"chunk_id": "two"}],
            )
        self.assertEqual(len(submitted), 2)
        self.assertEqual(
            submitted[-1],
            [{"chunk_id": "two", "@search.action": "upload"}],
        )

    def test_duplicate_acknowledgements_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "uniquely cover"):
            search_index._acknowledgements(
                {"value": [acknowledgement("one"), acknowledgement("one")]},
                {"one", "two"},
            )

    def test_non_object_indexing_response_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "JSON object"):
            search_index._acknowledgements([], {"one"})

    def test_sdk_deletion_sends_only_the_requested_stale_key(self):
        submitted = []

        def send(request, **kwargs):
            if request.method == "GET":
                return http_response(request, search_key_schema())
            submitted.extend(json.loads(request.body)["value"])
            return http_response(request, {"value": [acknowledgement("stale")]})

        with patch("requests.Session.send", side_effect=send):
            search_index._write_actions(
                buffered_sender_factory(),
                [{"@search.action": "delete", "chunk_id": "stale"}],
            )
        self.assertEqual(submitted, [{"@search.action": "delete", "chunk_id": "stale"}])

    def test_explicit_sdk_flush_preserves_the_document_count_limit(self):
        batches = []

        def send(request, **kwargs):
            if request.method == "GET":
                return http_response(request, search_key_schema())
            keys = [action["chunk_id"] for action in json.loads(request.body)["value"]]
            batches.append(keys)
            return http_response(
                request, {"value": [acknowledgement(key) for key in keys]}
            )

        documents = [{"chunk_id": str(number)} for number in range(1025)]
        with patch("requests.Session.send", side_effect=send):
            search_index.upload_documents(
                sender_factory=buffered_sender_factory(),
                documents=documents,
            )
        self.assertTrue(
            all(len(batch) <= search_index.INDEX_BATCH_SIZE for batch in batches)
        )
        self.assertCountEqual(
            [key for batch in batches for key in batch],
            [document["chunk_id"] for document in documents],
        )

    def test_keyset_pagination_is_scoped_to_one_pdf_and_selects_only_keys(self):
        first_page = [{"chunk_id": f"a{number:04d}"} for number in range(1000)]
        pages = iter(
            [
                {"value": first_page},
                {"value": [{"chunk_id": "z"}]},
            ]
        )
        with (
            patch(
                "requests.Session.send",
                side_effect=lambda request, **kwargs: http_response(request, next(pages)),
            ) as send,
            SearchClient(
                "https://example.search.windows.net",
                "hr",
                AzureKeyCredential("unit-test"),
                api_version=azure_http.SEARCH_API_VERSION,
            ) as client,
        ):
            keys = search_index._document_chunk_ids(client, "pdf's-id")
        self.assertEqual(keys, {row["chunk_id"] for row in first_page} | {"z"})
        self.assertEqual(send.call_count, 2)
        first_query = json.loads(send.call_args_list[0].args[0].body)
        self.assertEqual(first_query["filter"], "document_id eq 'pdf''s-id'")
        self.assertEqual(first_query["select"], "chunk_id")
        self.assertEqual(
            json.loads(send.call_args_list[1].args[0].body)["filter"],
            "document_id eq 'pdf''s-id' and chunk_id gt 'a0999'",
        )

    def test_small_pdf_lookup_uses_one_query_and_leaves_the_client_open(self):
        client = Mock(spec=SearchClient)
        client.search.return_value = [{"chunk_id": "new"}]
        self.assertEqual(search_index._document_chunk_ids(client, "pdf"), {"new"})
        client.search.assert_called_once()
        client.close.assert_not_called()

    def test_buffered_operations_do_not_share_successful_acknowledgements(self):
        senders = []

        def make_sender(**callbacks):
            sender = MagicMock()
            sender.__enter__.return_value = sender
            sender.__exit__.return_value = False
            if not senders:
                sender.upload_documents.side_effect = lambda actions: [
                    callbacks["on_progress"](action) for action in actions
                ]
            senders.append(sender)
            return sender

        search_index.upload_documents(
            sender_factory=make_sender, documents=[{"chunk_id": "same"}]
        )
        with self.assertRaisesRegex(RuntimeError, r"missing=\['same'\]"):
            search_index._write_actions(
                make_sender, [{"@search.action": "delete", "chunk_id": "same"}]
            )
        self.assertEqual(len(senders), 2)
        for sender in senders:
            sender.__exit__.assert_called_once()

    def test_sdk_splits_oversized_requests_without_losing_utf8_documents(self):
        documents = [
            {"chunk_id": str(number), "chunk_text": "\u653f" * 30}
            for number in range(5)
        ]
        submitted_sizes = []
        accepted = []

        def send(request, **kwargs):
            if request.method == "GET":
                return http_response(request, search_key_schema())
            body = (
                request.body.encode() if isinstance(request.body, str) else request.body
            )
            submitted_sizes.append(len(body))
            if len(body) > 300:
                return http_response(
                    request,
                    {
                        "error": {
                            "code": "RequestEntityTooLarge",
                            "message": "fixture request size limit",
                        }
                    },
                    413,
                )
            keys = [action["chunk_id"] for action in json.loads(body)["value"]]
            accepted.extend(keys)
            return http_response(
                request, {"value": [acknowledgement(key) for key in keys]}
            )

        with patch("requests.Session.send", side_effect=send):
            search_index.upload_documents(
                sender_factory=buffered_sender_factory(),
                documents=documents,
            )
        self.assertGreater(max(submitted_sizes), 300)
        self.assertCountEqual(
            accepted, [document["chunk_id"] for document in documents]
        )

    def test_sdk_permanent_partial_failure_is_not_reported_as_success(self):
        def send(request, **kwargs):
            if request.method == "GET":
                return http_response(request, search_key_schema())
            return http_response(
                request,
                {
                    "value": [
                        acknowledgement("one"),
                        {
                            **acknowledgement("two", status=False, status_code=400),
                            "errorMessage": "invalid field",
                        },
                    ]
                },
                207,
            )

        with (
            patch("requests.Session.send", side_effect=send),
            self.assertRaisesRegex(RuntimeError, "invalid field"),
        ):
            search_index.upload_documents(
                sender_factory=buffered_sender_factory(),
                documents=[{"chunk_id": "one"}, {"chunk_id": "two"}],
            )

    def test_bad_vectors_and_wrong_model_are_rejected(self):
        for vector in ([float("nan"), 1], [0, 0], [True, 1], [1]):
            with self.subTest(vector=vector), self.assertRaises(RuntimeError):
                search_index._embedding_vectors(
                    {
                        "model": "text-embedding-3-large",
                        "data": [{"index": 0, "embedding": vector}],
                    },
                    1,
                    2,
                )
        with self.assertRaisesRegex(RuntimeError, "deployment must serve"):
            search_index._embedding_vectors(
                {"model": "another-model", "data": [{"index": 0, "embedding": [1, 2]}]},
                1,
                2,
            )


class SourceAndCacheTests(unittest.TestCase):
    def test_all_regional_pdfs_are_discovered_without_a_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            local_pdf(folder, "global/policy.pdf")
            local_pdf(folder, "finland/nested/policy.PDF")
            local_pdf(folder, "finland/translation_en.pdf")
            (folder / "notes.txt").write_text("Not a source")
            entries = sources.discover_sources(folder)
            self.assertEqual(len(entries), 3)
            self.assertEqual(len({entry["documentId"] for entry in entries}), 3)
            self.assertEqual(
                [(entry["region"], entry["language"]) for entry in entries],
                [("finland", "fi"), ("finland", "en"), ("global", "en")],
            )
            for entry in entries:
                self.assertRegex(
                    entry["documentId"],
                    r"^[a-f0-9]{8}-[a-f0-9]{4}-5[a-f0-9]{3}-[a-f0-9]{4}-[a-f0-9]{12}$",
                )
                self.assertRegex(entry["version"], r"^[a-f0-9]{32}$")
                self.assertEqual(
                    entry["sha256"],
                    hashlib.sha256(Path(entry["localPath"]).read_bytes()).hexdigest(),
                )

    def test_guid_survives_root_moves_and_content_changes_but_versions_do_not(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            first_root, second_root = Path(first), Path(second)
            local_pdf(first_root)
            changed_path = local_pdf(second_root)
            original = sources.discover_sources(first_root)[0]
            relocated = sources.discover_sources(second_root)[0]
            self.assertEqual(original["documentId"], relocated["documentId"])
            self.assertEqual(original["version"], relocated["version"])
            changed_path.write_bytes(b"%PDF-updated")
            changed = sources.discover_sources(second_root)[0]
            self.assertEqual(original["documentId"], changed["documentId"])
            self.assertNotEqual(original["version"], changed["version"])
            self.assertNotEqual(original["documentPath"], changed["documentPath"])
            case_path = changed_path.with_name("POLICY.pdf")
            changed_path.rename(case_path)
            renamed_case = sources.discover_sources(second_root)[0]
            self.assertEqual(changed["documentId"], renamed_case["documentId"])
            self.assertNotEqual(changed["version"], renamed_case["version"])
            case_path.rename(case_path.with_name("renamed.pdf"))
            self.assertNotEqual(
                original["documentId"],
                sources.discover_sources(second_root)[0]["documentId"],
            )

    def test_parser_changes_get_new_artifacts_without_changing_document_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            local_pdf(folder)
            original = sources.discover_sources(folder)[0]
            with patch.object(sources, "PARSER_VERSION", sources.PARSER_VERSION + 1):
                revised = sources.discover_sources(folder)[0]
            self.assertEqual(original["documentId"], revised["documentId"])
            self.assertNotEqual(original["annotationPath"], revised["annotationPath"])

    def test_missing_empty_and_unclassified_sources_fail_before_indexing(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with self.assertRaises(NotADirectoryError):
                sources.discover_sources(folder / "missing")
            with self.assertRaisesRegex(ValueError, "refusing to clear"):
                sources.discover_sources(folder)
        for relative in ("policy.pdf", "unknown/policy.pdf"):
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory() as temporary,
            ):
                folder = Path(temporary)
                local_pdf(folder, relative)
                with self.assertRaisesRegex(ValueError, "regional folder"):
                    sources.discover_sources(folder)

    def test_unreadable_directory_does_not_silently_drop_sources(self):
        def unreadable_walk(*, on_error):
            on_error(PermissionError("Cannot read regional folder"))
            return iter(())

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(Path, "walk", side_effect=unreadable_walk),
            self.assertRaises(PermissionError),
        ):
            sources.discover_sources(Path(temporary))

    def test_linked_directory_is_rejected_even_when_other_pdfs_remain(self):
        for link_method in ("is_symlink", "is_junction"):
            for listed_as_file in (False, True):
                with (
                    self.subTest(method=link_method, listed_as_file=listed_as_file),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    folder = Path(temporary)
                    readable = local_pdf(folder)
                    linked = folder / "finland"
                    walk = [
                        (
                            folder,
                            ["global"] if listed_as_file else ["global", "finland"],
                            ["finland"] if listed_as_file else [],
                        ),
                        (readable.parent, [], [readable.name]),
                    ]
                    with (
                        patch.object(Path, "walk", return_value=iter(walk)),
                        patch.object(
                            Path, link_method, autospec=True,
                            side_effect=lambda path, linked=linked: path == linked,
                        ),
                        self.assertRaisesRegex(ValueError, "Symbolic links and junctions"),
                    ):
                        sources.discover_sources(folder)

    def test_changed_pdf_is_rejected_after_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path = local_pdf(folder)
            entry = sources.discover_sources(folder)[0]
            path.write_bytes(b"%PDF-changed-after-discovery")
            with self.assertRaisesRegex(ValueError, "restart ingestion"):
                sources.verify_source(path, entry["sha256"])

    def test_live_evaluation_cases_reference_discovered_ids(self):
        root = Path(__file__).resolve().parents[1]
        identifiers = {
            entry["documentId"] for entry in sources.discover_sources(root / "data")
        }
        fields = {
            "expectedDocumentIds",
            "expectedPrimaryDocumentIds",
            "expectedSupportingDocumentIds",
            "forbiddenDocumentIds",
        }
        for name in ("agent-quality-cases.json", "document-retrieval-cases.json"):
            cases = json.loads((root / "tests" / name).read_text(encoding="utf-8"))
            for case in cases["cases"]:
                for field in fields & case.keys():
                    with self.subTest(file=name, case=case["id"], field=field):
                        self.assertFalse(set(case[field]) - identifiers)

    def test_saved_analysis_is_reused_and_invalid_results_are_regenerated(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            local_pdf(folder)
            item = sources.discover_sources(folder)[0]
            analysis_dir = folder / "analysis"
            cache = (
                analysis_dir / item["documentId"] / item["version"]
                / f"{item['sha256']}.json"
            )
            page = Page(
                0,
                0,
                "Policy.",
                blocks=[
                    ContentBlock("Policy.", (0,), heading="Example policy > Leave")
                ],
            )
            arguments = {
                "client": Mock(),
                "source": item,
                "analysis_dir": analysis_dir,
            }
            with patch.object(
                analysis_cache, "analyze_local_pdf", return_value=[page]
            ) as analyze:
                analysis_cache.load_or_analyze_pages(**arguments)
                result = analysis_cache.load_or_analyze_pages(**arguments)
                self.assertEqual(result, [page])
                analyze.assert_called_once()
                payload = json.loads(cache.read_text(encoding="utf-8"))
                for damaged in (
                    "{",
                    json.dumps({**payload, "pages": []}),
                    json.dumps({**payload, "pages": [{"invalid": True}]}),
                    json.dumps({
                        **payload,
                        "metadata": {**payload["metadata"], "schemaVersion": 3},
                    }),
                ):
                    with self.subTest(damaged=damaged):
                        cache.write_text(damaged, encoding="utf-8")
                        before = analyze.call_count
                        self.assertEqual(
                            analysis_cache.load_or_analyze_pages(**arguments), [page],
                        )
                        self.assertEqual(analyze.call_count, before + 1)
                with patch.object(analysis_cache, "PARSER_VERSION", 999):
                    before = analyze.call_count
                    self.assertEqual(
                        analysis_cache.load_or_analyze_pages(**arguments), [page],
                    )
                    self.assertEqual(analyze.call_count, before + 1)

    def test_corrupt_cache_blocks_do_not_become_pages(self):
        payload = {
            "pages": [
                {
                    "page_num": 0,
                    "offset": 0,
                    "text": "different",
                    "blocks": [
                        {"text": "original", "page_numbers": [0], "kind": "text"}
                    ],
                    "lines": [],
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "do not match"):
            analysis_cache._read_pages(payload)


class LocalIngestionStateTests(unittest.TestCase):
    def test_failed_atomic_replace_preserves_the_previous_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ingestion-state.json"
            previous = state.SavedFileRecords(
                settings={},
                files={"pdf": state.FileRecord("a" * 64, "version", 1)},
                pending_document_ids=[],
            )
            state.save_file_records(path, previous)
            before = path.read_bytes()
            with (
                patch("json_output.os.replace", side_effect=OSError("disk failure")),
                self.assertRaisesRegex(OSError, "disk failure"),
            ):
                state.save_file_records(path, state.SavedFileRecords({}, {}, ["pdf"]))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(state.load_file_records(path, {}), previous)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_file_records_are_not_used_to_skip_processing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ingestion-state.json"
            for record in (
                {"sha256": "a" * 64, "version": "version", "chunk_count": 0},
                {"sha256": "a" * 64, "version": "version", "chunk_count": True},
                {"sha256": "a" * 64, "version": "version", "chunk_count": None},
                {"sha256": None, "version": "version", "chunk_count": 1},
            ):
                with self.subTest(record=record):
                    path.write_text(json.dumps({
                        "schema_version": state.STATE_SCHEMA_VERSION, "settings": {},
                        "pending_document_ids": [], "files": {"pdf": record},
                    }), encoding="utf-8")
                    loaded = state.load_file_records(path, {})
                    self.assertEqual(loaded.pending_document_ids, [])
                    self.assertEqual(loaded.files, {})


class IngestionWorkflowTests(unittest.TestCase):
    def test_orchestration_owns_client_lifetimes_and_preserves_upload_order(self):
        for failure, skip_adls in (
            (None, False), ("embedding", False), ("upload", False),
            ("index", False), (None, True),
        ):
            with (
                self.subTest(failure=failure, skip_adls=skip_adls),
                tempfile.TemporaryDirectory() as temporary,
            ):
                folder = Path(temporary)
                local_pdf(folder)
                entries = sources.discover_sources(folder)
                args = ingestion_arguments(folder, skip_adls=skip_adls)
                calls = []
                clients = {
                    name: MagicMock()
                    for name in ("credential", "layout", "embedding", "storage", "admin", "search")
                }
                for client in clients.values():
                    client.__enter__.return_value = client
                    client.__exit__.return_value = False

                def embed(*, calls=calls, clients=clients, failure=failure, **kwargs):
                    calls.append("embedding")
                    self.assertIs(kwargs["client"], clients["embedding"])
                    if failure == "embedding":
                        raise RuntimeError("embedding failed")

                def upload(*, calls=calls, clients=clients, failure=failure, **kwargs):
                    calls.append("upload")
                    self.assertIs(kwargs["file_system"], clients["storage"])
                    entry = kwargs["source"]
                    annotation = kwargs["annotation"]
                    self.assertEqual(annotation["version"], entry["version"])
                    self.assertEqual(set(annotation["pages"]), {"1"})
                    if failure == "upload":
                        raise RuntimeError("upload failed")

                def synchronize(*, calls=calls, clients=clients, failure=failure, **kwargs):
                    calls.append("index")
                    self.assertIs(kwargs["client"], clients["search"])
                    self.assertEqual(
                        {document["document_id"] for document in kwargs["documents"]},
                        {kwargs["document_id"]},
                    )
                    if failure == "index":
                        raise RuntimeError("index failed")
                    return 0

                with (
                    patch.object(
                        ingestion_cli, "AzureCliCredential",
                        return_value=clients["credential"],
                    ),
                    patch.object(
                        ingestion_cli, "DocumentIntelligenceClient",
                        return_value=clients["layout"],
                    ),
                    patch.object(
                        ingestion_cli, "AzureOpenAI", return_value=clients["embedding"],
                    ),
                    patch.object(
                        ingestion_cli, "FileSystemClient", return_value=clients["storage"],
                    ) as make_storage,
                    patch.object(
                        ingestion_cli, "SearchIndexClient", return_value=clients["admin"],
                    ) as make_admin,
                    patch.object(
                        ingestion_cli, "SearchClient", return_value=clients["search"],
                    ) as make_search,
                    patch.object(
                        ingestion_cli, "embed_documents", side_effect=embed,
                    ),
                    patch.object(
                        ingestion_cli, "load_or_analyze_pages",
                        return_value=[Page(0, 0, "Policy applies.")],
                    ),
                    patch.object(ingestion_cli, "upload_adls_content", side_effect=upload),
                    patch.object(ingestion_cli, "create_index"),
                    patch.object(ingestion_cli, "plan_index_update", return_value="create"),
                    patch.object(
                        ingestion_cli, "synchronize_document",
                        side_effect=synchronize,
                    ),
                ):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, failure + " failed"):
                            ingestion_cli.run_ingestion(args, entries, {})
                    else:
                        report = ingestion_cli.run_ingestion(args, entries, {})
                        self.assertEqual(report["documents"], 1)
                expected = ["embedding"] + ([] if skip_adls else ["upload"]) + ["index"]
                if failure:
                    expected = expected[:expected.index(failure) + 1]
                self.assertEqual(calls, expected)
                for name in ("credential", "layout", "embedding"):
                    clients[name].__exit__.assert_called_once()
                if not skip_adls:
                    make_storage.assert_called_once()
                    clients["storage"].__exit__.assert_called_once()
                else:
                    make_storage.assert_not_called()
                make_admin.assert_called_once()
                make_search.assert_called_once()
                clients["admin"].__exit__.assert_called_once()
                clients["search"].__exit__.assert_called_once()


class IncrementalIngestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.data = self.folder / "data"
        self.global_pdf = local_pdf(self.data)
        local_pdf(self.data, "finland/local.pdf")
        self.args = ingestion_arguments(self.folder)
        self.state_path = self.args.output_dir / "ingestion-state.json"
        self.schema = index_schema(
            index_name=self.args.index_name,
            openai_endpoint=self.args.openai_endpoint,
            embedding_deployment=self.args.embedding_deployment,
            dimensions=self.args.embedding_dimensions,
        )
        self.rows = {}
        self.server_schema = None
        self.actions = []
        self.page_text = {}
        self.fail_document = None
        self.clients = {}
        self.factories = {}
        for constructor in (
            "AzureCliCredential", "SearchIndexClient", "SearchClient",
            "DocumentIntelligenceClient", "AzureOpenAI", "FileSystemClient",
        ):
            client = MagicMock()
            client.__enter__.return_value = client
            client.__exit__.return_value = False
            self.clients[constructor] = client
            self.factories[constructor] = self.enterContext(
                patch.object(ingestion_cli, constructor, return_value=client),
            )
        self.clients["SearchClient"].search.side_effect = (
            AssertionError("Unexpected query outside the per-PDF key lookup")
        )
        self.clients["SearchIndexClient"].get_index.side_effect = self._get_index
        self.read = self.enterContext(patch.object(
            search_index, "_document_chunk_ids",
            side_effect=lambda client, document_id: set(self._document_rows(document_id)),
        ))
        self.analyze = self.enterContext(patch.object(
            ingestion_cli, "load_or_analyze_pages",
            side_effect=lambda **kwargs: [
                Page(
                    0, 0,
                    self.page_text.get(kwargs["source"]["documentId"], "Policy applies."),
                )
            ],
        ))
        self.embed = self.enterContext(patch.object(
            ingestion_cli, "embed_documents", side_effect=self._embed,
        ))
        self.upload = self.enterContext(patch.object(ingestion_cli, "upload_adls_content"))
        self.create = self.enterContext(patch.object(
            ingestion_cli, "create_index", side_effect=self._create_index,
        ))
        self.write = self.enterContext(patch.object(
            search_index, "_write_actions", side_effect=self._write_actions,
        ))
        self.enterContext(patch("builtins.print"))
        self.enterContext(patch(
            "requests.Session.send", side_effect=AssertionError("Unexpected network call"),
        ))

    def _embed(self, *, documents, **kwargs):
        for document in documents:
            document["chunk_vector"] = [0.5, 0.5]

    def _get_index(self, name):
        if self.server_schema is None:
            raise ResourceNotFoundError("missing")
        return Mock(as_dict=lambda: json.loads(json.dumps(self.server_schema)))

    def _create_index(self, *, action, schema, **kwargs):
        if action == "rebuild":
            self.rows.clear()
        self.server_schema = json.loads(json.dumps(schema))

    def _write_actions(self, sender_factory, actions):
        applied = 0
        for action in actions:
            if (
                action["@search.action"] == "upload"
                and action["document_id"] == self.fail_document
            ):
                if applied:
                    raise RuntimeError("Partial upload failed")
                applied += 1
            self.actions.append(action.copy())
            if action["@search.action"] == "delete":
                self.rows.pop(action["chunk_id"], None)
            else:
                self.rows[action["chunk_id"]] = {
                    key: value for key, value in action.items() if key != "@search.action"
                }

    def _run(self):
        return ingestion_cli.run_ingestion(
            self.args, sources.discover_sources(self.data), self.schema,
        )

    def _reset_activity(self):
        self.actions.clear()
        for mock in (
            self.read, self.analyze, self.embed, self.upload, self.create, self.write,
            *self.factories.values(),
        ):
            mock.reset_mock()

    def _source(self, region):
        return next(
            source for source in sources.discover_sources(self.data)
            if source["region"] == region
        )

    def _document_rows(self, document_id):
        return {
            key: row.copy() for key, row in self.rows.items()
            if row["document_id"] == document_id
        }

    def _saved_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_unchanged_pdfs_do_not_create_clients_or_read_search(self):
        self._run()
        before = self.rows.copy()
        state_before = self.state_path.read_bytes()
        self._reset_activity()
        self.data = self.data.rename(self.folder / "relocated-data")
        report = self._run()
        self.assertEqual(report["processedDocuments"], 0)
        self.assertEqual(report["skippedDocuments"], 2)
        self.assertEqual(report["chunks"], len(before))
        self.assertEqual(self.rows, before)
        for mock in (self.read, self.analyze, self.embed, self.upload, self.create, self.write):
            mock.assert_not_called()
        for factory in self.factories.values():
            factory.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), state_before)
        self.assertEqual(report["removedStaleChunks"], 0)
        self.assertNotIn("validation", report)

    def test_file_hashes_are_saved_once_per_pdf_and_not_uploaded_to_search(self):
        self._run()
        saved = self._saved_state()
        self.assertEqual(saved["pending_document_ids"], [])
        self.assertEqual(len(saved["files"]), 2)
        for source in sources.discover_sources(self.data):
            file = saved["files"][source["documentId"]]
            self.assertEqual(file["sha256"], source["sha256"])
            self.assertEqual(file["version"], source["version"])
            self.assertEqual(
                file["chunk_count"], len(self._document_rows(source["documentId"])),
            )
            self.assertNotIn("chunk_ids", file)
        for row in self.rows.values():
            self.assertNotIn("ingestion_hash", row)
            self.assertNotIn("document_chunk_count", row)
        field_names = {field["name"] for field in self.schema["fields"]}
        self.assertNotIn("ingestion_hash", field_names)
        self.assertNotIn("document_chunk_count", field_names)

    def test_changed_pdf_is_the_only_one_processed_and_old_chunks_are_removed(self):
        self._run()
        global_id = self._source("global")["documentId"]
        local_id = self._source("finland")["documentId"]
        previous = set(self._document_rows(global_id))
        unchanged = self._document_rows(local_id)
        self._reset_activity()
        self.global_pdf.write_bytes(b"%PDF-updated-policy")
        report = self._run()
        self.assertEqual(report["processedDocuments"], 1)
        self.assertEqual(report["skippedDocuments"], 1)
        self.assertEqual(report["removedStaleChunks"], len(previous))
        self.assertEqual(self._document_rows(local_id), unchanged)
        self.assertFalse(previous & self.rows.keys())
        self.analyze.assert_called_once()
        self.assertEqual(self.analyze.call_args.kwargs["source"]["documentId"], global_id)
        self.assertEqual(
            {doc["document_id"] for doc in self.embed.call_args.kwargs["documents"]},
            {global_id},
        )
        self.assertEqual(self.upload.call_args.kwargs["source"]["documentId"], global_id)
        self.read.assert_called_once_with(self.clients["SearchClient"], global_id)

    def test_new_pdf_is_added_without_reprocessing_existing_pdfs(self):
        self._run()
        before = self.rows.copy()
        self._reset_activity()
        local_pdf(self.data, "india/new.pdf")
        added_id = self._source("india")["documentId"]
        report = self._run()
        self.assertEqual(report["documents"], 3)
        self.assertEqual(report["processedDocuments"], 1)
        self.assertEqual(report["skippedDocuments"], 2)
        self.assertEqual({key: self.rows[key] for key in before}, before)
        self.assertTrue(all(action["document_id"] == added_id for action in self.actions))
        self.assertEqual(report["removedStaleChunks"], 0)
        self.read.assert_called_once_with(self.clients["SearchClient"], added_id)

    def test_unrelated_index_records_are_never_queried_or_deleted(self):
        self._run()
        foreign = {"chunk_id": "foreign", "document_id": "another-corpus"}
        self.rows["foreign"] = foreign
        self.global_pdf.write_bytes(b"%PDF-updated-policy")
        global_id = self._source("global")["documentId"]
        self._reset_activity()
        self._run()
        self.assertEqual(self.rows["foreign"], foreign)
        self.read.assert_called_once_with(self.clients["SearchClient"], global_id)

    def test_deleted_pdf_removes_only_its_records(self):
        self._run()
        global_id = self._source("global")["documentId"]
        local_id = self._source("finland")["documentId"]
        removed = set(self._document_rows(global_id))
        unchanged = self._document_rows(local_id)
        self._reset_activity()
        self.global_pdf.unlink()
        report = self._run()
        self.assertEqual(report["processedDocuments"], 0)
        self.assertEqual(report["skippedDocuments"], 1)
        self.assertEqual(self.rows, unchanged)
        self.assertEqual(
            self.actions,
            [{"@search.action": "delete", "chunk_id": key} for key in sorted(removed)],
        )
        for mock in (self.analyze, self.embed, self.upload):
            mock.assert_not_called()
        self.read.assert_called_once_with(self.clients["SearchClient"], global_id)

    def test_partial_upload_is_retried_without_reprocessing_unchanged_pdfs(self):
        self._run()
        global_id = self._source("global")["documentId"]
        local_id = self._source("finland")["documentId"]
        old_keys = set(self._document_rows(global_id))
        unchanged = self._document_rows(local_id)
        self.global_pdf.write_bytes(b"%PDF-more-policy-content")
        self.page_text[global_id] = "Policy applies. " * 300
        self.fail_document = global_id
        with self.assertRaisesRegex(RuntimeError, "Partial upload"):
            self._run()
        saved = self._saved_state()
        self.assertEqual(saved["pending_document_ids"], [global_id])
        self.assertNotEqual(saved["files"][global_id]["sha256"], self._source("global")["sha256"])
        self.assertIn(local_id, saved["files"])
        partial = self._document_rows(global_id)
        self.assertTrue(old_keys <= partial.keys())
        self.assertEqual(len(partial.keys() - old_keys), 1)
        self.assertEqual(self._document_rows(local_id), unchanged)
        self.fail_document = None
        self._reset_activity()
        report = self._run()
        self.assertEqual(report["processedDocuments"], 1)
        self.assertEqual(report["skippedDocuments"], 1)
        self.assertEqual(self._document_rows(local_id), unchanged)
        self.assertFalse(old_keys & self.rows.keys())
        completed = self._document_rows(global_id)
        self.assertGreater(len(completed), 1)
        self.assertEqual(self._saved_state()["pending_document_ids"], [])
        self.assertEqual(
            self._saved_state()["files"][global_id]["chunk_count"], len(completed),
        )
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 0)
        self.write.assert_not_called()

    def test_missing_or_corrupt_state_reprocesses_files_and_rebuilds_local_state(self):
        self._run()
        for damaged in (None, "{", "[]", '{"schema_version": 999}', b"\xff"):
            with self.subTest(state=damaged):
                if damaged is None:
                    self.state_path.unlink()
                elif isinstance(damaged, bytes):
                    self.state_path.write_bytes(damaged)
                else:
                    self.state_path.write_text(damaged, encoding="utf-8")
                self._reset_activity()
                self.assertEqual(self._run()["processedDocuments"], 2)
                self.assertEqual(self._saved_state()["pending_document_ids"], [])
                self._reset_activity()
                self.assertEqual(self._run()["processedDocuments"], 0)
                self.read.assert_not_called()
                self.write.assert_not_called()

    def test_failed_cleanup_retries_the_changed_pdf_without_reprocessing_others(self):
        self._run()
        global_id = self._source("global")["documentId"]
        old_rows = self._document_rows(global_id)
        self.global_pdf.write_bytes(b"%PDF-updated-policy")

        def fail_deletion(sender_factory, actions):
            if any(action["@search.action"] == "delete" for action in actions):
                raise RuntimeError("deletion failed")
            self._write_actions(sender_factory, actions)

        with (
            patch.object(search_index, "_write_actions", side_effect=fail_deletion),
            self.assertRaisesRegex(RuntimeError, "deletion failed"),
        ):
            self._run()
        self.assertEqual(self._saved_state()["pending_document_ids"], [global_id])
        self._reset_activity()
        report = self._run()
        self.assertEqual(report["processedDocuments"], 1)
        self.assertEqual(report["removedStaleChunks"], len(old_rows))
        self.assertEqual(
            [action for action in self.actions if action["@search.action"] == "delete"],
            [{"@search.action": "delete", "chunk_id": key} for key in sorted(old_rows)],
        )
        self.assertEqual(self._saved_state()["pending_document_ids"], [])

    def test_failed_deletion_only_run_is_retried_even_without_new_file_changes(self):
        self._run()
        global_id = self._source("global")["documentId"]
        self.global_pdf.unlink()
        with (
            patch.object(search_index, "_write_actions", side_effect=RuntimeError("delete failed")),
            self.assertRaisesRegex(RuntimeError, "delete failed"),
        ):
            self._run()
        self.assertEqual(self._saved_state()["pending_document_ids"], [global_id])
        self._reset_activity()
        report = self._run()
        self.assertEqual(report["processedDocuments"], 0)
        self.assertFalse(self._document_rows(global_id))
        for mock in (self.analyze, self.embed, self.upload):
            mock.assert_not_called()
        self.assertEqual(self._saved_state()["pending_document_ids"], [])

    def test_reverted_pdf_after_partial_failure_is_not_mistaken_for_completed_work(self):
        self._run()
        original_pdf = self.global_pdf.read_bytes()
        original_keys = set(self.rows)
        global_id = self._source("global")["documentId"]
        self.global_pdf.write_bytes(b"%PDF-updated-policy")
        self.page_text[global_id] = "Policy applies. " * 300
        self.fail_document = global_id
        with self.assertRaisesRegex(RuntimeError, "Partial upload"):
            self._run()
        self.global_pdf.write_bytes(original_pdf)
        self.page_text.pop(global_id)
        self.fail_document = None
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 1)
        self.assertEqual(set(self.rows), original_keys)
        self.assertEqual(self._saved_state()["pending_document_ids"], [])

    def test_new_pdf_removed_after_partial_failure_is_still_cleaned_up(self):
        self._run()
        new_pdf = local_pdf(self.data, "india/new.pdf")
        new_id = self._source("india")["documentId"]
        self.page_text[new_id] = "Policy applies. " * 300
        self.fail_document = new_id
        with self.assertRaisesRegex(RuntimeError, "Partial upload"):
            self._run()
        self.assertNotIn(new_id, self._saved_state()["files"])
        self.assertEqual(self._saved_state()["pending_document_ids"], [new_id])
        self.assertTrue(self._document_rows(new_id))
        new_pdf.unlink()
        self.fail_document = None
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 0)
        self.assertFalse(self._document_rows(new_id))
        self.assertEqual(self._saved_state()["pending_document_ids"], [])
        self.read.assert_called_once_with(self.clients["SearchClient"], new_id)
        self.analyze.assert_not_called()

    def test_successful_pdf_is_saved_even_when_a_later_pdf_fails(self):
        self._run()
        entries = sources.discover_sources(self.data)
        first_id, second_id = (source["documentId"] for source in entries)
        for source in entries:
            Path(source["localPath"]).write_bytes(b"%PDF-updated-policy")
        self.page_text[second_id] = "Policy applies. " * 300
        self.fail_document = second_id
        with self.assertRaisesRegex(RuntimeError, "Partial upload"):
            self._run()
        saved = self._saved_state()
        self.assertEqual(saved["pending_document_ids"], [second_id])
        self.assertEqual(
            saved["files"][first_id]["sha256"], sources.discover_sources(self.data)[0]["sha256"],
        )
        self.fail_document = None
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 1)
        self.read.assert_called_once_with(self.clients["SearchClient"], second_id)
        self.analyze.assert_called_once()

    def test_each_pdf_finishes_and_is_saved_before_the_next_is_analyzed(self):
        events = []
        original_analyze = self.analyze.side_effect
        save = ingestion_cli.save_file_records

        def analyze(**kwargs):
            events.append(("analyze", kwargs["source"]["documentId"]))
            return original_analyze(**kwargs)

        def embed(**kwargs):
            events.append(("embed", kwargs["documents"][0]["document_id"]))
            self._embed(**kwargs)

        def upload(**kwargs):
            events.append(("adls", kwargs["source"]["documentId"]))

        def write(sender_factory, actions):
            if actions and actions[0]["@search.action"] == "upload":
                events.append(("search", actions[0]["document_id"]))
            self._write_actions(sender_factory, actions)

        def save_records(path, records):
            save(path, records)
            if records.files:
                events.append(("save", next(reversed(records.files))))

        self.analyze.side_effect = analyze
        self.embed.side_effect = embed
        self.upload.side_effect = upload
        self.write.side_effect = write
        with patch.object(ingestion_cli, "save_file_records", side_effect=save_records):
            self._run()
        self.assertEqual(
            events,
            [
                (stage, source["documentId"])
                for source in sources.discover_sources(self.data)
                for stage in ("analyze", "embed", "adls", "search", "save")
            ],
        )
        self.create.assert_called_once()
        for constructor in self.factories.values():
            constructor.assert_called_once()
        for client in self.clients.values():
            client.__exit__.assert_called_once()

    def test_later_analysis_failure_preserves_completed_files_and_stops_the_loop(self):
        self._run()
        local_pdf(self.data, "india/new.pdf")
        for source in sources.discover_sources(self.data):
            Path(source["localPath"]).write_bytes(b"%PDF-updated-policy")
        entries = sources.discover_sources(self.data)
        first_id, second_id, third_id = (source["documentId"] for source in entries)
        original_analyze = self.analyze.side_effect

        def fail_second(**kwargs):
            if kwargs["source"]["documentId"] == second_id:
                raise RuntimeError("analysis failed")
            return original_analyze(**kwargs)

        self._reset_activity()
        with (
            patch.object(
                ingestion_cli, "load_or_analyze_pages", side_effect=fail_second,
            ) as failed_analysis,
            self.assertRaisesRegex(RuntimeError, "analysis failed"),
        ):
            self._run()
        saved = self._saved_state()
        self.assertEqual(saved["files"][first_id]["sha256"], entries[0]["sha256"])
        self.assertEqual(set(saved["pending_document_ids"]), {second_id, third_id})
        self.assertEqual(
            [call.kwargs["source"]["documentId"] for call in failed_analysis.call_args_list],
            [first_id, second_id],
        )
        self.read.assert_called_once_with(self.clients["SearchClient"], first_id)
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 2)
        self.assertEqual(
            {call.kwargs["source"]["documentId"] for call in self.analyze.call_args_list},
            {second_id, third_id},
        )

    def test_state_write_failure_prevents_index_changes(self):
        self._run()
        before = self.rows.copy()
        state_before = self.state_path.read_bytes()
        self.global_pdf.write_bytes(b"%PDF-updated-policy")
        self._reset_activity()
        with (
            patch.object(ingestion_cli, "save_file_records", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            self._run()
        self.create.assert_not_called()
        self.write.assert_not_called()
        self.assertEqual(self.rows, before)
        self.assertEqual(self.state_path.read_bytes(), state_before)

    def test_completion_write_failure_leaves_the_changed_pdf_retryable(self):
        self._run()
        global_id = self._source("global")["documentId"]
        self.global_pdf.write_bytes(b"%PDF-updated-policy")
        save = ingestion_cli.save_file_records

        def fail_completion(path, value):
            if not value.pending_document_ids:
                raise OSError("completion write failed")
            save(path, value)

        with (
            patch.object(ingestion_cli, "save_file_records", side_effect=fail_completion),
            self.assertRaisesRegex(OSError, "completion write failed"),
        ):
            self._run()
        self.assertEqual(self._saved_state()["pending_document_ids"], [global_id])
        self.assertNotEqual(
            self._saved_state()["files"][global_id]["sha256"], self._source("global")["sha256"],
        )
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 1)
        self.assertEqual(self._saved_state()["pending_document_ids"], [])

    def test_changing_output_directory_starts_a_new_local_processing_record(self):
        self._run()
        self.args.output_dir = self.folder / "new-output"
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 2)
        self.assertTrue((self.args.output_dir / "ingestion-state.json").is_file())

    def test_changing_search_target_or_storage_reprocesses_the_files(self):
        self._run()
        for setting, value in (
            ("search_endpoint", "https://other.search.windows.net"),
            ("index_name", "other-index"),
            ("storage_file_system", "other-docs"),
        ):
            with self.subTest(setting=setting):
                setattr(self.args, setting, value)
                if setting == "index_name":
                    self.schema["name"] = value
                self._reset_activity()
                self.assertEqual(self._run()["processedDocuments"], 2)
                self._reset_activity()
                self.assertEqual(self._run()["processedDocuments"], 0)
                self.read.assert_not_called()

    def test_parser_version_changes_are_detected_with_unchanged_pdf_bytes(self):
        self._run()
        hashes = {
            key: value["sha256"] for key, value in self._saved_state()["files"].items()
        }
        with patch.object(sources, "PARSER_VERSION", sources.PARSER_VERSION + 1):
            self._reset_activity()
            self.assertEqual(self._run()["processedDocuments"], 2)
        self.assertEqual(
            hashes,
            {key: value["sha256"] for key, value in self._saved_state()["files"].items()},
        )

    def test_new_processing_version_reprocesses_existing_pdfs(self):
        self._run()
        with patch.object(ingestion_cli, "INGESTION_VERSION", ingestion_cli.INGESTION_VERSION + 1):
            self._reset_activity()
            self.assertEqual(self._run()["processedDocuments"], 2)
            self._reset_activity()
            self.assertEqual(self._run()["processedDocuments"], 0)

    def test_processing_setting_changes_do_not_lose_deleted_pdf_tracking(self):
        self._run()
        removed_id = self._source("global")["documentId"]
        self.global_pdf.unlink()
        with patch.object(ingestion_cli, "INGESTION_VERSION", ingestion_cli.INGESTION_VERSION + 1):
            self._reset_activity()
            self.assertEqual(self._run()["processedDocuments"], 1)
        self.assertFalse(self._document_rows(removed_id))
        self.assertNotIn(removed_id, self._saved_state()["files"])
        self.assertEqual(
            {call.args[1] for call in self.read.call_args_list},
            {removed_id, self._source("finland")["documentId"]},
        )

    def test_enabling_pdf_uploads_after_skipping_them_processes_the_pdfs(self):
        self.args.skip_adls_upload = True
        self._run()
        self.upload.assert_not_called()
        self.args.skip_adls_upload = False
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 2)
        self.assertEqual(self.upload.call_count, 2)
        self.assertEqual(
            {call.kwargs["source"]["documentId"] for call in self.upload.call_args_list},
            {source["documentId"] for source in sources.discover_sources(self.data)},
        )

    def test_recreate_index_explicitly_processes_every_pdf(self):
        self._run()
        self.args.recreate_index = True
        self._reset_activity()
        report = self._run()
        self.assertEqual(report["processedDocuments"], 2)
        self.assertEqual(report["skippedDocuments"], 0)
        self.assertEqual(self.create.call_args.kwargs["action"], "rebuild")
        self.assertEqual(len(self.rows), report["chunks"])

    def test_mutable_schema_updates_do_not_reprocess_unchanged_pdfs(self):
        self._run()
        before = self.rows.copy()
        files = self._saved_state()["files"]
        self.schema["fields"][2]["retrievable"] = False
        self.schema["semantic"]["configurations"][0]["rankingOrder"] = "BoostedRerankerScore"
        self._reset_activity()
        report = self._run()
        self.assertEqual(report["processedDocuments"], 0)
        self.assertEqual(self.create.call_args.kwargs["action"], "update")
        self.assertEqual(self.rows, before)
        self.assertEqual(self._saved_state()["files"], files)
        for mock in (self.analyze, self.embed, self.upload, self.read, self.write):
            mock.assert_not_called()
        for name in ("DocumentIntelligenceClient", "AzureOpenAI", "FileSystemClient"):
            self.factories[name].assert_not_called()
        self.assertEqual(self._saved_state()["settings"]["schema"], self.schema)
        self._reset_activity()
        self._run()
        for factory in self.factories.values():
            factory.assert_not_called()

    def test_adding_a_field_backfills_without_deleting_the_index(self):
        self._run()
        foreign = {"chunk_id": "foreign", "document_id": "another-corpus"}
        self.rows["foreign"] = foreign
        self.schema["fields"].append({"name": "new_metadata", "type": "Edm.String"})
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 2)
        self.assertEqual(self.create.call_args.kwargs["action"], "backfill")
        self.assertEqual(self.rows["foreign"], foreign)
        self.assertEqual(self.analyze.call_count, 2)

    def test_rebuild_marks_all_pdfs_before_deletion_and_reprocesses_unchanged_files(self):
        self._run()
        self.server_schema["fields"].append({"name": "legacy", "type": "Edm.String"})
        self.global_pdf.write_bytes(b"%PDF-updated-policy")
        create = self._create_index

        def verify_before_rebuild(**kwargs):
            saved = self._saved_state()
            self.assertEqual(saved["files"], {})
            self.assertEqual(
                set(saved["pending_document_ids"]),
                {source["documentId"] for source in sources.discover_sources(self.data)},
            )
            create(**kwargs)

        self.create.side_effect = verify_before_rebuild
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 2)
        self.assertEqual(self.create.call_args.kwargs["action"], "rebuild")
        self.assertEqual(self.analyze.call_count, 2)

    def test_failed_schema_only_update_is_retried_without_pdf_processing(self):
        self._run()
        previous_settings = self._saved_state()["settings"]
        self.schema["fields"][2]["retrievable"] = False
        self.create.side_effect = HttpResponseError("update failed")
        self._reset_activity()
        with self.assertRaisesRegex(HttpResponseError, "update failed"):
            self._run()
        self.assertEqual(self._saved_state()["settings"], previous_settings)
        self.analyze.assert_not_called()
        self.create.side_effect = self._create_index
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 0)
        self.create.assert_called_once()
        self.assertEqual(self._saved_state()["settings"]["schema"], self.schema)

    def test_failed_rebuild_keeps_every_pdf_retryable_after_index_deletion(self):
        self._run()
        self.schema["fields"][2]["filterable"] = False

        def fail_after_deletion(**kwargs):
            self.rows.clear()
            self.server_schema = None
            raise HttpResponseError("creation failed")

        self.create.side_effect = fail_after_deletion
        with self.assertRaisesRegex(HttpResponseError, "creation failed"):
            self._run()
        saved = self._saved_state()
        self.assertEqual(saved["files"], {})
        self.assertEqual(len(saved["pending_document_ids"]), 2)
        self.create.side_effect = self._create_index
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 2)
        self.assertEqual(self.create.call_args.kwargs["action"], "create")
        self.assertEqual(self._saved_state()["pending_document_ids"], [])

    def test_missing_index_on_a_changed_run_reprocesses_the_whole_corpus(self):
        self._run()
        self.server_schema = None
        self.rows.clear()
        self.global_pdf.write_bytes(b"%PDF-updated-policy")
        self._reset_activity()
        self.assertEqual(self._run()["processedDocuments"], 2)
        self.assertEqual(self.create.call_args.kwargs["action"], "create")


class StorageTests(unittest.TestCase):
    def test_real_sdk_creates_metadata_only_if_version_is_missing(self):
        metadata = {"documentid": "HR-TEST", "version": "1"}

        def send(request, **kwargs):
            action = parse_qs(urlsplit(request.url).query).get("action")
            status = (
                201 if request.method == "PUT" else 202 if action == ["append"] else 200
            )
            return http_response(request, b"", status, headers={"ETag": '"created"'})

        with (
            patch("requests.Session.send", side_effect=send) as transport,
            DataLakeFileClient(
                "https://example.dfs.core.windows.net", "docs", "policy.pdf"
            ) as client,
        ):
            storage._upload_versioned_file(
                client, b"policy", metadata, "application/pdf"
            )

        sent = [call.args[0] for call in transport.call_args_list]
        self.assertEqual([request.method for request in sent], ["PUT", "PATCH", "PATCH"])
        self.assertEqual(sent[0].headers["If-None-Match"], "*")
        properties = dict(
            entry.split("=", 1)
            for entry in sent[0].headers["x-ms-properties"].split(",")
        )
        self.assertEqual(
            {
                name: base64.b64decode(value).decode()
                for name, value in properties.items()
            },
            metadata,
        )
        self.assertEqual(sent[-1].headers["If-Match"], '"created"')
        self.assertEqual(sent[-1].headers["x-ms-content-type"], "application/pdf")

    def test_real_sdk_precondition_failure_verifies_without_replacing_content(self):
        for existing in (b"same", b"different"):
            with self.subTest(existing=existing):
                def send(request, *, existing=existing, **kwargs):
                    if request.method == "PUT":
                        return http_response(
                            request,
                            {
                                "error": {
                                    "code": "ConditionNotMet",
                                    "message": "File exists",
                                }
                            },
                            412,
                            headers={"x-ms-error-code": "ConditionNotMet"},
                        )
                    self.assertEqual(request.method, "GET")
                    return http_response(
                        request,
                        existing,
                        206,
                        headers={
                            "Content-Length": str(len(existing)),
                            "Content-Range": f"bytes 0-{len(existing) - 1}/{len(existing)}",
                            "ETag": '"existing"',
                        },
                    )

                with (
                    patch("requests.Session.send", side_effect=send) as transport,
                    DataLakeFileClient(
                        "https://example.dfs.core.windows.net", "docs", "policy.pdf"
                    ) as client,
                    (
                        nullcontext()
                        if existing == b"same"
                        else self.assertRaisesRegex(ValueError, "will not be overwritten")
                    ),
                ):
                    storage._upload_versioned_file(
                        client,
                        b"same",
                        {"documentid": "HR-TEST", "version": "1"},
                        "application/pdf",
                    )
                sent = [call.args[0] for call in transport.call_args_list]
                self.assertEqual([request.method for request in sent], ["PUT", "GET"])
                self.assertEqual(sent[0].headers["If-None-Match"], "*")

    def test_annotation_whitespace_and_key_order_do_not_require_a_new_version(self):
        client = Mock()
        client.upload_data.side_effect = ResourceExistsError("exists")
        client.download_file.return_value.readall.return_value = (
            b'{ "pages": {}, "version": "1" }'
        )
        storage._upload_versioned_file(
            client,
            b'{"version":"1","pages":{}}',
            {"documentid": "HR-TEST", "version": "1"},
            "application/json; charset=utf-8",
        )
        client.upload_data.assert_called_once()

    def test_existing_different_versioned_content_is_not_overwritten(self):
        client = Mock()
        client.upload_data.side_effect = ResourceExistsError("exists")
        client.download_file.return_value.readall.return_value = b"old"
        with self.assertRaisesRegex(ValueError, "will not be overwritten"):
            storage._upload_versioned_file(
                client,
                b"new",
                {"documentid": "HR-TEST", "version": "1"},
                "application/pdf",
            )
        self.assertIs(
            client.upload_data.call_args.kwargs["match_condition"],
            MatchConditions.IfMissing,
        )

    def test_identical_versioned_content_is_idempotent(self):
        client = Mock()
        client.upload_data.side_effect = ResourceExistsError("exists")
        client.download_file.return_value.readall.return_value = b"same"
        storage._upload_versioned_file(client, b"same", {}, "application/pdf")
        client.upload_data.assert_called_once()

    def test_discovered_citation_paths_match_uploaded_pdfs_and_annotations(self):
        file_system = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path = local_pdf(folder)
            item = sources.discover_sources(folder)[0]
            pages = [Page(0, 0, "Policy applies.")]
            annotation = storage.annotation_payload(source=item, pages=pages)
            document = build_search_documents(source=item, pages=pages)[0]
            clients = {
                item["documentPath"]: Mock(),
                item["annotationPath"]: Mock(),
            }
            file_system.get_file_client.side_effect = clients.__getitem__
            storage.upload_adls_content(
                file_system=file_system,
                source=item,
                annotation=annotation,
            )
            self.assertEqual(
                clients[document["document_path"]].upload_data.call_args.args[0],
                path.read_bytes(),
            )
            uploaded = json.loads(
                clients[document["annotation_path"]].upload_data.call_args.args[0]
            )
            self.assertEqual(uploaded["documentId"], document["document_id"])
            self.assertEqual(uploaded["version"], document["version"])
            self.assertEqual(document["page_start"], 1)
            self.assertEqual(set(uploaded["pages"]), {"1"})
            self.assertEqual(document["source_uri"], document["document_path"])
            self.assertNotIn(str(folder), json.dumps(uploaded))
            file_system.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()

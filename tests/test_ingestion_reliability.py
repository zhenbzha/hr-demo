"""Mocked failure-path tests: no Azure credentials or network access are needed."""

import base64
import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import validate_document_retrieval as retrieval
from azure.core import MatchConditions
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceExistsError
from azure.storage.filedatalake import DataLakeFileClient
from ingestion import analysis_cache, azure_http, search_index, storage
from ingestion.manifest import load_manifest
from ingestion.page import ContentBlock, Page
from ingestion.search_schema import index_schema
from test_ingestion_contracts import (
    embedding_client,
    http_response,
    policy_metadata,
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
    def test_missing_stale_link_count_is_not_treated_as_zero(self):
        with (
            patch.object(retrieval, "request_json", return_value=(200, {}, {})),
            self.assertRaisesRegex(RuntimeError, "valid Search result count"),
        ):
            retrieval.validate_no_dovetail(
                search_endpoint="https://example.search.windows.net",
                index_name="hr",
                credential=Mock(),
            )

    def test_query_embeddings_use_the_same_validated_embedding_contract(self):
        with patch.object(
            search_index,
            "AzureOpenAI",
            return_value=embedding_client(
                {
                    "model": "text-embedding-3-large",
                    "data": [{"index": 0, "embedding": [0.5, 0.5]}],
                }
            ),
        ):
            vector = retrieval.embed_query(
                endpoint="https://example.openai.azure.com",
                deployment="embedding",
                dimensions=2,
                query="Policy?",
                credential=Mock(),
            )
        self.assertEqual(vector, [0.5, 0.5])

    def test_schema_update_retains_existing_immutable_field_flags(self):
        schema = index_schema(
            index_name="hr",
            openai_endpoint="https://example.openai.azure.com",
            embedding_deployment="embedding",
            dimensions=2,
        )
        existing = json.loads(json.dumps(schema))
        existing["fields"][0]["searchable"] = True
        existing["fields"][0]["facetable"] = True
        existing["fields"] = [
            field for field in existing["fields"] if field["name"] != "semantic_text"
        ]
        with patch(
            "requests.Session.send",
            side_effect=lambda request, **kwargs: http_response(
                request, existing if request.method == "GET" else {}
            ),
        ) as send:
            search_index.create_index(
                endpoint="https://example.search.windows.net",
                index_name="hr",
                schema=schema,
                recreate=False,
                credential=AzureKeyCredential("unit-test"),
            )
        sent_fields = {
            field["name"]: field
            for field in json.loads(send.call_args.args[0].body)["fields"]
        }
        self.assertTrue(sent_fields["chunk_id"]["searchable"])
        self.assertTrue(sent_fields["chunk_id"]["facetable"])
        self.assertIn("semantic_text", sent_fields)
        self.assertFalse(schema["fields"][0]["searchable"])

    def test_unsortable_existing_keys_fail_without_deleting_the_index(self):
        schema = index_schema(
            index_name="hr",
            openai_endpoint="https://example.openai.azure.com",
            embedding_deployment="embedding",
            dimensions=2,
        )
        existing = json.loads(json.dumps(schema))
        existing["fields"][0]["sortable"] = False
        with (
            patch(
                "requests.Session.send",
                side_effect=lambda request, **kwargs: http_response(
                    request, existing
                ),
            ) as send,
            self.assertRaisesRegex(ValueError, "sortable"),
        ):
            search_index.create_index(
                endpoint="https://example.search.windows.net",
                index_name="hr",
                schema=schema,
                recreate=False,
                credential=AzureKeyCredential("unit-test"),
            )
        send.assert_called_once()

    def test_upload_failure_never_deletes_previous_chunks(self):
        with (
            patch.object(
                search_index, "_index_rows", return_value=[{"chunk_id": "old"}]
            ),
            patch.object(
                search_index,
                "upload_documents",
                side_effect=RuntimeError("upload failed"),
            ),
            patch.object(search_index, "_write_actions") as write,
            self.assertRaisesRegex(RuntimeError, "upload failed"),
        ):
            search_index.synchronize_documents(
                endpoint="https://example.search.windows.net",
                index_name="hr",
                documents=[{"chunk_id": "new"}],
                credential=Mock(),
            )
        write.assert_not_called()

    def test_visibility_failure_never_deletes_previous_chunks(self):
        with (
            patch.object(
                search_index, "_index_rows", return_value=[{"chunk_id": "old"}]
            ),
            patch.object(search_index, "upload_documents"),
            patch.object(
                search_index, "_wait_for_keys", side_effect=TimeoutError("not visible")
            ),
            patch.object(search_index, "_write_actions") as write,
            self.assertRaisesRegex(TimeoutError, "not visible"),
        ):
            search_index.synchronize_documents(
                endpoint="https://example.search.windows.net",
                index_name="hr",
                documents=[{"chunk_id": "new"}],
                credential=Mock(),
            )
        write.assert_not_called()

    def test_only_stale_keys_are_pruned_after_upload_visibility(self):
        events = []
        with (
            patch.object(
                search_index,
                "_index_rows",
                return_value=[{"chunk_id": "old"}, {"chunk_id": "same"}],
            ),
            patch.object(
                search_index,
                "upload_documents",
                side_effect=lambda **kwargs: events.append("upload"),
            ),
            patch.object(
                search_index,
                "_wait_for_keys",
                side_effect=lambda *args, **kwargs: events.append(
                    "exact" if kwargs["exact"] else "visible"
                ),
            ),
            patch.object(
                search_index,
                "_write_actions",
                side_effect=lambda *args: events.append(args[2]),
            ),
        ):
            removed = search_index.synchronize_documents(
                endpoint="https://example.search.windows.net",
                index_name="hr",
                documents=[{"chunk_id": "new"}, {"chunk_id": "same"}],
                credential=Mock(),
            )
        self.assertEqual(removed, 1)
        self.assertEqual(
            events,
            [
                "upload",
                "visible",
                [{"@search.action": "delete", "chunk_id": "old"}],
                "exact",
            ],
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
                endpoint="https://example.search.windows.net",
                index_name="hr",
                documents=[{"chunk_id": "one"}, {"chunk_id": "two"}],
                credential=AzureKeyCredential("unit-test"),
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
                "https://example.search.windows.net",
                "hr",
                [{"@search.action": "delete", "chunk_id": "stale"}],
                AzureKeyCredential("unit-test"),
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
                endpoint="https://example.search.windows.net",
                index_name="hr",
                documents=documents,
                credential=AzureKeyCredential("unit-test"),
            )
        self.assertTrue(
            all(len(batch) <= search_index.INDEX_BATCH_SIZE for batch in batches)
        )
        self.assertCountEqual(
            [key for batch in batches for key in batch],
            [document["chunk_id"] for document in documents],
        )

    def test_keyset_pagination_continues_until_empty(self):
        pages = iter(
            [
                {"value": [{"chunk_id": "a"}, {"chunk_id": "b"}]},
                {"value": [{"chunk_id": "c"}]},
                {"value": []},
            ]
        )
        with patch(
            "requests.Session.send",
            side_effect=lambda request, **kwargs: http_response(request, next(pages)),
        ) as send:
            rows = search_index._index_rows(
                "https://example.search.windows.net",
                "hr",
                AzureKeyCredential("unit-test"),
            )
        self.assertEqual([row["chunk_id"] for row in rows], ["a", "b", "c"])
        self.assertEqual(
            json.loads(send.call_args_list[1].args[0].body)["filter"], "chunk_id gt 'b'"
        )

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
                endpoint="https://example.search.windows.net",
                index_name="hr",
                documents=documents,
                credential=AzureKeyCredential("unit-test"),
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
                endpoint="https://example.search.windows.net",
                index_name="hr",
                documents=[{"chunk_id": "one"}, {"chunk_id": "two"}],
                credential=AzureKeyCredential("unit-test"),
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


class ManifestAndCacheTests(unittest.TestCase):
    def _manifest(self, folder: Path, **overrides) -> tuple[Path, dict]:
        source = folder / "policy.pdf"
        source.write_bytes(b"%PDF-local-fixture")
        item = {
            **policy_metadata(),
            "name": source.name,
            "localPath": source.name,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "isActive": True,
            "effectiveDate": "2026-01-01T00:00:00Z",
            "reviewDate": "2027-01-01T00:00:00Z",
            **overrides,
        }
        item.pop("documentPath")
        item.pop("annotationPath")
        path = folder / "manifest.json"
        path.write_text(json.dumps({"documents": [item]}), encoding="utf-8")
        return path, item

    def test_manifest_paths_are_explicit_and_original_is_not_mutated(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            path, _ = self._manifest(folder)
            manifest, active = load_manifest(path)
            self.assertEqual(manifest["documents"][0]["localPath"], "policy.pdf")
            self.assertEqual(active[0]["localPath"], str(folder / "policy.pdf"))
            self.assertNotIn(
                "annotationPath", json.loads(path.read_text())["documents"][0]
            )

    def test_invalid_path_segments_and_non_boolean_active_are_rejected(self):
        for override in (
            {"version": "../other"},
            {"isActive": "true"},
            {"sha256": "wrong"},
        ):
            with (
                tempfile.TemporaryDirectory() as temporary,
                self.subTest(override=override),
            ):
                path, _ = self._manifest(Path(temporary), **override)
                with self.assertRaises(ValueError):
                    load_manifest(path)

    def test_inactive_source_need_not_be_present(self):
        with tempfile.TemporaryDirectory() as temporary:
            path, item = self._manifest(Path(temporary))
            inactive = {
                **item,
                "documentId": "HR-INACTIVE",
                "isActive": False,
                "localPath": "removed.pdf",
            }
            path.write_text(json.dumps({"documents": [item, inactive]}))
            self.assertEqual(len(load_manifest(path)[1]), 1)

    def test_legacy_exclusions_are_rejected_rather_than_silently_ignored(self):
        for settings in (
            {"excludePages": [1]},
            {"excludeSections": ["Employee acknowledgement"]},
        ):
            for scope in ("manifest", "document"):
                with (
                    tempfile.TemporaryDirectory() as temporary,
                    self.subTest(scope=scope, settings=settings),
                ):
                    path, item = self._manifest(Path(temporary))
                    manifest = {"documents": [item]}
                    if scope == "manifest":
                        manifest["ingestion"] = settings
                    else:
                        item.update(settings)
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValueError, "exclusions are not supported"
                    ):
                        load_manifest(path)

    def test_cache_reuses_matching_source_and_rejects_old_or_changed_parsers(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            _, item = self._manifest(folder)
            source, cache = folder / "policy.pdf", folder / "cache.json"
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
                "path": source,
                "output_path": cache,
                "retain_analysis": True,
                "document_id": item["documentId"],
                "document_version": item["version"],
                "source_sha256": item["sha256"],
            }
            with patch.object(
                analysis_cache, "analyze_local_pdf", return_value=[page]
            ) as analyze:
                analysis_cache.load_or_analyze_pages(**arguments, reuse_analysis=False)
                result = analysis_cache.load_or_analyze_pages(
                    **arguments, reuse_analysis=True
                )
                self.assertEqual(result, [page])
                analyze.assert_called_once()
                with (
                    patch.object(analysis_cache, "PARSER_VERSION", 999),
                    self.assertRaisesRegex(ValueError, "stale"),
                ):
                    analysis_cache.load_or_analyze_pages(
                        **arguments, reuse_analysis=True
                    )
                payload = json.loads(cache.read_text(encoding="utf-8"))
                for excluded_pages in ([], [1]):
                    with self.subTest(excluded_pages=excluded_pages):
                        payload["metadata"].update(
                            {
                                "schemaVersion": 3,
                                "parserVersion": 3,
                                "excludePages": excluded_pages,
                                "excludeSections": [],
                            }
                        )
                        cache.write_text(json.dumps(payload), encoding="utf-8")
                        with self.assertRaisesRegex(
                            ValueError, "Reanalyze without --reuse-analysis"
                        ):
                            analysis_cache.load_or_analyze_pages(
                                **arguments, reuse_analysis=True
                            )
                analyze.assert_called_once()

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

    def test_manifest_inventory_does_not_expose_local_paths(self):
        service = Mock()
        service.__enter__ = Mock(return_value=service)
        service.__exit__ = Mock(return_value=False)
        file_client = (
            service.get_file_system_client.return_value.get_file_client.return_value
        )
        item = {**policy_metadata(), "localPath": "C:\\private\\policy.pdf"}
        with patch.object(storage, "DataLakeServiceClient", return_value=service):
            storage.publish_manifest(
                endpoint="https://example.dfs.core.windows.net",
                file_system_name="docs",
                credential=Mock(),
                manifest={"documentRoot": "C:\\private", "documents": [item]},
                active_items=[item],
            )
        uploaded = json.loads(file_client.upload_data.call_args.args[0])
        self.assertNotIn("documentRoot", uploaded)
        self.assertNotIn("localPath", uploaded["documents"][0])
        self.assertEqual(uploaded["documents"][0]["documentPath"], item["documentPath"])


if __name__ == "__main__":
    unittest.main()

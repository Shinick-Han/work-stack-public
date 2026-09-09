from __future__ import annotations

import json
import unittest
from pathlib import Path

# jsonschema==4.26.0 is already declared in the repository requirements.txt.
# It is imported unconditionally on purpose: the schema is closed evidence, so
# a missing validator must fail the suite rather than silently skip it.
from jsonschema import Draft202012Validator, FormatChecker

from mapping import (
    DECLARED_SOURCE_TYPES,
    MAX_CHUNK_POSITION,
    MAX_CONNECTION_ID_CHARS,
    MAX_EXCERPT_CHARS,
    MAX_HEADING_CHARS,
    MAX_HEADING_SEGMENTS,
    MAX_QUERY_CHARS,
    looks_like_filesystem_path,
    map_opendocuments_chat_bound,
)

ROOT = Path(__file__).resolve().parent
CASES = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((ROOT / "proposed-normalized.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

DUMMY_CHAT = {"queryId": "22222222-2222-4222-8222-222222222222", "sources": []}
DOC_A = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee1"
DOC_B = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"


def _load(relative: str) -> object:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _run_case(case: dict) -> dict:
    if "bundle" in case:
        bundle = _load(case["bundle"])
        assert isinstance(bundle, dict)
        return map_opendocuments_chat_bound(
            bundle["request"],
            bundle["chat"],
            bundle.get("documents"),
        )
    request = _load(case["request"])
    chat = _load(case["chat"]) if "chat" in case else DUMMY_CHAT
    documents = _load(case["documents"]) if "documents" in case else None
    assert isinstance(request, dict)
    assert isinstance(chat, dict)
    if documents is not None:
        assert isinstance(documents, dict)
    return map_opendocuments_chat_bound(request, chat, documents)


def _case(name: str) -> dict:
    return next(c for c in CASES["cases"] if c["name"] == name)


def _probe(
    source: dict,
    request_overrides: dict | None = None,
    chat_overrides: dict | None = None,
) -> dict:
    """Map a single synthetic source with no files or network."""
    request = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "connection_id": "conn_opendocuments_pilot_1",
        "query": "probe",
    }
    request.update(request_overrides or {})
    chat = {
        "queryId": "22222222-2222-4222-8222-222222222222",
        "sources": [source],
        "confidence": {"score": 0.5, "level": "medium", "reason": "probe"},
        "route": "rag",
        "profile": "balanced",
    }
    chat.update(chat_overrides or {})
    return map_opendocuments_chat_bound(request, chat)


def _source(**overrides: object) -> dict:
    source = {
        "chunkId": f"{DOC_A}_chunk_0",
        "content": "probe body",
        "score": 0.5,
        "documentId": DOC_A,
    }
    source.update(overrides)
    return source


# The declaration each connector actually writes, paired with the sourcePath
# form that connector actually emits at frozen upstream f3aba15f. This table is
# evidence, not inference: see DECLARED_SOURCE_TYPES in mapping.py for the file
# and line of every entry.
UPSTREAM_DECLARATIONS: tuple[tuple[str, str, str], ...] = (
    ("local", r"Z:\PilotShare\handbook.md", "local"),
    ("web", "https://blog.example.test/post", "url"),
    ("upload", "upload:0123abcd:handbook.md", "upload"),
    ("@opendocuments/connector-notion", "notion://11111111-2222-4333-8444-555555555555", "notion"),
    ("@opendocuments/connector-gdrive", "gdrive://drive-file-id", "gdrive"),
    ("@opendocuments/connector-confluence", "confluence://12345", "confluence"),
    ("@opendocuments/connector-github", "github://acme/repo/docs/a.md", "github"),
    ("@opendocuments/connector-s3", "s3://bucket/key.md", "s3"),
    ("@opendocuments/connector-s3", "gcs://bucket/object.md", "gcs"),
    ("@opendocuments/connector-swagger", "swagger:///pets#get", "swagger"),
    ("@opendocuments/connector-web-crawler", "https://docs.example.test/a", "url"),
    ("@opendocuments/connector-web-search", "https://docs.example.test/b", "url"),
)

# Every boundary and adversarial projection the final review asked to be
# proven schema-valid, plus the secrets that must never survive any of them.
# Each entry is (label, source, request_overrides, chat_overrides, secrets).
BOUNDARY_PROBES: tuple[tuple[str, dict, dict, dict, tuple[str, ...]], ...] = (
    (
        "mixed-case credential url under the real crawler declaration",
        _source(
            sourcePath="HTTPS://alice:secret@example.test/doc?api_key=TOPSECRET",
            sourceType="@opendocuments/connector-web-crawler",
        ),
        {}, {}, ("alice", "TOPSECRET", "api_key", "example.test"),
    ),
    (
        "mixed-case credential url with no declaration at all",
        _source(sourcePath="HtTpS://alice:secret@example.test/doc?api_key=TOPSECRET"),
        {}, {}, ("alice", "TOPSECRET", "api_key"),
    ),
    (
        "mixed-case http scheme spelled without an authority",
        _source(
            sourcePath="HTTPS:alice:secret@example.test/doc?api_key=TOPSECRET",
            sourceType="@opendocuments/connector-web-crawler",
        ),
        {}, {}, ("alice", "TOPSECRET", "api_key"),
    ),
    (
        "mixed-case query-bearing url",
        _source(
            sourcePath="HTTPS://docs.example.test/handbook?token=TOPSECRET#ANCHORSECRET",
            sourceType="@opendocuments/connector-web-crawler",
        ),
        {}, {}, ("TOPSECRET", "token=", "ANCHORSECRET"),
    ),
    (
        "mixed-case opaque gdrive identifier",
        _source(
            sourcePath="GDRIVE://1AbCd-private-file-id",
            sourceType="@opendocuments/connector-gdrive",
        ),
        {}, {}, ("private-file-id", "1AbCd"),
    ),
    (
        "mixed-case notion page id",
        _source(
            sourcePath="NOTION://ABCDEF0123456789ABCDEF0123456789",
            sourceType="@opendocuments/connector-notion",
        ),
        {}, {}, (),
    ),
    (
        "gdrive declaration carrying a valid notion locator",
        _source(
            sourcePath="notion://11111111-2222-4333-8444-555555555555",
            sourceType="@opendocuments/connector-gdrive",
        ),
        {}, {}, ("11111111-2222-4333-8444-555555555555",),
    ),
    (
        "web-crawler declaration carrying a valid notion locator",
        _source(
            sourcePath="notion://11111111-2222-4333-8444-555555555555",
            sourceType="@opendocuments/connector-web-crawler",
        ),
        {}, {}, ("11111111-2222-4333-8444-555555555555",),
    ),
    (
        "s3 declaration carrying a github locator",
        _source(
            sourcePath="github://acme/private-repo/secret.md",
            sourceType="@opendocuments/connector-s3",
        ),
        {}, {}, ("private-repo", "secret.md"),
    ),
    (
        "unknown declaration carrying an otherwise valid notion locator",
        _source(
            sourcePath="notion://11111111-2222-4333-8444-555555555555",
            sourceType="@opendocuments/connector-not-a-real-plugin",
        ),
        {}, {}, ("11111111-2222-4333-8444-555555555555",),
    ),
    (
        "unrecognised scheme embedding credentials",
        _source(sourcePath="ftp://alice:secret@files.example.test/x?api_key=TOPSECRET"),
        {}, {}, ("alice", "TOPSECRET", "api_key"),
    ),
    (
        "chunk position one past the schema ceiling",
        _source(chunkId=f"{DOC_A}_chunk_{MAX_CHUNK_POSITION + 1}"),
        {}, {}, (),
    ),
    (
        "chunk position exactly at the schema ceiling",
        _source(chunkId=f"{DOC_A}_chunk_{MAX_CHUNK_POSITION}"),
        {}, {}, (),
    ),
    (
        "request id with a trailing line feed",
        _source(),
        {"request_id": "11111111-1111-4111-8111-111111111111\n"}, {}, (),
    ),
    (
        "query id with a trailing line feed",
        _source(),
        {}, {"queryId": "22222222-2222-4222-8222-222222222222\n"}, (),
    ),
    (
        "document and chunk ids with a trailing line feed",
        _source(documentId=f"{DOC_A}\n", chunkId=f"{DOC_A}_chunk_0\n"),
        {}, {}, (),
    ),
    (
        "integer score far beyond the float range",
        _source(score=int("9" * 4000)),
        {}, {"confidence": {"score": int("9" * 4000), "level": "high", "reason": "x"}}, (),
    ),
    (
        "nonfinite and boolean scores",
        _source(score=float("nan")),
        {}, {"confidence": {"score": float("inf"), "level": True, "reason": "x"}}, (),
    ),
    (
        "negative and boolean source score",
        _source(score=True),
        {}, {"confidence": {"score": -5, "level": "low", "reason": "x"}}, (),
    ),
)


class CatalogTests(unittest.TestCase):
    def test_catalog_cases_match_frozen_expected(self) -> None:
        self.assertGreaterEqual(len(CASES["cases"]), 14)
        for case in CASES["cases"]:
            with self.subTest(case["name"]):
                self.assertEqual(_run_case(case), _load(case["expected"]))

    def test_every_mapped_output_validates_against_the_schema(self) -> None:
        """A0-4: the schema is closed evidence, not illustration."""
        for case in CASES["cases"]:
            with self.subTest(case["name"]):
                errors = sorted(VALIDATOR.iter_errors(_run_case(case)), key=lambda e: e.path)
                self.assertEqual(
                    [],
                    [f"{list(e.path)}: {e.message}" for e in errors],
                )

    def test_frozen_expected_documents_also_validate(self) -> None:
        for case in CASES["cases"]:
            with self.subTest(case["name"]):
                VALIDATOR.validate(_load(case["expected"]))

    def test_schema_rejects_a_success_body_carrying_an_error(self) -> None:
        """The ok/result/error branches are conditional, not merely optional."""
        happy = _run_case(_case("happy-sanitized-chat"))
        both = dict(happy)
        both["error"] = {"code": "invalid_request", "message": "should not be allowed"}
        self.assertTrue(VALIDATOR.iter_errors(both), "ok:true must forbid an error body")
        refusal = _run_case(_case("out-of-scope-collection-filter"))
        stray = dict(refusal)
        stray["result"] = happy["result"]
        self.assertTrue(VALIDATOR.iter_errors(stray), "ok:false must forbid a result body")


class SourceLocatorTests(unittest.TestCase):
    """A0-1: sourceType never admits a sourcePath."""

    def test_notion_source_type_cannot_smuggle_a_filesystem_path(self) -> None:
        mapped = _probe(
            {
                "chunkId": f"{DOC_A}_chunk_0",
                "content": "hostile",
                "score": 0.5,
                "documentId": DOC_A,
                "sourcePath": "Z:\\Corp\\Secrets\\token.txt",
                "sourceType": "@opendocuments/connector-notion",
            }
        )
        candidate = mapped["result"]["candidates"][0]
        self.assertEqual(candidate["source"]["type_locator_agreement"], "mismatch")
        self.assertEqual(candidate["source"]["locator"]["type"], "redacted")
        self.assertIsNone(candidate["source"]["locator"]["value"])
        blob = json.dumps(mapped)
        for leaked in ("Z:", "Corp", "Secrets", "token.txt"):
            self.assertNotIn(leaked, blob)

    def test_local_source_type_cannot_claim_a_notion_locator(self) -> None:
        mapped = _probe(
            {
                "chunkId": f"{DOC_A}_chunk_0",
                "content": "hostile",
                "documentId": DOC_A,
                "sourcePath": "notion://11111111-2222-4333-8444-555555555555",
                "sourceType": "local",
            }
        )
        locator = mapped["result"]["candidates"][0]["source"]["locator"]
        self.assertEqual(locator["type"], "redacted")
        self.assertEqual(locator["reason"], "source_type_locator_mismatch")

    def test_notion_locator_requires_a_valid_page_id(self) -> None:
        mapped = _probe(
            {
                "chunkId": f"{DOC_A}_chunk_0",
                "content": "hostile",
                "documentId": DOC_A,
                "sourcePath": "notion://../../etc/passwd",
                "sourceType": "@opendocuments/connector-notion",
            }
        )
        locator = mapped["result"]["candidates"][0]["source"]["locator"]
        self.assertEqual(locator["type"], "redacted")
        self.assertEqual(locator["reason"], "malformed_notion_page_id")
        self.assertNotIn("passwd", json.dumps(mapped))

    def test_credential_bearing_url_is_never_retained(self) -> None:
        mapped = _probe(
            {
                "chunkId": f"{DOC_A}_chunk_0",
                "content": "hostile",
                "documentId": DOC_A,
                "sourcePath": "https://alice:secret@example.test/doc?api_key=TOPSECRET",
                "sourceType": "@opendocuments/connector-web-crawler",
            }
        )
        locator = mapped["result"]["candidates"][0]["source"]["locator"]
        self.assertEqual(locator["type"], "redacted")
        self.assertEqual(locator["reason"], "credentialed_url")
        blob = json.dumps(mapped)
        for leaked in ("alice", "secret", "TOPSECRET", "api_key", "example.test"):
            self.assertNotIn(leaked, blob)

    def test_url_query_and_fragment_are_dropped(self) -> None:
        mapped = _probe(
            {
                "chunkId": f"{DOC_A}_chunk_0",
                "content": "hostile",
                "documentId": DOC_A,
                "sourcePath": "https://docs.example.test/handbook?token=SESSIONTOKEN#anchor",
                "sourceType": "@opendocuments/connector-web-crawler",
            }
        )
        locator = mapped["result"]["candidates"][0]["source"]["locator"]
        self.assertEqual(locator["value"], "https://docs.example.test/handbook")
        self.assertEqual(locator["reason"], "url_query_and_fragment_dropped")
        self.assertNotIn("SESSIONTOKEN", json.dumps(mapped))

    def test_unvalidatable_scheme_keeps_only_the_source_class(self) -> None:
        mapped = _probe(
            {
                "chunkId": f"{DOC_A}_chunk_0",
                "content": "hostile",
                "documentId": DOC_A,
                "sourcePath": "gdrive://1AbCdEfGh-private-file-id",
                "sourceType": "@opendocuments/connector-gdrive",
            }
        )
        locator = mapped["result"]["candidates"][0]["source"]["locator"]
        self.assertEqual(locator, {
            "type": "opaque",
            "value": "gdrive",
            "reason": "opaque_scheme_identifier_dropped",
        })
        self.assertNotIn("private-file-id", json.dumps(mapped))

    def test_control_characters_in_a_source_path_redact_it(self) -> None:
        mapped = _probe(
            {
                "chunkId": f"{DOC_A}_chunk_0",
                "content": "hostile",
                "documentId": DOC_A,
                "sourcePath": "notion://1111\u00001111",
                "sourceType": "unknown-connector",
            }
        )
        locator = mapped["result"]["candidates"][0]["source"]["locator"]
        self.assertEqual(locator["type"], "redacted")
        self.assertEqual(locator["reason"], "unsafe_source_path")

    def test_hostile_traversal_does_not_keep_path_or_target_name(self) -> None:
        result = _run_case(_case("hostile-filesystem-path"))
        blob = json.dumps(result)
        for leaked in ("..", "Secrets", "token.txt", "Z:"):
            self.assertNotIn(leaked, blob)
        locator = result["result"]["candidates"][0]["source"]["locator"]
        self.assertEqual(locator["value"], "document")

    def test_filesystem_path_detector(self) -> None:
        self.assertTrue(looks_like_filesystem_path(r"Z:\PilotShare\sample-handbook.md"))
        self.assertTrue(looks_like_filesystem_path("/ready.md"))
        self.assertTrue(looks_like_filesystem_path(r"\\nas\share\file.md"))
        self.assertFalse(looks_like_filesystem_path("notion://11111111-2222-4333-8444-555555555555"))
        self.assertFalse(looks_like_filesystem_path("upload:abcd:notes.md"))
        self.assertFalse(looks_like_filesystem_path("https://example.test/a"))
        # A recognised scheme is a scheme in every spelling; a drive letter is
        # not a recognised scheme.
        self.assertFalse(looks_like_filesystem_path("NOTION://11111111-2222-4333-8444-555555555555"))
        self.assertFalse(looks_like_filesystem_path("HTTPS://example.test/a"))
        self.assertFalse(looks_like_filesystem_path("GDRIVE://x"))
        self.assertTrue(looks_like_filesystem_path(r"C:\Users\a.md"))


class ChunkIdentityTests(unittest.TestCase):
    """A0-2: a heading hierarchy is not an address."""

    def test_source_without_a_chunk_id_is_omitted(self) -> None:
        mapped = _probe(
            {
                "content": "no chunk id but plenty of headings",
                "documentId": DOC_A,
                "headingHierarchy": ["Handbook", "Release"],
                "sourcePath": "notion://11111111-2222-4333-8444-555555555555",
                "sourceType": "@opendocuments/connector-notion",
            }
        )
        self.assertEqual(mapped["result"]["candidates"], [])
        self.assertEqual(
            mapped["result"]["omitted"], [{"index": 0, "reason": "missing_chunk_id"}]
        )

    def test_malformed_and_mismatched_chunk_ids_are_omitted(self) -> None:
        result = _run_case(_case("hostile-chunk-identity"))
        self.assertEqual(
            [item["reason"] for item in result["result"]["omitted"]],
            [
                "missing_chunk_id",
                "invalid_chunk_id",
                "mismatched_chunk_id",
                "invalid_chunk_id",
            ],
        )
        self.assertEqual(len(result["result"]["candidates"]), 1)

    def test_no_candidate_is_ever_addressed_by_a_heading_path(self) -> None:
        for case in CASES["cases"]:
            mapped = _run_case(case)
            if not mapped["ok"]:
                continue
            for candidate in mapped["result"]["candidates"]:
                with self.subTest(case["name"]):
                    self.assertEqual(candidate["locator"]["type"], "chunk_index")
                    self.assertIs(candidate["locator"]["source_locator"], False)
                    self.assertIs(candidate["index_heading_path"]["addressable"], False)
                    self.assertEqual(
                        candidate["locator"]["index_chunk_id"],
                        candidate["indexed_identity"]["indexed_chunk_id"],
                    )

    def test_chunk_identifier_is_not_a_document_identity(self) -> None:
        result = _run_case(_case("hostile-chunk-as-document"))
        self.assertEqual(result["result"]["candidates"], [])
        self.assertEqual(
            [item["reason"] for item in result["result"]["omitted"]],
            ["chunk_id_used_as_document_id", "missing_document_id"],
        )


class IdentityLabellingTests(unittest.TestCase):
    """A0-3: indexed identity is not source-qualified identity."""

    def test_identity_is_labelled_as_index_scoped_and_source_identity_is_unknown(self) -> None:
        result = _run_case(_case("happy-sanitized-chat"))
        for candidate in result["result"]["candidates"]:
            identity = candidate["indexed_identity"]
            self.assertEqual(identity["identity_scope"], "opendocuments_index")
            self.assertEqual(identity["retriever_id"], "opendocuments")
            self.assertEqual(identity["connection_id"], "conn_opendocuments_pilot_1")
            self.assertIn("indexed_document_id", identity)
            self.assertNotIn("document_id", identity)
            self.assertEqual(candidate["source"]["identity"]["status"], "unknown")
            self.assertIsNone(candidate["source"]["identity"]["value"])
        self.assertFalse(
            result["result"]["limitations"]["source_document_identity_proven"]
        )
        self.assertIn(
            "source_document_identity", result["result"]["retriever"]["unsupported"]
        )

    def test_a_valid_notion_locator_still_does_not_prove_source_identity(self) -> None:
        mapped = _probe(
            {
                "chunkId": f"{DOC_A}_chunk_0",
                "content": "well formed notion hit",
                "documentId": DOC_A,
                "sourcePath": "notion://11111111-2222-4333-8444-555555555555",
                "sourceType": "@opendocuments/connector-notion",
            }
        )
        source = mapped["result"]["candidates"][0]["source"]
        self.assertEqual(source["locator"]["type"], "uri")
        self.assertEqual(source["identity"]["status"], "unknown")


class BoundsTests(unittest.TestCase):
    """A0-4: declared bounds are enforced by the mapper, not only documented."""

    def test_overlong_connection_id_is_refused(self) -> None:
        mapped = _probe(
            {"chunkId": f"{DOC_A}_chunk_0", "content": "x", "documentId": DOC_A},
            {"connection_id": "c" * (MAX_CONNECTION_ID_CHARS + 1)},
        )
        self.assertFalse(mapped["ok"])
        self.assertEqual(mapped["error"]["code"], "invalid_request")
        self.assertEqual([], list(VALIDATOR.iter_errors(mapped)))

    def test_boundary_length_connection_id_and_query_are_accepted(self) -> None:
        mapped = _probe(
            {"chunkId": f"{DOC_A}_chunk_0", "content": "x", "documentId": DOC_A},
            {"connection_id": "c" * MAX_CONNECTION_ID_CHARS, "query": "q" * MAX_QUERY_CHARS},
        )
        self.assertTrue(mapped["ok"])
        self.assertEqual([], list(VALIDATOR.iter_errors(mapped)))

    def test_overlong_query_is_refused(self) -> None:
        mapped = _probe(
            {"chunkId": f"{DOC_A}_chunk_0", "content": "x", "documentId": DOC_A},
            {"query": "q" * (MAX_QUERY_CHARS + 1)},
        )
        self.assertFalse(mapped["ok"])
        self.assertEqual(mapped["error"]["code"], "invalid_request")

    def test_overlong_and_control_metadata_is_bounded_and_schema_valid(self) -> None:
        result = _run_case(_case("hostile-overlong-metadata"))
        self.assertEqual([], list(VALIDATOR.iter_errors(result)))
        candidate = result["result"]["candidates"][0]
        self.assertEqual(len(candidate["excerpt"]), MAX_EXCERPT_CHARS)
        self.assertTrue(candidate["excerpt_truncated"])
        self.assertTrue(candidate["excerpt_sanitized"])
        self.assertFalse(
            any(ord(ch) < 32 and ch not in "\t\n" for ch in candidate["excerpt"])
        )
        segments = candidate["index_heading_path"]["segments"]
        self.assertLessEqual(len(segments), MAX_HEADING_SEGMENTS)
        self.assertTrue(all(len(s) <= MAX_HEADING_CHARS for s in segments))
        self.assertEqual(candidate["engine_score"], 1.0)
        self.assertEqual(result["result"]["engine"]["confidence"]["score"], 0.0)
        self.assertEqual(result["result"]["engine"]["confidence"]["level"], "none")
        self.assertEqual(candidate["content_hash"]["status"], "unavailable")

    def test_too_many_sources_fail_closed(self) -> None:
        request = {
            "request_id": "11111111-1111-4111-8111-111111111111",
            "connection_id": "conn_opendocuments_pilot_1",
            "query": "flood",
        }
        chat = {
            "queryId": "22222222-2222-4222-8222-222222222222",
            "sources": [
                {"chunkId": f"{DOC_A}_chunk_{n}", "content": "x", "documentId": DOC_A}
                for n in range(101)
            ],
        }
        mapped = map_opendocuments_chat_bound(request, chat)
        self.assertFalse(mapped["ok"])
        self.assertEqual(mapped["error"]["code"], "invalid_chat_result")

    def test_unknown_request_fields_fail_closed(self) -> None:
        request = {
            "request_id": "11111111-1111-4111-8111-111111111111",
            "connection_id": "conn_opendocuments_pilot_1",
            "query": "hello",
            "eval": "os.system('id')",
        }
        mapped = map_opendocuments_chat_bound(request, DUMMY_CHAT)
        self.assertFalse(mapped["ok"])
        self.assertEqual(mapped["error"]["code"], "invalid_request")

    def test_collection_conversation_and_workspace_override_are_refused(self) -> None:
        for name, code in (
            ("out-of-scope-collection-filter", "collection_filter_unsupported"),
            ("out-of-scope-conversation", "conversation_persistence_out_of_scope"),
            ("out-of-scope-workspace-override", "invalid_request"),
        ):
            with self.subTest(name):
                result = _run_case(_case(name))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], code)


class DisclosureTests(unittest.TestCase):
    """A0-5 and A0-6: attempted logging, and non-workspace results."""

    def test_query_logging_is_attempted_not_proven_persisted(self) -> None:
        limitations = _run_case(_case("happy-sanitized-chat"))["result"]["limitations"]
        self.assertTrue(limitations["query_logging_attempted_by_route"])
        self.assertFalse(limitations["query_log_persistence_confirmed"])
        self.assertNotIn("query_logged_upstream", limitations)

    def test_web_and_non_workspace_results_are_omitted_with_a_distinct_reason(self) -> None:
        result = _run_case(_case("hostile-web-search-result"))
        self.assertEqual(
            [item["reason"] for item in result["result"]["omitted"]],
            ["non_workspace_result", "non_workspace_result"],
        )
        self.assertEqual(len(result["result"]["candidates"]), 1)
        self.assertTrue(result["result"]["limitations"]["non_workspace_results_omitted"])
        blob = json.dumps(result)
        self.assertNotIn("web-search", blob)
        self.assertNotIn("blog.example.test", blob)

    def test_synthetic_web_chunk_id_is_omitted_even_with_a_uuid_document_id(self) -> None:
        mapped = _probe(
            {
                "chunkId": "web_3",
                "content": "merged web result",
                "documentId": DOC_B,
                "sourcePath": "https://blog.example.test/post",
                "sourceType": "web",
            }
        )
        self.assertEqual(mapped["result"]["candidates"], [])
        self.assertEqual(
            mapped["result"]["omitted"], [{"index": 0, "reason": "non_workspace_result"}]
        )


class ContentTests(unittest.TestCase):
    def test_happy_path_drops_answer_secrets_and_drive_paths(self) -> None:
        result = _run_case(_case("happy-sanitized-chat"))
        blob = json.dumps(result)
        for leaked in ("password", "Z:", "PilotShare", "grant Task write access", "should-be-dropped"):
            self.assertNotIn(leaked, blob)
        self.assertEqual(result["result"]["generated_answer"], "discarded")
        notion, local = result["result"]["candidates"]
        self.assertEqual(notion["indexed_identity"]["indexed_document_id"], DOC_A)
        self.assertEqual(notion["source"]["kind"], "notion")
        self.assertEqual(local["source"]["locator"]["value"], "sample-handbook.md")
        self.assertFalse(result["result"]["limitations"]["origin_verified"])
        self.assertFalse(result["result"]["limitations"]["source_version_from_chat"])

    def test_missing_source_version_stays_unavailable(self) -> None:
        candidate = _run_case(_case("missing-version"))["result"]["candidates"][0]
        self.assertEqual(candidate["source_version"]["status"], "unavailable")
        self.assertIsNone(candidate["source_version"]["value"])
        self.assertEqual(candidate["source_version"]["basis"], "document_read")
        self.assertEqual(candidate["content_hash"]["status"], "unavailable")
        self.assertIs(candidate["verification"]["origin_current"], False)

    def test_chat_without_metadata_marks_version_unavailable_from_chat(self) -> None:
        request = _load("input/happy-request.json")
        chat = _load("input/happy-chat.json")
        notion = map_opendocuments_chat_bound(request, chat, None)["result"]["candidates"][0]
        self.assertEqual(notion["source_version"]["basis"], "chat_response")
        self.assertEqual(notion["source_version"]["status"], "unavailable")
        self.assertEqual(notion["content_hash"]["status"], "unavailable")


class BoundaryProjectionTests(unittest.TestCase):
    """A0-1 and A0-4 counterexamples from the independent final review."""

    def test_every_boundary_projection_validates_against_the_closed_schema(self) -> None:
        """Not only the frozen catalog: every accepted boundary output too."""
        for label, source, request_overrides, chat_overrides, _ in BOUNDARY_PROBES:
            with self.subTest(label):
                mapped = _probe(source, request_overrides, chat_overrides)
                errors = sorted(VALIDATOR.iter_errors(mapped), key=lambda e: e.path)
                self.assertEqual(
                    [], [f"{list(e.path)}: {e.message}" for e in errors]
                )

    def test_no_boundary_projection_discloses_a_secret(self) -> None:
        for label, source, request_overrides, chat_overrides, secrets in BOUNDARY_PROBES:
            with self.subTest(label):
                blob = json.dumps(_probe(source, request_overrides, chat_overrides))
                for secret in secrets:
                    self.assertNotIn(secret, blob)

    def test_mixed_case_credential_url_is_redacted_like_its_lowercase_spelling(self) -> None:
        for spelling in ("https", "HTTPS", "HtTpS", "hTTPs"):
            with self.subTest(spelling):
                mapped = _probe(
                    _source(
                        sourcePath=f"{spelling}://alice:secret@example.test/doc?api_key=TOPSECRET",
                        sourceType="@opendocuments/connector-web-crawler",
                    )
                )
                locator = mapped["result"]["candidates"][0]["source"]["locator"]
                self.assertEqual(locator["type"], "redacted")
                self.assertEqual(locator["reason"], "credentialed_url")
                self.assertEqual(mapped["result"]["candidates"][0]["title"], "Untitled")

    def test_mixed_case_opaque_scheme_drops_the_private_identifier(self) -> None:
        mapped = _probe(
            _source(
                sourcePath="GDRIVE://1AbCd-private-file-id",
                sourceType="@opendocuments/connector-gdrive",
            )
        )
        source = mapped["result"]["candidates"][0]["source"]
        self.assertEqual(source["kind"], "gdrive")
        self.assertEqual(source["type_locator_agreement"], "agree")
        self.assertEqual(source["locator"]["value"], "gdrive")

    def test_every_upstream_declaration_reconciles_with_the_path_it_emits(self) -> None:
        """A0-1: the mismatch check covers the real connector names, not three."""
        for declaration, path, kind in UPSTREAM_DECLARATIONS:
            with self.subTest(f"{declaration} {path}"):
                candidate = _probe(
                    _source(sourcePath=path, sourceType=declaration)
                )["result"]["candidates"][0]
                self.assertEqual(candidate["source"]["kind"], kind)
                self.assertEqual(
                    candidate["source"]["type_locator_agreement"], "agree"
                )

    def test_declared_source_types_cover_every_upstream_producer(self) -> None:
        self.assertEqual(
            {declaration for declaration, _, _ in UPSTREAM_DECLARATIONS},
            set(DECLARED_SOURCE_TYPES),
        )

    def test_a_declaration_that_cannot_carry_the_parsed_class_is_a_mismatch(self) -> None:
        notion_path = "notion://11111111-2222-4333-8444-555555555555"
        for declaration in (
            "@opendocuments/connector-gdrive",
            "@opendocuments/connector-web-crawler",
            "@opendocuments/connector-s3",
            "upload",
        ):
            with self.subTest(declaration):
                source = _probe(
                    _source(sourcePath=notion_path, sourceType=declaration)
                )["result"]["candidates"][0]["source"]
                self.assertEqual(source["kind"], "unknown")
                self.assertEqual(source["type_locator_agreement"], "mismatch")
                self.assertEqual(
                    source["locator"]["reason"], "source_type_locator_mismatch"
                )

    def test_an_unreconcilable_declaration_redacts_instead_of_retaining(self) -> None:
        source = _probe(
            _source(
                sourcePath="notion://11111111-2222-4333-8444-555555555555",
                sourceType="@opendocuments/connector-not-a-real-plugin",
            )
        )["result"]["candidates"][0]["source"]
        self.assertEqual(source["kind"], "unknown")
        self.assertEqual(source["type_locator_agreement"], "unverified")
        self.assertEqual(
            source["locator"]["reason"], "unreconcilable_declared_source_type"
        )

    def test_an_absent_declaration_still_keeps_the_parsed_locator(self) -> None:
        """Partial evidence stays usable: only a *nonempty* declaration redacts."""
        source = _probe(
            _source(sourcePath="notion://11111111-2222-4333-8444-555555555555")
        )["result"]["candidates"][0]["source"]
        self.assertEqual(source["kind"], "notion")
        self.assertEqual(source["type_locator_agreement"], "unverified")
        self.assertEqual(
            source["locator"]["value"],
            "notion://11111111-2222-4333-8444-555555555555",
        )

    def test_chunk_position_ceiling_is_enforced_before_construction(self) -> None:
        accepted = _probe(_source(chunkId=f"{DOC_A}_chunk_{MAX_CHUNK_POSITION}"))
        candidate = accepted["result"]["candidates"][0]
        self.assertEqual(candidate["locator"]["position"], MAX_CHUNK_POSITION)
        self.assertEqual([], list(VALIDATOR.iter_errors(accepted)))

        refused = _probe(_source(chunkId=f"{DOC_A}_chunk_{MAX_CHUNK_POSITION + 1}"))
        self.assertEqual(refused["result"]["candidates"], [])
        self.assertEqual(
            refused["result"]["omitted"], [{"index": 0, "reason": "invalid_chunk_id"}]
        )

    def test_an_out_of_range_chunk_shaped_document_id_is_still_reported_as_such(self) -> None:
        mapped = _probe(
            _source(
                documentId=f"{DOC_A}_chunk_{MAX_CHUNK_POSITION + 1}",
                chunkId=f"{DOC_A}_chunk_0",
            )
        )
        self.assertEqual(
            mapped["result"]["omitted"],
            [{"index": 0, "reason": "chunk_id_used_as_document_id"}],
        )

    def test_identity_matching_is_whole_string(self) -> None:
        """A trailing line feed is not a UUID, a chunk id, or a valid request."""
        refused = _probe(
            _source(), {"request_id": "11111111-1111-4111-8111-111111111111\n"}
        )
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "invalid_request")

        refused_chat = _probe(
            _source(), {}, {"queryId": "22222222-2222-4222-8222-222222222222\n"}
        )
        self.assertFalse(refused_chat["ok"])
        self.assertEqual(refused_chat["error"]["code"], "invalid_chat_result")

        omitted = _probe(_source(documentId=f"{DOC_A}\n"))
        self.assertEqual(
            omitted["result"]["omitted"], [{"index": 0, "reason": "invalid_document_id"}]
        )
        omitted_chunk = _probe(_source(chunkId=f"{DOC_A}_chunk_0\n"))
        self.assertEqual(
            omitted_chunk["result"]["omitted"],
            [{"index": 0, "reason": "invalid_chunk_id"}],
        )

    def test_a_connection_id_carrying_a_control_character_is_refused(self) -> None:
        refused = _probe(_source(), {"connection_id": "conn\nid"})
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "invalid_request")

    def test_extreme_and_nonfinite_scores_are_bounded_not_raised(self) -> None:
        huge = int("9" * 4000)
        mapped = _probe(
            _source(score=huge),
            {},
            {"confidence": {"score": huge, "level": "high", "reason": "x"}},
        )
        self.assertTrue(mapped["ok"])
        self.assertEqual(mapped["result"]["candidates"][0]["engine_score"], 1.0)
        self.assertEqual(mapped["result"]["engine"]["confidence"]["score"], 1.0)

        for value, expected in (
            (float("inf"), 0.0),
            (float("-inf"), 0.0),
            (float("nan"), 0.0),
            (True, 0.0),
            (-5, 0.0),
            (-int("9" * 4000), 0.0),
        ):
            with self.subTest(repr(value)):
                probed = _probe(_source(score=value))
                self.assertTrue(probed["ok"])
                self.assertEqual(
                    probed["result"]["candidates"][0]["engine_score"], expected
                )
                self.assertEqual([], list(VALIDATOR.iter_errors(probed)))


if __name__ == "__main__":
    unittest.main()

"""Capture v1.1 retrieval extension boundaries, and Capture v1.0 left intact.

Every document here is synthetic: no retrieval engine, connector, NAS share,
Notion workspace, index or network is touched, and nothing is persisted. A
green run says the extension validator refuses the shapes it claims to refuse.
It is not an end-to-end acceptance of an OpenDocuments exchange, and it proves
nothing about authentication, the host-issued request ledger or replay refusal,
which this packet deliberately does not implement.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from workstack.capture_retrieval import (
    ANSWER_SCOPES,
    CONFIDENCE_LEVELS,
    MAX_CAPTURE_BODY_BYTES,
    MAX_EVIDENCE,
    MAX_EXTENSION_BYTES,
    MAX_TITLE_CHARS,
    SCHEMA,
    SOURCE_TYPES,
    CaptureRetrievalError,
    RetrievalVerification,
    VerifiedSource,
    decode_retrieval_extension,
    evidence_summary,
    require_within_capture_body_budget,
    validate_retrieval_extension,
    validate_retrieval_payload,
)
from workstack.knowledge_request import KnowledgeRequestError

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_REQUEST_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DOCUMENT_REF = "od-page-7f3ba1d34f50c884600112ab"
OTHER_DOCUMENT_REF = "od-file-2c9e51aa77b0c1de4432ff01"
DIGEST = "sha256:" + "a" * 64
SOURCE_VERSION = "od-version-14"


def verification(*sources: VerifiedSource) -> RetrievalVerification:
    return RetrievalVerification(sources)


def evidence_item(**overrides: object) -> dict:
    item = {
        "source_type": "notion.page",
        "title": "Release quality gate",
        "document_ref": DOCUMENT_REF,
        "chunk_ref": "chunk-0004abcd",
        "source_version": "od-version-14",
        "indexed_digest": DIGEST,
        "web_url": None,
    }
    item.update(overrides)
    return item


def extension(**overrides: object) -> dict:
    document = {
        "schema": SCHEMA,
        "capture_schema_version": "1.1",
        "request_id": REQUEST_ID,
        "query_id": "engine-q-00194f5a",
        "answer_scope": "single_source",
        "confidence": {"level": "medium", "score": 0.62},
        "evidence": [evidence_item()],
        "truncated": False,
    }
    document.update(overrides)
    return document


def refusal(document: object, request_id: str = REQUEST_ID) -> CaptureRetrievalError:
    try:
        validate_retrieval_extension(document, request_id=request_id)
    except CaptureRetrievalError as error:
        return error
    raise AssertionError("document was accepted")


class RetrievalAcceptanceTest(unittest.TestCase):
    def test_a_single_source_answer_projects_exactly_the_closed_fields(self):
        result = validate_retrieval_extension(extension(), request_id=REQUEST_ID)
        self.assertEqual(
            result,
            {
                "schema": SCHEMA,
                "capture_schema_version": "1.1",
                "request_id": REQUEST_ID,
                "query_id": "engine-q-00194f5a",
                "answer_scope": "single_source",
                "confidence": {"level": "medium", "score": 0.62},
                "evidence": [
                    {
                        "reported_source_type": "notion.page",
                        "title": "Release quality gate",
                        "document_ref": DOCUMENT_REF,
                        "chunk_ref": "chunk-0004abcd",
                        "reported_source_version": SOURCE_VERSION,
                        "version_state": "reported_unverified",
                        "indexed_digest": DIGEST,
                        "web_url": None,
                    }
                ],
                "truncated": False,
                "reported_origin": {
                    "document_ref": DOCUMENT_REF,
                    "source_type": "notion.page",
                },
                "origin": None,
                "origin_state": "reported_unverified",
                "capture_source_type": "knowledge.answer",
            },
        )

    def test_every_declared_source_type_and_confidence_level_is_accepted(self):
        for source_type in SOURCE_TYPES:
            with self.subTest(source_type=source_type):
                document = extension(
                    evidence=[evidence_item(source_type=source_type)]
                )
                result = validate_retrieval_extension(
                    document, request_id=REQUEST_ID
                )
                projected = result["evidence"][0]
                self.assertEqual(projected["reported_source_type"], source_type)
                # Accepted as a bounded claim, and recorded as an answer: no
                # unattested document supplies the Capture source type.
                self.assertEqual(
                    result["capture_source_type"], "knowledge.answer"
                )
        for level in CONFIDENCE_LEVELS:
            with self.subTest(level=level):
                document = extension(confidence={"level": level, "score": 0.5})
                result = validate_retrieval_extension(
                    document, request_id=REQUEST_ID
                )
                self.assertEqual(result["confidence"]["level"], level)

    def test_validation_does_not_mutate_the_submitted_document(self):
        document = extension()
        before = copy.deepcopy(document)
        validate_retrieval_extension(document, request_id=REQUEST_ID)
        self.assertEqual(document, before)

    def test_a_well_formed_payload_decodes_under_the_extension_bound(self):
        payload = json.dumps(extension())
        result = validate_retrieval_extension(
            decode_retrieval_extension(payload), request_id=REQUEST_ID
        )
        self.assertEqual(result["query_id"], "engine-q-00194f5a")

    def test_the_decoder_refuses_duplicate_keys_and_non_finite_scores(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_retrieval_extension('{"a": 1, "a": 2}')
        self.assertEqual(caught.exception.code, "duplicate_json_key")
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    decode_retrieval_extension('{"score": %s}' % literal)
                self.assertEqual(caught.exception.code, "non_finite_number")
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_retrieval_extension(b'{"t": "' + b"x" * MAX_EXTENSION_BYTES + b'"}')
        self.assertEqual(caught.exception.code, "request_too_large")


class RetrievalIdentityTest(unittest.TestCase):
    def test_the_request_id_comes_from_the_caller_not_the_answering_body(self):
        error = refusal(extension(request_id=OTHER_REQUEST_ID))
        self.assertEqual(error.code, "request_id_mismatch")

    def test_a_malformed_identifier_on_either_side_is_refused(self):
        self.assertEqual(refusal(extension(request_id="not-a-uuid")).code, "invalid_uuid")
        self.assertEqual(
            refusal(
                extension(request_id="00000000-0000-0000-0000-000000000000"),
                request_id="00000000-0000-0000-0000-000000000000",
            ).code,
            "invalid_uuid",
        )
        self.assertEqual(
            refusal(extension(), request_id=REQUEST_ID.upper()).code, "invalid_uuid"
        )

    def test_the_engine_query_id_may_not_impersonate_the_request_identity(self):
        error = refusal(extension(query_id=REQUEST_ID))
        self.assertEqual(error.code, "query_id_not_distinct")

    def test_the_query_id_is_a_bounded_opaque_token(self):
        for value in ("", None, 5, "q" * 129, "engine q", "engine/q", "engine\nq"):
            with self.subTest(value=repr(value)[:24]):
                self.assertEqual(refusal(extension(query_id=value)).code, "invalid_text")

    def test_a_wrong_schema_or_capture_version_is_refused(self):
        self.assertEqual(
            refusal(extension(schema="workstack.capture-retrieval.v2")).code,
            "unsupported_schema",
        )
        self.assertEqual(
            refusal(extension(capture_schema_version="1.0")).code, "unsupported_schema"
        )


class RetrievalEvidenceTest(unittest.TestCase):
    def test_evidence_is_bounded_at_one_and_at_ten(self):
        largest = [
            evidence_item(document_ref="od-page-{:024d}".format(index))
            for index in range(MAX_EVIDENCE)
        ]
        accepted = validate_retrieval_extension(
            extension(answer_scope="synthesized", evidence=largest),
            request_id=REQUEST_ID,
        )
        self.assertEqual(len(accepted["evidence"]), MAX_EVIDENCE)
        for value in ([], largest + [evidence_item(document_ref="od-page-x0000001")], {}, None):
            with self.subTest(size=len(value) if hasattr(value, "__len__") else value):
                self.assertEqual(
                    refusal(extension(evidence=value)).code, "invalid_evidence"
                )

    def test_the_same_document_and_chunk_may_not_be_counted_twice(self):
        document = extension(evidence=[evidence_item(), evidence_item()])
        self.assertEqual(refusal(document).code, "duplicate_evidence")

    def test_an_absent_source_version_reports_unreported_and_invents_nothing(self):
        for absent in ({"source_version": None}, {}):
            with self.subTest(shape=tuple(absent)):
                item = evidence_item(**absent)
                if not absent:
                    item.pop("source_version")
                result = validate_retrieval_extension(
                    extension(evidence=[item]), request_id=REQUEST_ID
                )
                projected = result["evidence"][0]
                self.assertIsNone(projected["reported_source_version"])
                self.assertEqual(projected["version_state"], "unreported")

    def test_an_indexed_digest_is_not_promoted_into_a_source_version(self):
        item = evidence_item(source_version=None, indexed_digest=DIGEST)
        result = validate_retrieval_extension(
            extension(evidence=[item]), request_id=REQUEST_ID
        )
        projected = result["evidence"][0]
        self.assertEqual(projected["indexed_digest"], DIGEST)
        self.assertIsNone(projected["reported_source_version"])
        self.assertEqual(projected["version_state"], "unreported")

    def test_an_indexed_digest_must_be_a_canonical_sha256_or_absent(self):
        absent = evidence_item()
        absent.pop("indexed_digest")
        self.assertIsNone(
            validate_retrieval_extension(
                extension(evidence=[absent]), request_id=REQUEST_ID
            )["evidence"][0]["indexed_digest"]
        )
        for value in ("sha256:" + "A" * 64, "a" * 64, "sha1:" + "a" * 40, 5, ""):
            with self.subTest(value=repr(value)[:24]):
                self.assertEqual(
                    refusal(
                        extension(evidence=[evidence_item(indexed_digest=value)])
                    ).code,
                    "invalid_digest",
                )

    def test_a_title_is_bounded_display_text_not_a_smuggled_document(self):
        longest = "t" * MAX_TITLE_CHARS
        accepted = validate_retrieval_extension(
            extension(evidence=[evidence_item(title=longest)]), request_id=REQUEST_ID
        )
        self.assertEqual(accepted["evidence"][0]["title"], longest)
        for value, code in (
            ("t" * (MAX_TITLE_CHARS + 1), "invalid_text"),
            ("", "invalid_text"),
            (None, "invalid_text"),
            (5, "invalid_text"),
            ("Release\nreview", "invalid_text"),
            ("Release\treview", "invalid_text"),
            ("Release\x00review", "invalid_text"),
            ("Release\x7freview", "invalid_text"),
            ("Release\x85review", "invalid_text"),
            ("<div>Release review</div>", "raw_content_suspected"),
            ("owner@example.com asked", "raw_content_suspected"),
            ("On Monday the reviewer wrote:", "raw_content_suspected"),
            ("RAW_CANARY_DO_NOT_STORE", "raw_content_suspected"),
            ("Authorization: Bearer abcdefghijklmnopqrst", "credential_material_suspected"),
        ):
            with self.subTest(value=repr(value)[:32]):
                self.assertEqual(
                    refusal(extension(evidence=[evidence_item(title=value)])).code, code
                )

    def test_a_ref_is_an_opaque_handle_never_a_path_share_or_url(self):
        for value in (
            "share/team/release.md",
            "..\\team\\release.md",
            "C:\\nas\\team\\release.md",
            "\\\\nas01\\team\\release.md",
            "https://nas01.example.com/team/release.md",
            "notion://page/7f3ba1d3",
            "od page 7f3ba1d3",
            "od%2Fpage%2F7f3ba1d3",
            "short",
            "",
            None,
            5,
            "x" * 257,
        ):
            with self.subTest(value=repr(value)[:32]):
                self.assertEqual(
                    refusal(
                        extension(evidence=[evidence_item(document_ref=value)])
                    ).code,
                    "invalid_ref",
                )

    def test_a_credential_shaped_ref_is_refused_rather_than_stored(self):
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u"
        for field in ("document_ref", "chunk_ref", "source_version"):
            with self.subTest(field=field):
                self.assertEqual(
                    refusal(
                        extension(evidence=[evidence_item(**{field: token})])
                    ).code,
                    "credential_material_suspected",
                )

    def test_a_web_url_is_null_here_because_no_resolver_policy_exists_yet(self):
        for value in (
            "https://nas01.example.com/team/release.md",
            "https://www.notion.so/7f3ba1d3",
            "",
            "about:blank",
        ):
            with self.subTest(value=value[:32]):
                self.assertEqual(
                    refusal(extension(evidence=[evidence_item(web_url=value)])).code,
                    "web_url_not_allowed",
                )

    def test_an_unsupported_source_type_is_refused(self):
        for value in ("sharepoint.file", "notion.database", "NAS.FILE", "", None):
            with self.subTest(value=value):
                self.assertEqual(
                    refusal(
                        extension(evidence=[evidence_item(source_type=value)])
                    ).code,
                    "invalid_source_type",
                )


class RetrievalAnswerScopeTest(unittest.TestCase):
    def test_a_multi_source_answer_names_no_single_original_source(self):
        document = extension(
            answer_scope="synthesized",
            evidence=[
                evidence_item(),
                evidence_item(
                    source_type="nas.file",
                    document_ref=OTHER_DOCUMENT_REF,
                    chunk_ref=None,
                    source_version=None,
                ),
            ],
        )
        result = validate_retrieval_extension(document, request_id=REQUEST_ID)
        self.assertIsNone(result["origin"])
        self.assertEqual(result["capture_source_type"], "knowledge.answer")

    def test_single_source_scope_is_refused_once_the_evidence_spans_two_documents(self):
        document = extension(
            evidence=[
                evidence_item(),
                evidence_item(document_ref=OTHER_DOCUMENT_REF),
            ]
        )
        self.assertEqual(refusal(document).code, "answer_scope_mismatch")

    def test_single_source_scope_is_refused_when_source_types_disagree(self):
        document = extension(
            evidence=[
                evidence_item(chunk_ref="chunk-0001abcd"),
                evidence_item(source_type="nas.file", chunk_ref="chunk-0002abcd"),
            ]
        )
        self.assertEqual(refusal(document).code, "answer_scope_mismatch")

    def test_synthesized_scope_is_refused_when_only_one_document_backs_it(self):
        document = extension(answer_scope="synthesized")
        self.assertEqual(refusal(document).code, "answer_scope_mismatch")

    def test_an_unsupported_answer_scope_is_refused(self):
        self.assertEqual(ANSWER_SCOPES, ("single_source", "synthesized"))
        for value in ("multi_source", "", None, True):
            with self.subTest(value=value):
                self.assertEqual(
                    refusal(extension(answer_scope=value)).code, "invalid_answer_scope"
                )


class RetrievalValueTest(unittest.TestCase):
    def test_a_confidence_score_is_a_finite_bounded_number_and_not_a_boolean(self):
        for value in (True, False, "0.5", None, [0.5], 1.5, -0.1, 2, -1, 10**400):
            with self.subTest(value=repr(value)[:24]):
                self.assertEqual(
                    refusal(
                        extension(confidence={"level": "low", "score": value})
                    ).code,
                    "invalid_confidence",
                )
        for value in (0, 1, 0.0, 1.0, 0.5):
            with self.subTest(value=value):
                result = validate_retrieval_extension(
                    extension(confidence={"level": "low", "score": value}),
                    request_id=REQUEST_ID,
                )
                self.assertEqual(result["confidence"]["score"], float(value))

    def test_an_unsupported_confidence_level_is_refused(self):
        for value in ("certain", "MEDIUM", 0.9, None):
            with self.subTest(value=value):
                self.assertEqual(
                    refusal(
                        extension(confidence={"level": value, "score": 0.5})
                    ).code,
                    "invalid_confidence",
                )

    def test_truncation_is_a_real_boolean_not_a_number(self):
        for value in (0, 1, "false", None):
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    refusal(extension(truncated=value)).code, "invalid_truncated"
                )
        self.assertTrue(
            validate_retrieval_extension(
                extension(truncated=True), request_id=REQUEST_ID
            )["truncated"]
        )

    def test_unknown_and_missing_fields_are_refused_at_every_level(self):
        self.assertEqual(refusal(extension(engine="opendocuments")).code, "unknown_field")
        self.assertEqual(
            refusal(extension(evidence=[evidence_item(score=0.9)])).code,
            "unknown_field",
        )
        self.assertEqual(
            refusal(
                extension(confidence={"level": "low", "score": 0.5, "basis": "bm25"})
            ).code,
            "unknown_field",
        )
        stripped = extension()
        del stripped["evidence"]
        self.assertEqual(refusal(stripped).code, "missing_field")
        item = evidence_item()
        del item["document_ref"]
        self.assertEqual(refusal(extension(evidence=[item])).code, "missing_field")

    def test_raw_content_and_credential_carrying_keys_are_refused_at_any_depth(self):
        canaries = (
            "body",
            "content",
            "raw",
            "attachments",
            "transcript",
            "recipients",
            "text",
            "snippet",
            "source_path",
            "file_path",
            "unc_path",
            "url",
            "token",
            "credentials",
            "api_key",
            "query_raw",
            "metadata",
            "properties",
        )
        for key in canaries:
            with self.subTest(key=key, depth="root"):
                error = refusal(extension(**{key: "RAW_CANARY_DO_NOT_STORE"}))
                self.assertEqual(error.code, "forbidden_field")
            with self.subTest(key=key, depth="evidence"):
                error = refusal(
                    extension(evidence=[evidence_item(**{key: "RAW_CANARY_DO_NOT_STORE"})])
                )
                self.assertEqual(error.code, "forbidden_field")

    def test_a_title_past_the_percent_decoding_bound_is_refused_not_decoded(self):
        buried = "%41"
        for _ in range(7):
            buried = buried.replace("%", "%25")
        self.assertEqual(
            refusal(extension(evidence=[evidence_item(title=buried)])).code,
            "invalid_text",
        )

    def test_non_string_object_keys_are_refused_at_every_level(self):
        document = extension()
        document[7] = "seven"  # type: ignore[index]
        self.assertEqual(refusal(document).code, "invalid_extension")
        item = evidence_item()
        item[7] = "seven"  # type: ignore[index]
        self.assertEqual(refusal(extension(evidence=[item])).code, "invalid_extension")

    def test_a_refusal_names_a_field_and_never_echoes_the_value(self):
        secret = "Authorization: Bearer abcdefghijklmnopqrst"
        error = refusal(extension(evidence=[evidence_item(title=secret)]))
        rendered = "{}|{}".format(error, error.details)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("abcdefghijklmnopqrst", rendered)
        self.assertEqual(error.details, {"field": "evidence[0].title"})

    def test_a_non_object_document_is_refused(self):
        for value in ([], "{}", 5, None, True):
            with self.subTest(value=value):
                self.assertEqual(refusal(value).code, "invalid_extension")


class RetrievalBudgetAndSummaryTest(unittest.TestCase):
    def test_the_capture_body_bound_is_unchanged_at_64_kib(self):
        self.assertEqual(MAX_CAPTURE_BODY_BYTES, 65536)
        require_within_capture_body_budget(MAX_CAPTURE_BODY_BYTES - 1024, 1024)
        with self.assertRaises(CaptureRetrievalError) as caught:
            require_within_capture_body_budget(MAX_CAPTURE_BODY_BYTES - 1024, 1025)
        self.assertEqual(caught.exception.code, "capture_body_too_large")

    def test_the_extension_may_not_buy_a_larger_capture_body(self):
        with self.assertRaises(CaptureRetrievalError) as caught:
            require_within_capture_body_budget(0, MAX_EXTENSION_BYTES + 1)
        self.assertEqual(caught.exception.code, "extension_too_large")
        for capture_bytes, extension_bytes in ((True, 10), (-1, 10), (10, "10")):
            with self.subTest(capture_bytes=capture_bytes):
                with self.assertRaises(CaptureRetrievalError) as caught:
                    require_within_capture_body_budget(
                        capture_bytes, extension_bytes  # type: ignore[arg-type]
                    )
                self.assertEqual(caught.exception.code, "invalid_number")

    def test_a_summary_counts_each_version_state_and_carries_no_content(self):
        document = extension(
            answer_scope="synthesized",
            truncated=True,
            evidence=[
                evidence_item(),
                evidence_item(
                    source_type="nas.file",
                    document_ref=OTHER_DOCUMENT_REF,
                    source_version=None,
                ),
            ],
        )
        result = validate_retrieval_extension(document, request_id=REQUEST_ID)
        summary = evidence_summary(result)
        self.assertEqual(
            summary,
            {
                "request_id": REQUEST_ID,
                "query_id": "engine-q-00194f5a",
                "answer_scope": "synthesized",
                "origin_state": "synthesized",
                "capture_source_type": "knowledge.answer",
                "evidence_count": 2,
                "unreported_count": 1,
                # The reported version is counted as reported, never folded in
                # with a checked one.
                "reported_unverified_count": 1,
                "verified_current_count": 0,
                "verified_stale_count": 0,
                "confidence_level": "medium",
                "truncated": True,
            },
        )
        rendered = json.dumps(summary)
        self.assertNotIn("Release quality gate", rendered)
        self.assertNotIn(DOCUMENT_REF, rendered)
        self.assertNotIn(DIGEST, rendered)


class CaptureV1CompatibilityTest(unittest.TestCase):
    """Importing the extension changes nothing about the Capture v1.0 packet."""

    def setUp(self):
        self.packet = json.loads(
            (CONTRACTS / "capture-packet-v1.fixture.json").read_text(encoding="utf-8")
        )

    def test_the_frozen_v1_fixture_still_projects_identically(self):
        from workstack.capture import validate_capture_packet

        first = validate_capture_packet(copy.deepcopy(self.packet))
        import workstack.capture_retrieval  # noqa: F401  (import is the subject)

        second = validate_capture_packet(copy.deepcopy(self.packet))
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], "1.0")
        self.assertNotIn("retrieval", first)

    def test_the_manual_v1_fixture_still_projects_identically(self):
        from workstack.capture import validate_capture_packet

        manual = json.loads(
            (CONTRACTS / "capture-packet-v1.manual.fixture.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            validate_capture_packet(copy.deepcopy(manual)),
            validate_capture_packet(copy.deepcopy(manual)),
        )

    def test_an_attached_extension_is_dropped_by_the_v1_projection(self):
        from workstack.capture import validate_capture_packet

        baseline = validate_capture_packet(copy.deepcopy(self.packet))
        carried = copy.deepcopy(self.packet)
        carried["retrieval"] = extension()
        # A v1.0 reader projects an allow-list, so the extension is not stored
        # and not silently merged. Carrying it inside a Capture buys nothing
        # until the 1.1 projection gate defines where it lands.
        self.assertEqual(validate_capture_packet(carried), baseline)

    def test_the_v1_negative_fixture_is_still_refused(self):
        from workstack.capture import CaptureValidationError, validate_capture_packet

        negative = json.loads(
            (CONTRACTS / "capture-packet-v1.negative-raw.json").read_text(
                encoding="utf-8"
            )
        )
        with self.assertRaises(CaptureValidationError):
            validate_capture_packet(negative)


class ReportedVersusVerifiedProvenanceTest(unittest.TestCase):
    """A document's provenance claim never becomes the projection's provenance."""

    def test_an_asserted_source_type_and_version_stay_reported_claims(self):
        # The review's counterexample: a syntactically valid payload naming a
        # source type and an invented version. Both are accepted as bounded
        # text and neither reaches a trusted field.
        document = extension(
            evidence=[
                evidence_item(
                    source_type="nas.file", source_version="attacker-version"
                )
            ]
        )
        result = validate_retrieval_extension(document, request_id=REQUEST_ID)
        projected = result["evidence"][0]
        self.assertEqual(projected["reported_source_type"], "nas.file")
        self.assertEqual(projected["reported_source_version"], "attacker-version")
        self.assertEqual(projected["version_state"], "reported_unverified")
        self.assertEqual(
            result["reported_origin"],
            {"document_ref": DOCUMENT_REF, "source_type": "nas.file"},
        )
        self.assertIsNone(result["origin"])
        self.assertEqual(result["origin_state"], "reported_unverified")
        self.assertEqual(result["capture_source_type"], "knowledge.answer")

    def test_no_projected_key_presents_an_unattested_claim_as_provenance(self):
        result = validate_retrieval_extension(extension(), request_id=REQUEST_ID)
        # Nothing carries the bare name a reader would take for attested truth.
        self.assertNotIn("source_type", result["evidence"][0])
        self.assertNotIn("source_version", result["evidence"][0])
        self.assertNotIn("versioned", {result["evidence"][0]["version_state"]})

    def test_a_trusted_origin_and_current_version_come_only_from_the_caller(self):
        attested = verification(
            VerifiedSource(DOCUMENT_REF, "notion.page", SOURCE_VERSION)
        )
        result = validate_retrieval_extension(
            extension(), request_id=REQUEST_ID, verification=attested
        )
        self.assertEqual(result["evidence"][0]["version_state"], "verified_current")
        self.assertEqual(
            result["origin"],
            {"document_ref": DOCUMENT_REF, "source_type": "notion.page"},
        )
        self.assertEqual(result["origin_state"], "verified")
        self.assertEqual(result["capture_source_type"], "notion.page")

    def test_a_reported_version_the_source_disagrees_with_reads_as_stale(self):
        attested = verification(
            VerifiedSource(DOCUMENT_REF, "notion.page", "od-version-15")
        )
        result = validate_retrieval_extension(
            extension(), request_id=REQUEST_ID, verification=attested
        )
        self.assertEqual(result["evidence"][0]["version_state"], "verified_stale")
        # Identity was still verified, so the origin stands; only currentness
        # is contradicted.
        self.assertEqual(result["origin_state"], "verified")

    def test_verified_identity_without_a_verified_version_claims_no_currentness(self):
        attested = verification(VerifiedSource(DOCUMENT_REF, "notion.page"))
        result = validate_retrieval_extension(
            extension(), request_id=REQUEST_ID, verification=attested
        )
        self.assertEqual(
            result["evidence"][0]["version_state"], "reported_unverified"
        )
        self.assertEqual(result["origin_state"], "verified")

    def test_an_unreported_version_is_not_called_stale_by_a_verified_one(self):
        attested = verification(
            VerifiedSource(DOCUMENT_REF, "notion.page", SOURCE_VERSION)
        )
        result = validate_retrieval_extension(
            extension(evidence=[evidence_item(source_version=None)]),
            request_id=REQUEST_ID,
            verification=attested,
        )
        # The answer claimed nothing; an absent claim cannot be contradicted.
        self.assertEqual(result["evidence"][0]["version_state"], "unreported")

    def test_an_indexed_digest_never_upgrades_source_verification(self):
        item = evidence_item(source_version=None, indexed_digest=DIGEST)
        attested = verification(
            VerifiedSource(DOCUMENT_REF, "notion.page", SOURCE_VERSION)
        )
        result = validate_retrieval_extension(
            extension(evidence=[item]),
            request_id=REQUEST_ID,
            verification=attested,
        )
        projected = result["evidence"][0]
        self.assertEqual(projected["indexed_digest"], DIGEST)
        self.assertIsNone(projected["reported_source_version"])
        self.assertEqual(projected["version_state"], "unreported")

    def test_verification_of_a_different_document_grants_nothing(self):
        attested = verification(
            VerifiedSource(OTHER_DOCUMENT_REF, "notion.page", SOURCE_VERSION)
        )
        result = validate_retrieval_extension(
            extension(), request_id=REQUEST_ID, verification=attested
        )
        self.assertEqual(
            result["evidence"][0]["version_state"], "reported_unverified"
        )
        self.assertIsNone(result["origin"])

    def test_a_document_contradicting_an_attested_source_type_is_refused(self):
        attested = verification(VerifiedSource(DOCUMENT_REF, "nas.file"))
        with self.assertRaises(CaptureRetrievalError) as caught:
            validate_retrieval_extension(
                extension(), request_id=REQUEST_ID, verification=attested
            )
        self.assertEqual(caught.exception.code, "verification_conflict")

    def test_a_synthesized_answer_stays_an_answer_even_when_verified(self):
        document = extension(
            answer_scope="synthesized",
            evidence=[
                evidence_item(),
                evidence_item(
                    document_ref=OTHER_DOCUMENT_REF, chunk_ref=None
                ),
            ],
        )
        attested = verification(
            VerifiedSource(DOCUMENT_REF, "notion.page", SOURCE_VERSION),
            VerifiedSource(OTHER_DOCUMENT_REF, "notion.page", SOURCE_VERSION),
        )
        result = validate_retrieval_extension(
            document, request_id=REQUEST_ID, verification=attested
        )
        self.assertIsNone(result["reported_origin"])
        self.assertIsNone(result["origin"])
        self.assertEqual(result["origin_state"], "synthesized")
        self.assertEqual(result["capture_source_type"], "knowledge.answer")

    def test_a_malformed_verification_object_refuses_under_a_closed_code(self):
        cases = (
            ({"document_ref": DOCUMENT_REF}, "invalid_verification"),
            (RetrievalVerification([VerifiedSource(DOCUMENT_REF, "notion.page")]),
             "invalid_verification"),
            (RetrievalVerification((VerifiedSource(7, "notion.page"),)),
             "invalid_verification"),
            (RetrievalVerification((VerifiedSource(DOCUMENT_REF, "smb.share"),)),
             "invalid_verification"),
            (RetrievalVerification((VerifiedSource(DOCUMENT_REF, "notion.page", 14),)),
             "invalid_verification"),
            (RetrievalVerification(
                (
                    VerifiedSource(DOCUMENT_REF, "notion.page"),
                    VerifiedSource(DOCUMENT_REF, "notion.page"),
                )
            ), "invalid_verification"),
        )
        for value, code in cases:
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(CaptureRetrievalError) as caught:
                    validate_retrieval_extension(
                        extension(),
                        request_id=REQUEST_ID,
                        verification=value,  # type: ignore[arg-type]
                    )
                self.assertEqual(caught.exception.code, code)


class RawDataEscapeTest(unittest.TestCase):
    """The two allowed free strings are not a way out for raw data."""

    def test_a_credential_shaped_query_id_is_refused(self):
        token = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dBjftJeZ4CVPmB92K27u"
        )
        error = refusal(extension(query_id=token))
        self.assertEqual(error.code, "credential_material_suspected")
        self.assertEqual(error.field, "query_id")

    def test_an_ordinary_engine_correlation_id_is_still_accepted(self):
        for value in ("engine-q-00194f5a", "od.search:2026-09-08T00", "q~117"):
            with self.subTest(value=value):
                result = validate_retrieval_extension(
                    extension(query_id=value), request_id=REQUEST_ID
                )
                self.assertEqual(result["query_id"], value)

    def test_a_source_location_may_not_be_relabelled_as_a_title(self):
        locations = (
            # T1: decoded drive absolute and relative, UNC, file URL, encoded.
            "C:/secret/payroll.xlsx",
            "C:\\secret\\payroll.xlsx",
            "C:secret/payroll.xlsx",
            "C:payroll.xlsx",
            "\\\\fs01\\finance\\payroll.xlsx",
            "//fs01/finance/payroll.xlsx",
            "file:///C:/secret/payroll.xlsx",
            "C%3A/secret/payroll.xlsx",
            "C%3Asecret/payroll.xlsx",
            "C%3Apayroll.xlsx",
            "file%3A%2F%2F%2FC%3A/secret/payroll.xlsx",
            "%2Fmnt%2Fnas%2Fpayroll.xlsx",
            "https://intranet.example/payroll",
            "/mnt/nas/finance/payroll.xlsx",
            "nas/finance/payroll.xlsx",
            "~/notes/payroll.xlsx",
            "../../etc/passwd",
            "smb:finance-share",
            "file:payroll.xlsx",
            # T2: generic RFC-style scheme with a non-whitespace payload.
            "urn:isbn:9780140328721",
            "s3:bucket/object",
            "custom+v1:opaque",
            # T3: one-level relative file locations and traversal.
            "finance/payroll.xlsx",
            "finance\\payroll.xlsx",
            "./payroll.xlsx",
            "../payroll.xlsx",
        )
        for value in locations:
            with self.subTest(value=value):
                error = refusal(extension(evidence=[evidence_item(title=value)]))
                self.assertEqual(error.code, "source_location_suspected")
                self.assertEqual(error.field, "evidence[0].title")

    def test_ordinary_filenames_and_titles_are_still_accepted(self):
        titles = (
            "Release quality gate",
            "2026-Q3 gate.xlsx",
            "payroll.xlsx",
            "Q3/Q4 planning",
            "Budget / Forecast",
            "R&D / QA / Ops handover",
            "and/or decisions",
            "File: Payroll review",
            "SMB: deployment notes",
            "\ubc30\ud3ec \uae30\uc900 v2",
            "Meeting notes (2026-09-08)",
        )
        for value in titles:
            with self.subTest(value=value):
                result = validate_retrieval_extension(
                    extension(evidence=[evidence_item(title=value)]),
                    request_id=REQUEST_ID,
                )
                self.assertEqual(result["evidence"][0]["title"], value)


class MeasuredWireBoundaryTest(unittest.TestCase):
    """Only the payload entry point establishes wire size and depth."""

    def oversized_payload(self) -> bytes:
        items = [
            evidence_item(
                document_ref="od-file-{:024d}".format(index),
                chunk_ref=None,
                title="\U0001f600" * MAX_TITLE_CHARS,
            )
            for index in range(MAX_EVIDENCE)
        ]
        document = extension(answer_scope="synthesized", evidence=items)
        return json.dumps(document, ensure_ascii=False).encode("utf-8")

    def test_an_object_over_the_byte_bound_is_accepted_only_off_the_wire(self):
        raw = self.oversized_payload()
        self.assertGreater(len(raw), MAX_EXTENSION_BYTES)
        # The split is deliberate and documented: the object helper validates
        # shape and says nothing about size.
        decoded = json.loads(raw.decode("utf-8"))
        self.assertEqual(
            validate_retrieval_extension(decoded, request_id=REQUEST_ID)[
                "origin_state"
            ],
            "synthesized",
        )
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_retrieval_payload(raw, request_id=REQUEST_ID)
        self.assertEqual(caught.exception.code, "request_too_large")

    def test_the_payload_entry_point_measures_the_real_utf8_octets(self):
        document = extension()
        raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
        result = validate_retrieval_payload(
            raw,
            request_id=REQUEST_ID,
            capture_body_bytes=MAX_CAPTURE_BODY_BYTES - len(raw),
        )
        self.assertEqual(result["origin_state"], "reported_unverified")
        with self.assertRaises(CaptureRetrievalError) as caught:
            validate_retrieval_payload(
                raw,
                request_id=REQUEST_ID,
                capture_body_bytes=MAX_CAPTURE_BODY_BYTES - len(raw) + 1,
            )
        self.assertEqual(caught.exception.code, "capture_body_too_large")

    def test_a_malformed_payload_refuses_as_malformed_not_as_oversized(self):
        raw = json.dumps(extension(query_id="")).encode("utf-8")
        with self.assertRaises(CaptureRetrievalError) as caught:
            validate_retrieval_payload(
                raw, request_id=REQUEST_ID, capture_body_bytes=MAX_CAPTURE_BODY_BYTES
            )
        self.assertEqual(caught.exception.code, "invalid_text")

    def test_a_payload_that_is_neither_text_nor_bytes_is_refused(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_retrieval_payload({}, request_id=REQUEST_ID)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_a_deeply_nested_direct_object_refuses_instead_of_recursing(self):
        nested: dict = {"schema": SCHEMA}
        cursor = nested
        for _ in range(64):
            cursor["evidence"] = [{"schema": SCHEMA}]
            cursor = cursor["evidence"][0]
        with self.assertRaises(CaptureRetrievalError) as caught:
            validate_retrieval_extension(nested, request_id=REQUEST_ID)
        self.assertEqual(caught.exception.code, "extension_too_deep")

if __name__ == "__main__":
    unittest.main()

"""Focused tests for the KnowledgeVerification v1 request and result wires.

Nothing here opens a Store, spawns a child, reads a wall clock, touches a
network or names a real corpus. Every instant is supplied by the test, every
document is synthetic, and the module under test is a pure validator: the point
of these cases is that a *well-formed-looking* answer which does not actually
answer the request that was sent is refused, not merely that a rule exists.
"""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workstack.knowledge_verification_protocol import (  # noqa: E402
    MAX_ACTIVE_SECONDS,
    MAX_EVIDENCE,
    MAX_VERIFICATION_BYTES,
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
    VERIFICATION_OUTCOMES,
    KnowledgeVerificationBinding,
    VerificationError,
    validate_verification_request,
    validate_verification_result,
)

WORKSPACE = "8f14e45f-ce8a-4b0e-9c2a-1d1f0a3b5c77"
UPSTREAM = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
NONCE = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
OTHER_NONCE = "6ba7b810-9dad-41d1-80b4-00c04fd430c8"

REQUESTED_AT = "2026-09-09T12:00:00Z"
EXPIRES_AT = "2026-09-09T12:01:00Z"
NOW = "2026-09-09T12:00:10Z"

DOC_ONE = "docalpha0001"
DOC_TWO = "docbeta.0002"
VERSION_ONE = "v-0000000001"
VERSION_TWO = "v-0000000002"


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "document_ref": DOC_ONE,
        "source_type": "nas.file",
        "expected_source_version": VERSION_ONE,
    }
    entry.update(overrides)
    return entry


def _request(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema": REQUEST_SCHEMA,
        "verification_id": NONCE,
        "binding": {
            "workspace_uid": WORKSPACE,
            "capture_id": "C-0042",
            "capture_revision": 3,
        },
        "connection": {
            "alias": "od-primary",
            "upstream_workspace_uid": UPSTREAM,
            "policy_revision": 7,
        },
        "corpus_refs": ["engineering", "policy"],
        "evidence": [_entry(), _entry(document_ref=DOC_TWO, source_type="notion.page")],
        "requested_at": REQUESTED_AT,
        "expires_at": EXPIRES_AT,
    }
    document.update(overrides)
    return document


def _answer(entry: dict[str, object], **overrides: object) -> dict[str, object]:
    answered = dict(entry)
    answered["observed_source_version"] = entry["expected_source_version"]
    answered["status"] = "current"
    answered["code"] = "hash_matched"
    answered.update(overrides)
    return answered


def _result(**overrides: object) -> dict[str, object]:
    request = _request()
    evidence = [_answer(item) for item in request["evidence"]]  # type: ignore[union-attr]
    document: dict[str, object] = {
        "schema": RESULT_SCHEMA,
        "verification_id": NONCE,
        "checked_at": "2026-09-09T12:00:05Z",
        "evidence": evidence,
    }
    document.update(overrides)
    return document


class _ProtocolTestCase(unittest.TestCase):
    def _refuse_request(self, document: object, code: str, *, now: str = NOW) -> None:
        with self.assertRaises(VerificationError) as caught:
            validate_verification_request(document, now=now)
        self.assertEqual(caught.exception.code, code)

    def _admitted(self) -> dict[str, object]:
        return validate_verification_request(_request(), now=NOW)

    def _refuse_result(self, document: object, code: str, *, now: str = NOW) -> None:
        with self.assertRaises(VerificationError) as caught:
            validate_verification_result(document, request=self._admitted(), now=now)
        self.assertEqual(caught.exception.code, code)


class RequestAdmissionTests(_ProtocolTestCase):
    def test_valid_request_normalises_to_the_exact_closed_projection(self) -> None:
        admitted = validate_verification_request(_request(), now=NOW)
        self.assertEqual(
            set(admitted),
            {
                "schema",
                "verification_id",
                "binding",
                "connection",
                "corpus_refs",
                "evidence",
                "requested_at",
                "expires_at",
            },
        )
        self.assertEqual(admitted["schema"], REQUEST_SCHEMA)
        self.assertEqual(admitted["verification_id"], NONCE)
        self.assertEqual(
            admitted["binding"],
            {"workspace_uid": WORKSPACE, "capture_id": "C-0042", "capture_revision": 3},
        )
        self.assertEqual(
            admitted["connection"],
            {
                "alias": "od-primary",
                "upstream_workspace_uid": UPSTREAM,
                "policy_revision": 7,
            },
        )
        self.assertEqual(admitted["corpus_refs"], ["engineering", "policy"])
        self.assertEqual(
            admitted["evidence"],
            [
                {
                    "document_ref": DOC_ONE,
                    "source_type": "nas.file",
                    "expected_source_version": VERSION_ONE,
                },
                {
                    "document_ref": DOC_TWO,
                    "source_type": "notion.page",
                    "expected_source_version": VERSION_ONE,
                },
            ],
        )

    def test_repeated_document_ref_is_admitted_as_distinct_evidence(self) -> None:
        document = _request(evidence=[_entry(), _entry(expected_source_version=None)])
        admitted = validate_verification_request(document, now=NOW)
        self.assertEqual(len(admitted["evidence"]), 2)
        self.assertEqual(
            [item["expected_source_version"] for item in admitted["evidence"]],
            [VERSION_ONE, None],
        )

    def test_absent_expected_version_is_admitted_as_an_explicit_absence(self) -> None:
        document = _request(evidence=[_entry(expected_source_version=None)])
        admitted = validate_verification_request(document, now=NOW)
        self.assertIsNone(admitted["evidence"][0]["expected_source_version"])

    def test_full_evidence_and_corpus_bounds_are_admitted(self) -> None:
        document = _request(
            corpus_refs=["a{}".format(index) for index in range(8)],
            evidence=[_entry() for _ in range(MAX_EVIDENCE)],
        )
        admitted = validate_verification_request(document, now=NOW)
        self.assertEqual(len(admitted["evidence"]), MAX_EVIDENCE)
        self.assertEqual(len(admitted["corpus_refs"]), 8)

    def test_wrong_schema_is_refused(self) -> None:
        self._refuse_request(
            _request(schema="workstack.knowledge-request.v1"),
            "unsupported_verification_schema",
        )

    def test_unknown_and_missing_envelope_fields_are_refused(self) -> None:
        self._refuse_request(
            _request(indexed_digest="a" * 64), "invalid_verification_request"
        )
        missing = _request()
        del missing["corpus_refs"]
        self._refuse_request(missing, "invalid_verification_request")
        self._refuse_request(["not", "an", "object"], "invalid_verification_request")

    def test_non_canonical_nonce_is_refused(self) -> None:
        self._refuse_request(
            _request(verification_id=NONCE.upper()), "invalid_verification_id"
        )
        self._refuse_request(
            _request(verification_id="00000000-0000-0000-0000-000000000000"),
            "invalid_verification_id",
        )


class RequestBindingTests(_ProtocolTestCase):
    def _binding(self, **overrides: object) -> dict[str, object]:
        binding = {
            "workspace_uid": WORKSPACE,
            "capture_id": "C-0042",
            "capture_revision": 3,
        }
        binding.update(overrides)
        return binding

    def test_capture_id_must_match_the_stored_capture_grammar(self) -> None:
        for value in ("C-042", "c-0042", "X-0042", "C-0042 ", 42, None):
            self._refuse_request(
                _request(binding=self._binding(capture_id=value)),
                "invalid_verification_binding",
            )

    def test_capture_revision_must_be_a_nonnegative_safe_integer(self) -> None:
        for value in (-1, 2**53, 1.0, True, "3", None):
            self._refuse_request(
                _request(binding=self._binding(capture_revision=value)),
                "invalid_verification_binding",
            )

    def test_binding_shape_is_closed(self) -> None:
        self._refuse_request(
            _request(binding=self._binding(task_uid=WORKSPACE)),
            "invalid_verification_binding",
        )
        self._refuse_request(
            _request(binding={"workspace_uid": WORKSPACE}),
            "invalid_verification_binding",
        )

    def test_connection_alias_upstream_and_revision_are_checked(self) -> None:
        connection = {
            "alias": "od-primary",
            "upstream_workspace_uid": UPSTREAM,
            "policy_revision": 7,
        }
        for override in (
            {"alias": "OD-Primary"},
            {"alias": "od primary"},
            {"alias": ""},
            {"upstream_workspace_uid": "not-a-uuid"},
            {"policy_revision": -1},
            {"policy_revision": 2**53},
        ):
            candidate = dict(connection)
            candidate.update(override)
            self._refuse_request(
                _request(connection=candidate), "invalid_verification_connection"
            )

    def test_corpus_refs_are_bounded_and_distinct(self) -> None:
        self._refuse_request(_request(corpus_refs=[]), "invalid_verification_corpus_refs")
        self._refuse_request(
            _request(corpus_refs=["a{}".format(index) for index in range(9)]),
            "invalid_verification_corpus_refs",
        )
        self._refuse_request(
            _request(corpus_refs=["Engineering"]), "invalid_verification_corpus_refs"
        )
        self._refuse_request(
            _request(corpus_refs=["engineering", "engineering"]),
            "duplicate_verification_corpus_ref",
        )


class RequestEvidenceTests(_ProtocolTestCase):
    def test_evidence_count_is_bounded(self) -> None:
        self._refuse_request(_request(evidence=[]), "invalid_verification_evidence")
        self._refuse_request(
            _request(evidence=[_entry() for _ in range(MAX_EVIDENCE + 1)]),
            "invalid_verification_evidence",
        )
        self._refuse_request(_request(evidence={}), "invalid_verification_evidence")

    def test_source_type_is_closed(self) -> None:
        for value in ("nas.share", "http.page", "", None, 3):
            self._refuse_request(
                _request(evidence=[_entry(source_type=value)]),
                "invalid_verification_source_type",
            )

    def test_a_path_share_or_url_is_not_representable_as_a_document_ref(self) -> None:
        for value in (
            "/mnt/share/payroll.xlsx",
            "\\\\nas01\\finance\\payroll.xlsx",
            "C:/finance/payroll.xlsx",
            "https://example.invalid/page",
            "docs%2Fpayroll",
            "doc alpha 0001",
            "../../etc/passwd",
            "short",
            "a" * 257,
        ):
            self._refuse_request(
                _request(evidence=[_entry(document_ref=value)]),
                "invalid_verification_evidence",
            )

    def test_expected_source_version_uses_the_same_opaque_grammar(self) -> None:
        for value in ("/etc/passwd", "v 1", 7, ""):
            self._refuse_request(
                _request(evidence=[_entry(expected_source_version=value)]),
                "invalid_verification_evidence",
            )

    def test_the_wire_hash_form_is_admitted_and_the_colon_form_is_not(self) -> None:
        """The R20 seam the root clarification pinned, from this side.

        The reused opaque grammar admits ``sha256-<64 hex>`` and refuses the
        internal ``sha256:<hex>`` spelling, because a colon is a scheme and a
        drive letter. The adapter translates between the two; the protocol is
        not widened to carry the colon form.
        """

        wire = "sha256-" + "ab12cd34" * 8
        admitted = validate_verification_request(
            _request(evidence=[_entry(expected_source_version=wire)]), now=NOW
        )
        self.assertEqual(
            admitted["evidence"][0]["expected_source_version"], wire
        )
        self._refuse_request(
            _request(
                evidence=[
                    _entry(expected_source_version="sha256:" + "ab12cd34" * 8)
                ]
            ),
            "invalid_verification_evidence",
        )

    def test_the_wire_hash_form_round_trips_through_a_result(self) -> None:
        wire = "sha256-" + "ab12cd34" * 8
        other = "sha256-" + "ff00ff00" * 8
        request = validate_verification_request(
            _request(evidence=[_entry(expected_source_version=wire)]), now=NOW
        )
        document = _result(
            evidence=[
                _answer(
                    _entry(expected_source_version=wire),
                    status="stale",
                    code="hash_differs",
                    observed_source_version=other,
                )
            ]
        )
        result = validate_verification_result(document, request=request, now=NOW)
        self.assertEqual(result["evidence"][0]["observed_source_version"], other)
        with self.assertRaises(VerificationError) as caught:
            validate_verification_result(
                _result(
                    evidence=[
                        _answer(
                            _entry(expected_source_version=wire),
                            status="stale",
                            code="hash_differs",
                            observed_source_version="sha256:" + "ff00ff00" * 8,
                        )
                    ]
                ),
                request=request,
                now=NOW,
            )
        self.assertEqual(
            caught.exception.code, "invalid_verification_observed_version"
        )

    def test_evidence_entry_shape_is_closed(self) -> None:
        self._refuse_request(
            _request(evidence=[_entry(indexed_digest="a" * 64)]),
            "invalid_verification_evidence",
        )
        self._refuse_request(
            _request(evidence=[_entry(path="/mnt/share")]),
            "invalid_verification_evidence",
        )
        self._refuse_request(
            _request(evidence=[{"document_ref": DOC_ONE, "source_type": "nas.file"}]),
            "invalid_verification_evidence",
        )


class RequestWindowTests(_ProtocolTestCase):
    def test_a_sixty_second_window_is_the_maximum(self) -> None:
        self.assertEqual(MAX_ACTIVE_SECONDS, 60)
        self._refuse_request(
            _request(expires_at="2026-09-09T12:01:00.001Z"),
            "invalid_verification_window",
        )

    def test_zero_length_and_backwards_windows_are_refused(self) -> None:
        self._refuse_request(
            _request(expires_at=REQUESTED_AT), "invalid_verification_window"
        )
        self._refuse_request(
            _request(expires_at="2026-09-09T11:59:00Z"), "invalid_verification_window"
        )

    def test_a_request_from_the_future_is_not_yet_active(self) -> None:
        self._refuse_request(
            _request(), "verification_not_yet_active", now="2026-09-09T11:59:59Z"
        )

    def test_expiry_is_terminal(self) -> None:
        self._refuse_request(_request(), "verification_expired", now=EXPIRES_AT)
        self._refuse_request(
            _request(), "verification_expired", now="2026-09-09T12:05:00Z"
        )

    def test_requested_at_equal_to_now_is_admitted(self) -> None:
        admitted = validate_verification_request(_request(), now=REQUESTED_AT)
        self.assertEqual(admitted["requested_at"], REQUESTED_AT)

    def test_malformed_instants_are_refused(self) -> None:
        for override in (
            {"requested_at": "2026-09-09 12:00:00"},
            {"requested_at": "2026-09-09T12:00:00"},
            {"expires_at": 1788884999},
            {"expires_at": None},
        ):
            self._refuse_request(
                _request(**override), "invalid_verification_timestamp"
            )
        self._refuse_request(_request(), "invalid_verification_timestamp", now="soon")


class StrictWireTests(_ProtocolTestCase):
    """The wire is decoded by the process' one strict decoder, not a second one."""

    def test_a_duplicate_json_key_is_refused_by_the_shared_decoder(self) -> None:
        payload = json.dumps(_request())
        duplicated = payload.replace(
            '"verification_id"', '"verification_id": "{}", "verification_id"'.format(
                OTHER_NONCE
            ),
            1,
        )
        self._refuse_request(duplicated, "duplicate_json_key")

    def test_an_oversized_payload_is_refused_before_it_is_parsed(self) -> None:
        oversized = json.dumps(
            _request(corpus_refs=["engineering", "x" * MAX_VERIFICATION_BYTES])
        )
        self.assertGreater(len(oversized.encode("utf-8")), MAX_VERIFICATION_BYTES)
        self._refuse_request(oversized, "request_too_large")

    def test_a_non_finite_number_is_refused(self) -> None:
        payload = json.dumps(_request()).replace('"capture_revision": 3', '"capture_revision": 1e9999')
        self._refuse_request(payload, "non_finite_number")
        self._refuse_request(
            json.dumps(_request()).replace('"capture_revision": 3', '"capture_revision": NaN'),
            "non_finite_number",
        )

    def test_an_over_deep_document_is_refused(self) -> None:
        # Which of the decoder's bounds an over-deep document trips first is an
        # interpreter detail, so this asserts the same closed set the existing
        # request and proposal suites assert -- never an admission.
        depth = 4000
        with self.assertRaises(VerificationError) as caught:
            validate_verification_request("[" * depth + "]" * depth, now=NOW)
        self.assertIn(
            caught.exception.code,
            {"request_too_deep", "invalid_json", "request_too_large"},
        )

    def test_invalid_encoding_and_invalid_json_are_refused(self) -> None:
        self._refuse_request(b"\xff\xfe\x00{", "invalid_encoding")
        self._refuse_request(b"{not json", "invalid_json")

    def test_bytes_and_text_forms_of_a_valid_request_agree(self) -> None:
        payload = json.dumps(_request())
        from_text = validate_verification_request(payload, now=NOW)
        from_bytes = validate_verification_request(payload.encode("utf-8"), now=NOW)
        self.assertEqual(from_text, from_bytes)
        self.assertEqual(from_text, validate_verification_request(_request(), now=NOW))


class ResultAdmissionTests(_ProtocolTestCase):
    def test_a_matching_result_normalises_to_the_exact_closed_projection(self) -> None:
        admitted = self._admitted()
        result = validate_verification_result(_result(), request=admitted, now=NOW)
        self.assertEqual(
            set(result), {"schema", "verification_id", "checked_at", "evidence"}
        )
        self.assertEqual(result["verification_id"], NONCE)
        self.assertEqual(result["checked_at"], "2026-09-09T12:00:05Z")
        self.assertEqual(
            result["evidence"][0],
            {
                "document_ref": DOC_ONE,
                "source_type": "nas.file",
                "expected_source_version": VERSION_ONE,
                "observed_source_version": VERSION_ONE,
                "status": "current",
                "code": "hash_matched",
            },
        )

    def test_every_declared_outcome_pair_is_admitted_on_its_own_terms(self) -> None:
        admitted = validate_verification_request(
            _request(evidence=[_entry()]), now=NOW
        )
        for status, code in VERIFICATION_OUTCOMES:
            observed = None
            if status == "current":
                observed = VERSION_ONE
            elif status == "stale":
                observed = VERSION_TWO
            document = _result(
                evidence=[
                    _answer(
                        _entry(),
                        status=status,
                        code=code,
                        observed_source_version=observed,
                    )
                ]
            )
            result = validate_verification_result(document, request=admitted, now=NOW)
            self.assertEqual(result["evidence"][0]["status"], status)
            self.assertEqual(result["evidence"][0]["code"], code)

    def test_wrong_result_schema_is_refused(self) -> None:
        self._refuse_result(
            _result(schema=REQUEST_SCHEMA), "unsupported_verification_schema"
        )

    def test_unknown_and_missing_result_fields_are_refused(self) -> None:
        self._refuse_result(_result(binding={}), "invalid_verification_result")
        missing = _result()
        del missing["checked_at"]
        self._refuse_result(missing, "invalid_verification_result")

    def test_a_result_answering_another_check_is_refused(self) -> None:
        self._refuse_result(
            _result(verification_id=OTHER_NONCE), "verification_id_mismatch"
        )
        self._refuse_result(
            _result(verification_id=NONCE.upper()), "invalid_verification_id"
        )

    def test_a_corrupt_retained_request_refuses_closed(self) -> None:
        for request in (None, {}, {"schema": REQUEST_SCHEMA}, ["evidence"]):
            with self.assertRaises(VerificationError) as caught:
                validate_verification_result(_result(), request=request, now=NOW)
            self.assertEqual(caught.exception.code, "invalid_verification_request")


class ResultBindingTests(_ProtocolTestCase):
    def test_evidence_count_must_equal_the_request(self) -> None:
        short = _result()
        short["evidence"] = short["evidence"][:1]  # type: ignore[index]
        self._refuse_result(short, "verification_evidence_mismatch")
        extra = _result()
        extra["evidence"] = list(extra["evidence"]) + [  # type: ignore[arg-type]
            _answer(_entry())
        ]
        self._refuse_result(extra, "verification_evidence_mismatch")
        self._refuse_result(_result(evidence={}), "verification_evidence_mismatch")

    def test_evidence_order_must_be_preserved(self) -> None:
        reordered = _result()
        reordered["evidence"] = list(reversed(reordered["evidence"]))  # type: ignore[call-overload]
        self._refuse_result(reordered, "verification_evidence_mismatch")

    def test_an_entry_may_not_be_rebound_to_another_document(self) -> None:
        rebound = _result(
            evidence=[
                _answer(_entry(document_ref=DOC_TWO)),
                _answer(_entry(document_ref=DOC_TWO, source_type="notion.page")),
            ]
        )
        self._refuse_result(rebound, "verification_evidence_mismatch")

    def test_an_entry_may_not_restate_another_source_type(self) -> None:
        request = _request()
        answered = [_answer(item) for item in request["evidence"]]  # type: ignore[union-attr]
        answered[0]["source_type"] = "knowledge.answer"
        self._refuse_result(_result(evidence=answered), "verification_evidence_mismatch")

    def test_an_entry_may_not_restate_another_expected_version(self) -> None:
        request = _request()
        answered = [_answer(item) for item in request["evidence"]]  # type: ignore[union-attr]
        answered[0]["expected_source_version"] = VERSION_TWO
        answered[0]["observed_source_version"] = VERSION_TWO
        self._refuse_result(_result(evidence=answered), "verification_evidence_mismatch")

    def test_result_entry_shape_is_closed(self) -> None:
        request = _request()
        answered = [_answer(item) for item in request["evidence"]]  # type: ignore[union-attr]
        answered[0]["indexed_digest"] = "a" * 64
        self._refuse_result(
            _result(evidence=answered), "invalid_verification_evidence"
        )
        trimmed = [_answer(item) for item in request["evidence"]]  # type: ignore[union-attr]
        del trimmed[0]["code"]
        self._refuse_result(
            _result(evidence=trimmed), "invalid_verification_evidence"
        )

    def test_an_observed_version_cannot_carry_a_path(self) -> None:
        request = _request()
        answered = [_answer(item) for item in request["evidence"]]  # type: ignore[union-attr]
        answered[0]["observed_source_version"] = "/mnt/share/payroll.xlsx"
        self._refuse_result(
            _result(evidence=answered), "invalid_verification_observed_version"
        )


class ResultStatusTests(_ProtocolTestCase):
    def _one_entry_request(self, **overrides: object) -> dict[str, object]:
        return validate_verification_request(
            _request(evidence=[_entry(**overrides)]), now=NOW
        )

    def _refuse_single(
        self, request: dict[str, object], answered: dict[str, object], code: str
    ) -> None:
        with self.assertRaises(VerificationError) as caught:
            validate_verification_result(
                _result(evidence=[answered]), request=request, now=NOW
            )
        self.assertEqual(caught.exception.code, code)

    def test_an_undeclared_status_or_code_pairing_is_refused(self) -> None:
        request = self._one_entry_request()
        for status, code in (
            ("current", "hash_differs"),
            ("stale", "hash_matched"),
            ("current", "verification_unavailable"),
            ("unknown", "hash_matched"),
            ("current", ""),
        ):
            self._refuse_single(
                request,
                _answer(_entry(), status=status, code=code),
                "invalid_verification_status",
            )

    def test_current_requires_two_versions_that_actually_agree(self) -> None:
        request = self._one_entry_request()
        self._refuse_single(
            request,
            _answer(_entry(), observed_source_version=VERSION_TWO),
            "verification_status_inconsistent",
        )
        self._refuse_single(
            request,
            _answer(_entry(), observed_source_version=None),
            "verification_status_inconsistent",
        )

    def test_no_expected_version_can_never_be_reported_as_current(self) -> None:
        request = self._one_entry_request(expected_source_version=None)
        self._refuse_single(
            request,
            _answer(
                _entry(expected_source_version=None),
                observed_source_version=VERSION_ONE,
            ),
            "verification_status_inconsistent",
        )
        self._refuse_single(
            request,
            _answer(
                _entry(expected_source_version=None), observed_source_version=None
            ),
            "verification_status_inconsistent",
        )

    def test_stale_requires_two_versions_that_actually_differ(self) -> None:
        request = self._one_entry_request()
        self._refuse_single(
            request,
            _answer(_entry(), status="stale", code="hash_differs"),
            "verification_status_inconsistent",
        )
        self._refuse_single(
            request,
            _answer(
                _entry(),
                status="stale",
                code="hash_differs",
                observed_source_version=None,
            ),
            "verification_status_inconsistent",
        )
        request_without = self._one_entry_request(expected_source_version=None)
        self._refuse_single(
            request_without,
            _answer(
                _entry(expected_source_version=None),
                status="stale",
                code="hash_differs",
                observed_source_version=VERSION_TWO,
            ),
            "verification_status_inconsistent",
        )

    def test_every_other_status_must_observe_nothing(self) -> None:
        request = self._one_entry_request()
        for status, code in VERIFICATION_OUTCOMES:
            if status in ("current", "stale"):
                continue
            self._refuse_single(
                request,
                _answer(
                    _entry(),
                    status=status,
                    code=code,
                    observed_source_version=VERSION_TWO,
                ),
                "verification_status_inconsistent",
            )


class ResultCheckedAtTests(_ProtocolTestCase):
    def test_checked_at_must_lie_inside_the_window_and_at_or_before_now(self) -> None:
        for value in (
            "2026-09-09T11:59:59Z",
            EXPIRES_AT,
            "2026-09-09T12:02:00Z",
            "2026-09-09T12:00:11Z",
        ):
            self._refuse_result(
                _result(checked_at=value), "verification_checked_at_out_of_window"
            )

    def test_the_window_edges_themselves_are_admitted(self) -> None:
        admitted = self._admitted()
        for value in (REQUESTED_AT, NOW):
            result = validate_verification_result(
                _result(checked_at=value), request=admitted, now=NOW
            )
            self.assertEqual(result["checked_at"], value)

    def test_a_malformed_checked_at_is_refused(self) -> None:
        self._refuse_result(
            _result(checked_at="2026-09-09 12:00:05"), "invalid_verification_timestamp"
        )
        self._refuse_result(
            _result(checked_at=None), "invalid_verification_timestamp"
        )

    def test_a_late_owner_clock_still_admits_an_in_window_observation(self) -> None:
        admitted = self._admitted()
        result = validate_verification_result(
            _result(), request=admitted, now="2026-09-09T12:00:59Z"
        )
        self.assertEqual(result["checked_at"], "2026-09-09T12:00:05Z")

    def test_a_backdated_result_arriving_after_the_window_is_refused(self) -> None:
        """The root integration counterexample, exactly as stated.

        Issued 16:00, expires 16:01, the verifier says it looked at 16:00:30,
        and the answer only reaches the owner at 16:02. The observation's own
        timestamp is honest and inside the window; the *arrival* is not, so a
        bounded observation must refuse it rather than display a two-minute-old
        currentness claim as if the check had just been made.
        """

        request = validate_verification_request(
            _request(
                requested_at="2026-09-09T16:00:00Z",
                expires_at="2026-09-09T16:01:00Z",
            ),
            now="2026-09-09T16:00:00Z",
        )
        document = _result(checked_at="2026-09-09T16:00:30Z")
        with self.assertRaises(VerificationError) as caught:
            validate_verification_result(
                document, request=request, now="2026-09-09T16:02:00Z"
            )
        self.assertEqual(caught.exception.code, "verification_expired")
        # The same document at the same claimed instant is admitted while the
        # window is still open, so the refusal is the arrival and nothing else.
        admitted = validate_verification_result(
            document, request=request, now="2026-09-09T16:00:59Z"
        )
        self.assertEqual(admitted["checked_at"], "2026-09-09T16:00:30Z")

    def test_arrival_exactly_at_expiry_is_already_too_late(self) -> None:
        admitted = self._admitted()
        with self.assertRaises(VerificationError) as caught:
            validate_verification_result(_result(), request=admitted, now=EXPIRES_AT)
        self.assertEqual(caught.exception.code, "verification_expired")


class BindingAndIsolationTests(unittest.TestCase):
    def test_the_verifier_binding_hides_its_argv_and_environment(self) -> None:
        binding = KnowledgeVerificationBinding(
            command=("/opt/adapters/verifier", "--verify"),
            environment={"OD_KEY_FILE": "/etc/secrets/od.key"},
        )
        rendered = repr(binding)
        self.assertEqual(rendered, "KnowledgeVerificationBinding()")
        self.assertNotIn("verifier", rendered)
        self.assertNotIn("od.key", rendered)

    def test_the_verifier_binding_is_frozen(self) -> None:
        binding = KnowledgeVerificationBinding(command=("/x",), environment={})
        with self.assertRaises(Exception):
            binding.command = ("/y",)  # type: ignore[misc]

    def test_the_admitted_projection_is_independent_of_the_submitted_document(
        self,
    ) -> None:
        document = _request()
        admitted = validate_verification_request(document, now=NOW)
        document["evidence"][0]["document_ref"] = DOC_TWO  # type: ignore[index]
        document["corpus_refs"].append("late")  # type: ignore[union-attr]
        document["binding"]["capture_revision"] = 99  # type: ignore[index]
        self.assertEqual(admitted["evidence"][0]["document_ref"], DOC_ONE)
        self.assertEqual(admitted["corpus_refs"], ["engineering", "policy"])
        self.assertEqual(admitted["binding"]["capture_revision"], 3)

    def test_the_protocol_imports_no_runtime_or_provider_module(self) -> None:
        source = (ROOT / "workstack" / "knowledge_verification_protocol.py").read_text(
            encoding="utf-8"
        )
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertNotIn("knowledge_execution_runtime", imported)
        self.assertNotIn("knowledge_driver_registry", imported)
        self.assertNotIn("knowledge_driver_exchange", imported)
        self.assertFalse({name for name in imported if "opendocuments" in name})
        self.assertFalse({name for name in imported if name.startswith("store")})


if __name__ == "__main__":
    unittest.main()

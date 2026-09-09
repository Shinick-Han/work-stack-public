"""KnowledgeRequest v1 boundaries: scope, binding, window and wire hygiene.

Every document here is synthetic. No Store is opened, no vault is read, no
corpus is queried and no connector is contacted, so a green run says the
validation primitive holds against fixtures. It does not say a real
OpenDocuments exchange was accepted end to end, and it is not evidence of a
customer acceptance: authentication, the host-issued request ledger and replay
refusal are separate gates this module deliberately does not implement.
"""

from __future__ import annotations

import copy
import json
import unittest

from workstack.knowledge_request import (
    MAX_ACTIVE_SECONDS,
    MAX_CORPUS_REFS,
    MAX_QUERY_CHARS,
    MAX_RESULT_LIMIT,
    MAX_TASK_REVISION,
    PURPOSES,
    SCHEMA,
    ActiveTask,
    KnowledgeRequestError,
    RequestAuthority,
    decode_knowledge_request,
    decode_strict_json,
    is_expired,
    payload_bytes,
    validate_knowledge_request,
)

WORKSPACE = "66666666-6666-4666-8666-666666666666"
OTHER_WORKSPACE = "55555555-5555-4555-8555-555555555555"
TASK_UID = "77777777-7777-4777-8777-777777777777"
OTHER_TASK_UID = "88888888-8888-4888-8888-888888888888"
REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GRANTED = ("nas-team-share", "notion-product", "wiki.personal")
REQUESTED_AT = "2026-09-08T09:00:00Z"
EXPIRES_AT = "2026-09-08T09:05:00Z"


def request(**overrides: object) -> dict:
    document = {
        "schema": SCHEMA,
        "request_id": REQUEST_ID,
        "binding": {
            "workspace_uid": WORKSPACE,
            "task_uid": TASK_UID,
            "task_id": "T-0033",
            "task_revision": 2,
        },
        "purpose": "find_context",
        "query": "rollback verification owner",
        "corpus_refs": ["nas-team-share", "notion-product"],
        "result_limit": 5,
        "requested_at": REQUESTED_AT,
        "expires_at": EXPIRES_AT,
    }
    document.update(overrides)
    return document


def authority(**overrides: object) -> RequestAuthority:
    fields: dict = {
        "workspace_uid": WORKSPACE,
        "granted_corpus_refs": GRANTED,
        "now": "2026-09-08T09:00:30Z",
        "active_task": ActiveTask(TASK_UID, "T-0033", 2),
    }
    fields.update(overrides)
    return RequestAuthority(**fields)  # type: ignore[arg-type]


class KnowledgeRequestAcceptanceTest(unittest.TestCase):
    def test_valid_request_projects_exactly_the_closed_fields(self):
        result = validate_knowledge_request(request(), authority())
        self.assertEqual(
            result,
            {
                "schema": SCHEMA,
                "request_id": REQUEST_ID,
                "binding": {
                    "workspace_uid": WORKSPACE,
                    "task_uid": TASK_UID,
                    "task_id": "T-0033",
                    "task_revision": 2,
                },
                "purpose": "find_context",
                "query": "rollback verification owner",
                "corpus_refs": ["nas-team-share", "notion-product"],
                "result_limit": 5,
                "requested_at": REQUESTED_AT,
                "expires_at": EXPIRES_AT,
            },
        )

    def test_every_declared_purpose_is_accepted_and_nothing_else_is(self):
        for purpose in PURPOSES:
            with self.subTest(purpose=purpose):
                result = validate_knowledge_request(
                    request(purpose=purpose), authority()
                )
                self.assertEqual(result["purpose"], purpose)
        for rejected in ("summarize", "FIND_CONTEXT", "", None, ["find_context"]):
            with self.subTest(rejected=rejected):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(purpose=rejected), authority())
                self.assertEqual(caught.exception.code, "invalid_purpose")

    def test_workspace_only_binding_is_accepted_when_no_task_is_open(self):
        document = request(binding={"workspace_uid": WORKSPACE})
        result = validate_knowledge_request(
            document, authority(active_task=None)
        )
        self.assertEqual(result["binding"], {"workspace_uid": WORKSPACE})

    def test_query_is_trimmed_but_never_otherwise_rewritten(self):
        result = validate_knowledge_request(
            request(query="  rollback  verification  "), authority()
        )
        self.assertEqual(result["query"], "rollback  verification")

    def test_request_is_data_only_and_the_input_is_not_mutated(self):
        document = request()
        before = copy.deepcopy(document)
        validate_knowledge_request(document, authority())
        self.assertEqual(document, before)


class KnowledgeRequestScopeTest(unittest.TestCase):
    def test_workspace_mismatch_is_refused_even_with_a_valid_uuid(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(
                request(binding={
                    "workspace_uid": OTHER_WORKSPACE,
                    "task_uid": TASK_UID,
                    "task_id": "T-0033",
                    "task_revision": 2,
                }),
                authority(),
            )
        self.assertEqual(caught.exception.code, "workspace_mismatch")

    def test_task_uid_display_id_and_revision_must_all_match_the_open_task(self):
        mismatches = (
            {"task_uid": OTHER_TASK_UID},
            {"task_id": "T-0034"},
            {"task_revision": 1},
            {"task_revision": 3},
        )
        for override in mismatches:
            with self.subTest(override=tuple(override)):
                binding = dict(request()["binding"])
                binding.update(override)
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(
                        request(binding=binding), authority()
                    )
                self.assertEqual(caught.exception.code, "task_binding_mismatch")

    def test_display_id_case_is_normalized_rather_than_treated_as_a_new_task(self):
        binding = dict(request()["binding"], task_id="t-0033")
        result = validate_knowledge_request(request(binding=binding), authority())
        self.assertEqual(result["binding"]["task_id"], "T-0033")

    def test_an_open_task_cannot_be_dropped_from_the_binding(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(
                request(binding={"workspace_uid": WORKSPACE}), authority()
            )
        self.assertEqual(caught.exception.code, "task_binding_required")

    def test_a_task_binding_without_an_open_task_is_refused(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(request(), authority(active_task=None))
        self.assertEqual(caught.exception.code, "task_binding_mismatch")

    def test_the_task_trio_is_all_or_nothing(self):
        partials = (
            {"workspace_uid": WORKSPACE, "task_uid": TASK_UID},
            {"workspace_uid": WORKSPACE, "task_uid": TASK_UID, "task_id": "T-0033"},
            {"workspace_uid": WORKSPACE, "task_revision": 2},
        )
        for binding in partials:
            with self.subTest(keys=tuple(sorted(binding))):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(binding=binding), authority())
                self.assertEqual(caught.exception.code, "invalid_task_binding")

    def test_the_display_id_must_keep_the_existing_task_id_grammar(self):
        for value in ("0033", "T-33", "TASK-0033", "", None, 33):
            with self.subTest(value=value):
                binding = dict(request()["binding"], task_id=value)
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(binding=binding), authority())
                self.assertEqual(caught.exception.code, "invalid_task_binding")
                self.assertEqual(
                    caught.exception.details, {"field": "binding.task_id"}
                )

    def test_no_task_detail_field_may_ride_along_with_the_binding(self):
        binding = dict(request()["binding"], title="Release quality review")
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(request(binding=binding), authority())
        self.assertEqual(caught.exception.code, "invalid_task_binding")

    def test_a_request_cannot_grant_itself_a_corpus_the_caller_lacks(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(
                request(corpus_refs=["nas-team-share", "finance-vault"]), authority()
            )
        self.assertEqual(caught.exception.code, "corpus_not_granted")

    def test_an_empty_grant_admits_no_corpus_at_all(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(
                request(), authority(granted_corpus_refs=())
            )
        self.assertEqual(caught.exception.code, "corpus_not_granted")

    def test_corpus_refs_are_bounded_unique_and_alias_shaped(self):
        cases = {
            "invalid_corpus_refs": (
                [],
                ["nas-team-share"] * (MAX_CORPUS_REFS + 1),
                ["NAS-TEAM-SHARE"],
                ["nas/team/share"],
                ["../nas-team-share"],
                ["\\\\host\\share"],
                ["nas team share"],
                [""],
                [None],
                ["nas-team-share-" + "x" * 64],
                "nas-team-share",
            ),
            "duplicate_corpus_ref": (["nas-team-share", "nas-team-share"],),
        }
        for code, values in cases.items():
            for value in values:
                with self.subTest(code=code, value=value):
                    with self.assertRaises(KnowledgeRequestError) as caught:
                        validate_knowledge_request(
                            request(corpus_refs=value), authority()
                        )
                    self.assertEqual(caught.exception.code, code)

    def test_the_full_granted_set_fits_inside_the_eight_ref_bound(self):
        granted = tuple("corpus-{}".format(index) for index in range(MAX_CORPUS_REFS))
        result = validate_knowledge_request(
            request(corpus_refs=list(granted)),
            authority(granted_corpus_refs=granted),
        )
        self.assertEqual(result["corpus_refs"], list(granted))

    def test_authority_must_be_the_declared_type_not_a_body_derived_mapping(self):
        forged = {
            "workspace_uid": WORKSPACE,
            "granted_corpus_refs": GRANTED,
            "now": "2026-09-08T09:00:30Z",
            "active_task": None,
        }
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(request(), forged)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "invalid_authority")

    def test_a_malformed_authority_refuses_instead_of_raising_a_python_error(self):
        # The authority is trusted, but corrupt host state is not a valid wire
        # result: it must leave as a closed refusal naming only a field.
        cases = (
            (authority(active_task=ActiveTask(7, "T-0033", 2)),
             "active_task.task_uid"),
            (authority(active_task=ActiveTask(TASK_UID, 33, 2)),
             "active_task.task_id"),
            (authority(active_task=ActiveTask(TASK_UID, "T-0033", "2")),
             "active_task.task_revision"),
            (authority(active_task=ActiveTask(TASK_UID, "T-0033", True)),
             "active_task.task_revision"),
            (authority(active_task={"task_uid": TASK_UID}), "active_task"),
            (authority(workspace_uid=None), "workspace_uid"),
            (authority(now=0), "now"),
            (authority(granted_corpus_refs=list(GRANTED)), "granted_corpus_refs"),
            (authority(granted_corpus_refs=(["nas-team-share"],)),
             "granted_corpus_refs"),
        )
        for value, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(), value)
                self.assertEqual(caught.exception.code, "invalid_authority")
                self.assertEqual(caught.exception.field, field)
                self.assertEqual(caught.exception.details, {"field": field})


class KnowledgeRequestWindowTest(unittest.TestCase):
    def test_a_five_minute_window_is_the_longest_accepted_one(self):
        result = validate_knowledge_request(
            request(expires_at="2026-09-08T09:05:00Z"), authority()
        )
        self.assertEqual(result["expires_at"], "2026-09-08T09:05:00Z")

    def test_a_window_longer_than_five_minutes_is_refused(self):
        for expires in (
            "2026-09-08T09:05:01Z",
            "2026-09-08T09:05:00.001Z",
            "2026-09-08T10:00:00Z",
            "2026-09-09T09:00:00Z",
        ):
            with self.subTest(expires=expires):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(
                        request(expires_at=expires), authority()
                    )
                self.assertEqual(caught.exception.code, "invalid_request_window")

    def test_a_fractional_window_is_measured_exactly_not_by_whole_seconds(self):
        accepted = request(
            requested_at="2026-09-08T09:00:00.500Z",
            expires_at="2026-09-08T09:05:00.500Z",
        )
        self.assertEqual(
            validate_knowledge_request(accepted, authority())["expires_at"],
            "2026-09-08T09:05:00.500Z",
        )
        refused = request(
            requested_at="2026-09-08T09:00:00.500Z",
            expires_at="2026-09-08T09:05:00.600Z",
        )
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(refused, authority())
        self.assertEqual(caught.exception.code, "invalid_request_window")

    def test_a_zero_length_or_backwards_window_bounds_nothing(self):
        for expires in ("2026-09-08T09:00:00Z", "2026-09-08T08:59:59Z"):
            with self.subTest(expires=expires):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(
                        request(expires_at=expires), authority()
                    )
                self.assertEqual(caught.exception.code, "invalid_request_window")

    def test_the_window_is_measured_across_offsets_not_local_wall_clocks(self):
        document = request(
            requested_at="2026-09-08T18:00:00+09:00",
            expires_at="2026-09-08T09:05:00Z",
        )
        result = validate_knowledge_request(document, authority())
        self.assertEqual(result["requested_at"], "2026-09-08T18:00:00+09:00")

    def test_an_expired_request_is_refused_at_and_after_the_boundary(self):
        for now in ("2026-09-08T09:05:00Z", "2026-09-08T09:05:00.001Z", "2027-01-01T00:00:00Z"):
            with self.subTest(now=now):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(), authority(now=now))
                self.assertEqual(caught.exception.code, "request_expired")

    def test_the_last_instant_before_expiry_is_still_active(self):
        result = validate_knowledge_request(
            request(), authority(now="2026-09-08T09:04:59.999Z")
        )
        self.assertEqual(result["request_id"], REQUEST_ID)

    def test_a_forward_dated_request_is_not_active_yet(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(
                request(), authority(now="2026-09-08T08:59:59Z")
            )
        self.assertEqual(caught.exception.code, "request_not_yet_active")

    def test_expiry_is_terminal_and_nothing_here_renews_a_window(self):
        accepted = validate_knowledge_request(request(), authority())
        self.assertFalse(is_expired(accepted, "2026-09-08T09:04:59Z"))
        self.assertTrue(is_expired(accepted, "2026-09-08T09:05:00Z"))
        # Reading the expiry again does not move it: a lapsed request stays
        # lapsed and must be raised afresh for the user to review.
        self.assertTrue(is_expired(accepted, "2026-09-08T09:05:00Z"))
        self.assertTrue(is_expired(accepted, "2026-09-08T09:06:00Z"))
        import workstack.knowledge_request as module

        for forbidden in ("renew", "extend", "refresh", "reactivate", "touch"):
            with self.subTest(name=forbidden):
                self.assertFalse(
                    any(
                        name.startswith(forbidden) or forbidden in name
                        for name in dir(module)
                    )
                )

    def test_the_five_minute_bound_is_the_documented_one(self):
        self.assertEqual(MAX_ACTIVE_SECONDS, 300)

    def test_malformed_timestamps_are_refused_without_guessing_an_instant(self):
        for field in ("requested_at", "expires_at"):
            for value in (
                "2026-09-08 09:00:00Z",
                "2026-09-08T09:00:00",
                "2026-13-01T09:00:00Z",
                "2026-09-08T24:00:00Z",
                "",
                None,
                1757322000,
                "2026-09-08T09:00:00Z" + "0" * 64,
            ):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(KnowledgeRequestError) as caught:
                        validate_knowledge_request(
                            request(**{field: value}), authority()
                        )
                    self.assertEqual(caught.exception.code, "invalid_timestamp")

    def test_a_malformed_host_clock_is_refused_rather_than_defaulted(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(request(), authority(now="not-a-time"))
        self.assertEqual(caught.exception.code, "invalid_timestamp")


class KnowledgeRequestValueTest(unittest.TestCase):
    def test_identifiers_must_be_canonical_non_nil_uuids(self):
        for value in (
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
            "{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}",
            "urn:uuid:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa",
            "00000000-0000-0000-0000-000000000000",
            "not-a-uuid",
            "",
            None,
            12345,
            ["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"],
        ):
            with self.subTest(value=value):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(request_id=value), authority())
                self.assertEqual(caught.exception.code, "invalid_uuid")

    def test_result_limit_rejects_booleans_floats_and_huge_integers(self):
        for value, code in (
            (True, "invalid_number"),
            (False, "invalid_number"),
            (5.0, "invalid_number"),
            ("5", "invalid_number"),
            (None, "invalid_number"),
            (0, "out_of_range"),
            (-1, "out_of_range"),
            (MAX_RESULT_LIMIT + 1, "out_of_range"),
            (2**64, "out_of_range"),
            (10**400, "out_of_range"),
        ):
            with self.subTest(value=value):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(result_limit=value), authority())
                self.assertEqual(caught.exception.code, code)

    def test_result_limit_accepts_the_declared_range_only(self):
        for value in (1, MAX_RESULT_LIMIT):
            with self.subTest(value=value):
                authorized = authority()
                result = validate_knowledge_request(
                    request(result_limit=value), authorized
                )
                self.assertEqual(result["result_limit"], value)

    def test_task_revision_rejects_booleans_and_out_of_range_integers(self):
        for value, code in (
            (True, "invalid_number"),
            (2.0, "invalid_number"),
            ("2", "invalid_number"),
            (-1, "out_of_range"),
            (MAX_TASK_REVISION + 1, "out_of_range"),
            (10**400, "out_of_range"),
        ):
            with self.subTest(value=value):
                binding = dict(request()["binding"], task_revision=value)
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(binding=binding), authority())
                self.assertEqual(caught.exception.code, code)

    def test_query_bounds_are_enforced_at_both_ends(self):
        longest = "q" * MAX_QUERY_CHARS
        self.assertEqual(
            validate_knowledge_request(request(query=longest), authority())["query"],
            longest,
        )
        for value in ("", "   ", "q" * (MAX_QUERY_CHARS + 1), None, 5, ["q"]):
            with self.subTest(value=repr(value)[:32]):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(query=value), authority())
                self.assertEqual(caught.exception.code, "invalid_query")

    def test_control_characters_never_reach_a_query(self):
        for character in ("\n", "\t", "\r", "\x00", "\x1b", "\x7f", "\x85"):
            with self.subTest(character=repr(character)):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(
                        request(query="rollback" + character + "owner"), authority()
                    )
                self.assertEqual(caught.exception.code, "invalid_query")

    def test_a_credential_pasted_into_a_query_is_refused_not_forwarded(self):
        for value in (
            "Authorization: Bearer abcdefghijklmnopqrstuvwx",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
            "client_secret=Zm9vYmFyYmF6cXV4MTIzNA",
        ):
            with self.subTest(value=value[:24]):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(query=value), authority())
                self.assertEqual(
                    caught.exception.code, "credential_material_suspected"
                )

    def test_a_query_past_the_percent_decoding_bound_is_refused_not_decoded(self):
        buried = "%41"
        for _ in range(7):
            buried = buried.replace("%", "%25")
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(request(query=buried), authority())
        self.assertEqual(caught.exception.code, "invalid_query")

    def test_non_string_object_keys_are_refused_at_every_level(self):
        document = request()
        document[7] = "seven"  # type: ignore[index]
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(document, authority())
        self.assertEqual(caught.exception.code, "invalid_request")
        binding = dict(request()["binding"])
        binding[7] = "seven"  # type: ignore[index]
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(request(binding=binding), authority())
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_a_refusal_never_echoes_the_submitted_value(self):
        secret = "client_secret=Zm9vYmFyYmF6cXV4MTIzNA"
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(request(query=secret), authority())
        rendered = "{}|{}".format(caught.exception, caught.exception.details)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("Zm9vYmFy", rendered)
        self.assertEqual(caught.exception.details, {"field": "query"})

    def test_unknown_and_missing_fields_are_refused_at_every_level(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(request(tools=["opendocuments.ask"]), authority())
        self.assertEqual(caught.exception.code, "unknown_field")
        stripped = request()
        del stripped["corpus_refs"]
        with self.assertRaises(KnowledgeRequestError) as caught:
            validate_knowledge_request(stripped, authority())
        self.assertEqual(caught.exception.code, "missing_field")

    def test_a_claimed_provider_or_tool_grant_in_the_body_is_not_a_field(self):
        # Claiming a connector in the payload is not authentication; the schema
        # has nowhere to put such a claim, and an unknown field is refused.
        for override in (
            {"provider": "opendocuments"},
            {"tools": ["opendocuments.ask"]},
            {"authority": {"opendocuments.ask": True}},
        ):
            with self.subTest(field=tuple(override)):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(**override), authority())
                self.assertEqual(caught.exception.code, "unknown_field")

    def test_a_wrong_schema_string_is_refused_before_anything_else_is_trusted(self):
        for value in (
            "workstack.knowledge-request.v2",
            "workstack-knowledge-request",
            "",
            None,
        ):
            with self.subTest(value=value):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(request(schema=value), authority())
                self.assertEqual(caught.exception.code, "unsupported_schema")

    def test_a_non_object_document_is_refused(self):
        for value in ([], "{}", 5, None, True):
            with self.subTest(value=value):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    validate_knowledge_request(value, authority())
                self.assertEqual(caught.exception.code, "invalid_request")


class KnowledgeRequestDecodeTest(unittest.TestCase):
    def test_a_well_formed_payload_decodes_and_validates(self):
        payload = json.dumps(request())
        result = validate_knowledge_request(
            decode_knowledge_request(payload), authority()
        )
        self.assertEqual(result["request_id"], REQUEST_ID)
        self.assertEqual(
            validate_knowledge_request(
                decode_knowledge_request(payload.encode("utf-8")), authority()
            ),
            result,
        )

    def test_duplicate_json_keys_have_no_single_meaning(self):
        payload = (
            '{"schema": "%s", "result_limit": 1, "result_limit": 10}' % SCHEMA
        )
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_knowledge_request(payload)
        self.assertEqual(caught.exception.code, "duplicate_json_key")

    def test_non_finite_numbers_are_refused_by_the_decoder(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    decode_knowledge_request('{"result_limit": %s}' % literal)
                self.assertEqual(caught.exception.code, "non_finite_number")

    def test_a_standard_json_number_that_overflows_to_infinity_is_refused(self):
        # ``1e9999`` is legal JSON syntax that Python's decoder answers ``inf``
        # for, so ``parse_constant`` never sees it. A decoder that promises
        # finite numbers has to refuse it itself.
        for literal in ("1e9999", "-1e9999", "1E400"):
            with self.subTest(literal=literal):
                with self.assertRaises(KnowledgeRequestError) as caught:
                    decode_knowledge_request('{"score": %s}' % literal)
                self.assertEqual(caught.exception.code, "non_finite_number")

    def test_ordinary_finite_numbers_still_decode(self):
        decoded = decode_knowledge_request('{"score": 0.62, "limit": 5}')
        self.assertEqual(decoded, {"score": 0.62, "limit": 5})
        # A huge *integer* stays exact and is bounded by the field rules, not
        # by the decoder; only float overflow is a decoding refusal.
        self.assertEqual(
            decode_knowledge_request('{"n": 10000000000000000000000}')["n"],
            10 ** 22,
        )

    def test_measured_payload_bytes_are_the_encoded_octets(self):
        self.assertEqual(payload_bytes("ab"), b"ab")
        # Non-ASCII costs what it costs on the wire, not what len() says.
        self.assertEqual(len(payload_bytes("\uac00")), 3)
        self.assertEqual(payload_bytes(bytearray(b"xy")), b"xy")
        with self.assertRaises(KnowledgeRequestError) as caught:
            payload_bytes(5)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "invalid_request")
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_strict_json("\ud800", maximum_bytes=64)
        self.assertEqual(caught.exception.code, "invalid_encoding")

    def test_oversize_and_undecodable_payloads_are_refused(self):
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_knowledge_request(b'{"query": "' + b"q" * 9000 + b'"}')
        self.assertEqual(caught.exception.code, "request_too_large")
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_knowledge_request(b'{"query": "\xff\xfe"}')
        self.assertEqual(caught.exception.code, "invalid_encoding")
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_knowledge_request('{"query": ')
        self.assertEqual(caught.exception.code, "invalid_json")
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_knowledge_request('{"query": "\ud800"}')
        self.assertEqual(caught.exception.code, "invalid_encoding")
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_knowledge_request(5)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_over_deep_nesting_is_malformed_input_not_a_crash(self):
        payload = "[" * 4000 + "]" * 4000
        with self.assertRaises(KnowledgeRequestError) as caught:
            decode_knowledge_request(payload)
        self.assertIn(
            caught.exception.code, {"request_too_deep", "invalid_json", "request_too_large"}
        )


if __name__ == "__main__":
    unittest.main()

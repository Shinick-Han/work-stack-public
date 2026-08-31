from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from workstack.capture import (
    CaptureValidationError,
    canonical_digest,
    fingerprint_for,
    parse_rfc3339,
    source_key_for,
    validate_capture_packet,
)
from workstack.service import (
    IdempotencyConflictError,
    SourceRevisionConflictError,
    StaleCaptureError,
    WorkStack,
)
from workstack.store import Store, StoreCorruptError, StoreLockedError


CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
DEMO_DATA = Path(__file__).resolve().parents[1] / "data"


def fixture(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def set_path(value: dict, path: str, replacement: object) -> None:
    parts = path.split(".")
    cursor: object = value
    for part in parts[:-1]:
        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]  # type: ignore[index]
    if isinstance(cursor, list):
        cursor[int(parts[-1])] = replacement
    else:
        cursor[parts[-1]] = replacement  # type: ignore[index]


def percent_encoding_layers(value: str, total_layers: int) -> str:
    for _ in range(total_layers - 1):
        value = value.replace("%", "%25")
    return value


class CaptureValidationTest(unittest.TestCase):
    def test_rfc3339_parser_accepts_only_the_frozen_wire_shape(self):
        valid = (
            "2026-08-29T08:00:00Z",
            "2026-08-29T08:00:00.123456789+09:00",
            "2024-02-29T23:59:59-00:00",
            "2026-01-01T00:00:00-23:59",
        )
        for value in valid:
            with self.subTest(valid=value):
                parsed = parse_rfc3339(value, "timestamp")
                self.assertEqual(parsed.utc_second.utcoffset(), dt.timedelta(0))

        self.assertLess(
            parse_rfc3339("2026-08-29T08:00:00.123456789Z", "timestamp"),
            parse_rfc3339("2026-08-29T08:00:00.123456790Z", "timestamp"),
        )
        self.assertEqual(
            parse_rfc3339("2026-08-29T08:00:00.1Z", "timestamp"),
            parse_rfc3339("2026-08-29T17:00:00.100+09:00", "timestamp"),
        )

        invalid = (
            "2026-W35-6T08:00:00Z",
            "2026-08-29 08:00:00Z",
            "2026-08-29T08:00:00",
            "2026-08-29t08:00:00z",
            "2026-08-29T08:00:00+0900",
            "2026-08-29T08:00:00+24:00",
            "2026-08-29T08:00:00+09:60",
            "2026-02-29T08:00:00Z",
            "2026-08-29T24:00:00Z",
            "2026-08-29T08:00:60Z",
            "2026-08-29T08:00:00.Z",
        )
        for value in invalid:
            with self.subTest(invalid=value):
                with self.assertRaises(CaptureValidationError):
                    parse_rfc3339(value, "timestamp")

    def test_frozen_positive_fixtures_validate(self):
        for name in (
            "capture-packet-v1.fixture.json",
            "capture-packet-v1.manual.fixture.json",
        ):
            packet = fixture(name)
            projected = validate_capture_packet(packet)
            self.assertEqual(projected["source_key"], packet["source_key"])
            self.assertEqual(projected["source"]["fingerprint"], packet["source"]["fingerprint"])

    def test_recursive_forbidden_key_is_rejected_before_projection(self):
        packet = fixture("capture-packet-v1.fixture.json")
        packet["unknown"] = {"nested": [{"ReCiPiEnTs": ["canary"]}]}
        with self.assertRaises(CaptureValidationError) as raised:
            validate_capture_packet(packet)
        self.assertEqual(raised.exception.code, "forbidden_capture_field")

    def test_every_frozen_value_negative_case_is_rejected(self):
        cases = fixture("capture-packet-v1.value-negative-cases.json")["cases"]
        for case in cases:
            with self.subTest(case=case["name"]):
                packet = fixture("capture-packet-v1.fixture.json")
                set_path(packet, case["path"], case["value"])
                with self.assertRaises(CaptureValidationError) as raised:
                    validate_capture_packet(packet)
                self.assertEqual(raised.exception.code, case["error_code"])

    def test_fingerprint_and_source_key_are_exact(self):
        packet = fixture("capture-packet-v1.fixture.json")
        packet["source"]["provider"] = "Microsoft-Outlook"
        with self.assertRaises(CaptureValidationError):
            validate_capture_packet(packet)
        packet = fixture("capture-packet-v1.fixture.json")
        packet["source"]["fingerprint"] = "sha256:" + "A" * 64
        with self.assertRaises(CaptureValidationError):
            validate_capture_packet(packet)

    def test_manual_provenance_cannot_claim_tool_evidence(self):
        packet = fixture("capture-packet-v1.manual.fixture.json")
        packet["provenance"]["model"] = "fabricated"
        with self.assertRaises(CaptureValidationError):
            validate_capture_packet(packet)

    def test_every_retained_metadata_section_rejects_raw_canary(self):
        canary = "RAW_CANARY_DO_NOT_STORE"
        cases = (
            ("source.resource_type", canary),
            ("source.connection_ref", canary),
            ("source.container_ref", canary),
            ("source.object_ref", canary),
            ("source.version_ref", canary),
            ("source.retrieved_at", canary),
            (
                "source.web_url",
                "https://outlook.office.com/mail/%52%41%57_CANARY_DO_NOT_STORE",
            ),
            ("provenance.adapter", canary),
            ("provenance.adapter_version", canary),
            ("provenance.model", canary),
            ("provenance.prompt_version", canary),
            ("provenance.redaction_policy_version", canary),
            ("provenance.created_at", canary),
        )
        for path, value in cases:
            with self.subTest(path=path):
                packet = fixture("capture-packet-v1.fixture.json")
                set_path(packet, path, value)
                packet["source_key"] = source_key_for(packet["source"])
                packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
                with self.assertRaises(CaptureValidationError) as raised:
                    validate_capture_packet(packet)
                self.assertEqual(raised.exception.code, "raw_content_suspected")

    def test_meaningful_microsoft_locator_tokens_remain_valid(self):
        packet = fixture("capture-packet-v1.fixture.json")
        packet["source"].update({
            "provider": "microsoft-teams",
            "resource_type": "teams.channelMessage",
            "connection_ref": "tenant:6f9619ff-8b86-d011-b42d-00cf4fc964ff",
            "container_ref": "channel:19:meeting_demo@thread.v2",
            "object_ref": "message:1740000000000",
            "version_ref": "etag:W/1234567890",
            "web_url": "https://teams.microsoft.com/l/message/19%3Ameeting_demo%40thread.v2/1740000000000",
        })
        packet["provenance"]["allowed_tools"] = [
            "m365.teams.read",
            "workstack.capture.write",
        ]
        packet["source_key"] = source_key_for(packet["source"])
        packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
        projected = validate_capture_packet(packet)
        self.assertEqual(
            projected["source"]["container_ref"],
            "channel:19:meeting_demo@thread.v2",
        )

    def test_microsoft_web_url_rejects_credential_query_and_fragment_material(self):
        credential_value = "synthetic-credential-value"
        access_name = "access" + "_token"
        refresh_name = "refresh" + "-token"
        id_name = "id" + "_token"
        client_secret_name = "client" + "_secret"
        urls = (
            "https://outlook.office.com/mail/read?{}={}".format(access_name, credential_value),
            "https://outlook.office.com/mail/read?{}={}".format(refresh_name, credential_value),
            "https://outlook.office.com/mail/read?{}={}".format(id_name, credential_value),
            "https://outlook.office.com/mail/read?{}={}".format(client_secret_name, credential_value),
            "https://outlook.office.com/mail/read?authorization=" + credential_value,
            "https://outlook.office.com/mail/read?code=" + credential_value,
            "https://outlook.office.com/mail/read#{}={}".format(access_name, credential_value),
            "https://outlook.office.com/mail/read#%61ccess_token%3D" + credential_value,
            (
                "https://outlook.office.com/mail/read?continue="
                "https%253A%252F%252Fexample.invalid%252Fcallback%253F"
                "access_token%253D" + credential_value
            ),
            "https://outlook.office.com/mail/read?payload=Bearer%20" + "a" * 26,
        )
        for url in urls:
            with self.subTest(url=url):
                packet = fixture("capture-packet-v1.fixture.json")
                packet["source"]["web_url"] = url
                with self.assertRaises(CaptureValidationError) as raised:
                    validate_capture_packet(packet)
                self.assertEqual(
                    raised.exception.code, "credential_material_suspected"
                )

    def test_legitimate_microsoft_deep_link_parameters_remain_valid(self):
        urls = (
            (
                "https://outlook.office.com/mail/deeplink/read/opaque"
                "?ItemID=opaque-item&exvsurl=1&path=%2Fmail%2Finbox"
            ),
            (
                "https://teams.microsoft.com/l/message/19%3Ademo%40thread.v2/1740000000000"
                "?tenantId=opaque-tenant&groupId=opaque-group&"
                "context=%7B%22contextType%22%3A%22channel%22%7D"
            ),
            "https://outlook.office.com/mail/deeplink/read/opaque#path=/mail/inbox",
        )
        for url in urls:
            with self.subTest(url=url):
                packet = fixture("capture-packet-v1.fixture.json")
                packet["source"]["web_url"] = url
                projected = validate_capture_packet(packet)
                self.assertEqual(projected["source"]["web_url"], url)

    def test_personal_microsoft_web_hosts_used_by_the_desktop_shell_remain_valid(self):
        urls = (
            "https://outlook.live.com/mail/inbox/id/opaque",
            "https://teams.live.com/v2/",
            "https://onedrive.live.com/edit.aspx?resid=opaque",
            "https://login.live.com/",
        )
        for url in urls:
            with self.subTest(url=url):
                packet = fixture("capture-packet-v1.fixture.json")
                packet["source"]["web_url"] = url
                projected = validate_capture_packet(packet)
                self.assertEqual(projected["source"]["web_url"], url)

        packet = fixture("capture-packet-v1.fixture.json")
        packet["source"]["web_url"] = "https://outlook.live.com.example.invalid/mail/"
        with self.assertRaises(CaptureValidationError):
            validate_capture_packet(packet)

    def test_microsoft_web_url_rejects_recipient_assignments(self):
        urls = (
            "https://outlook.office.com/mail/read?recipients=alice+bob",
            "https://outlook.office.com/mail/read?recipient%3Dalice",
            "https://teams.microsoft.com/l/message/opaque#to=alice",
        )
        for url in urls:
            with self.subTest(url=url):
                packet = fixture("capture-packet-v1.fixture.json")
                packet["source"]["web_url"] = url
                with self.assertRaises(CaptureValidationError) as raised:
                    validate_capture_packet(packet)
                self.assertEqual(raised.exception.code, "raw_content_suspected")

    def test_every_retained_capture_section_rejects_recipient_assignments(self):
        cases = (
            ("source.connection_ref", "recipients=alice+bob"),
            ("source.container_ref", "recipient:alice"),
            ("source.object_ref", "to:alice"),
            ("source.version_ref", "bcc=alice"),
            ("normalized.summary", "recipient%3Aalice"),
            ("normalized.action_items.0.detail", "recipients%3Dalice+bob"),
            ("provenance.adapter", "cc%3Aalice"),
        )
        for path, value in cases:
            with self.subTest(path=path):
                packet = fixture("capture-packet-v1.fixture.json")
                set_path(packet, path, value)
                packet["source_key"] = source_key_for(packet["source"])
                packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
                with self.assertRaises(CaptureValidationError) as raised:
                    validate_capture_packet(packet)
                self.assertEqual(raised.exception.code, "raw_content_suspected")

    def test_every_retained_capture_section_rejects_credential_shaped_text(self):
        bearer = "Bearer " + "a" * 32
        basic = "Basic " + "b" * 32
        oauth_assignment = "".join(("access", "_token", "=", "c" * 32))
        jwt = ".".join(("eyJ" + "d" * 12, "e" * 16, "f" * 16))
        private_key = "-----BEGIN " + "PRIVATE KEY-----"
        cases = (
            ("source.connection_ref", bearer),
            ("source.object_ref", jwt),
            ("source.display_title", oauth_assignment),
            ("normalized.summary", bearer),
            ("normalized.context", oauth_assignment),
            ("normalized.action_items.0.title", basic),
            ("normalized.action_items.0.detail", jwt),
            ("normalized.tags.0", private_key),
            ("provenance.adapter", bearer),
            ("provenance.adapter_version", oauth_assignment),
            ("provenance.model", jwt),
            ("provenance.prompt_version", basic),
            ("provenance.redaction_policy_version", private_key),
        )
        for path, value in cases:
            with self.subTest(path=path):
                packet = fixture("capture-packet-v1.fixture.json")
                set_path(packet, path, value)
                packet["source_key"] = source_key_for(packet["source"])
                packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
                with self.assertRaises(CaptureValidationError) as raised:
                    validate_capture_packet(packet)
                self.assertEqual(
                    raised.exception.code, "credential_material_suspected"
                )

    def test_percent_encoded_unsafe_retained_text_is_rejected(self):
        cases = (
            ("source.connection_ref", "Bearer%20" + "a" * 32, "credential_material_suspected"),
            (
                "source.container_ref",
                "access%5Ftoken%3D" + "b" * 20,
                "credential_material_suspected",
            ),
            (
                "normalized.summary",
                "recipient%40" + "example.invalid",
                "raw_content_suspected",
            ),
            (
                "normalized.context",
                "note%3A%3Cb%3Eraw%3C%2Fb%3E",
                "raw_content_suspected",
            ),
            (
                "normalized.action_items.0.detail",
                "RAW%5FCANARY%5FDO%5FNOT%5FSTORE",
                "raw_content_suspected",
            ),
            (
                "provenance.adapter",
                "From%3A%20synthetic%0ATo%3A%20synthetic",
                "raw_content_suspected",
            ),
            (
                "provenance.prompt_version",
                "On%20a%20synthetic%20date%2C%20someone%20wrote%3A",
                "raw_content_suspected",
            ),
        )
        for path, value, expected_code in cases:
            with self.subTest(path=path):
                packet = fixture("capture-packet-v1.fixture.json")
                set_path(packet, path, value)
                packet["source_key"] = source_key_for(packet["source"])
                packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
                with self.assertRaises(CaptureValidationError) as raised:
                    validate_capture_packet(packet)
                self.assertEqual(raised.exception.code, expected_code)

    def test_percent_decoding_depth_is_bounded_and_fails_closed(self):
        packet = fixture("capture-packet-v1.fixture.json")
        packet["source"]["connection_ref"] = percent_encoding_layers(
            "access%5Ftoken%3D" + "a" * 20, 6
        )
        packet["source_key"] = source_key_for(packet["source"])
        packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
        with self.assertRaises(CaptureValidationError) as raised:
            validate_capture_packet(packet)
        self.assertEqual(raised.exception.code, "encoded_content_too_deep")

        boundary = fixture("capture-packet-v1.fixture.json")
        boundary["source"]["connection_ref"] = percent_encoding_layers(
            "connection%3Aopaque", 5
        )
        boundary["source_key"] = source_key_for(boundary["source"])
        boundary["source"]["fingerprint"] = fingerprint_for(boundary["source"])
        projected = validate_capture_packet(boundary)
        self.assertEqual(
            projected["source"]["connection_ref"],
            boundary["source"]["connection_ref"],
        )

    def test_opaque_graph_identifiers_and_hashes_are_not_treated_as_credentials(self):
        packet = fixture("capture-packet-v1.fixture.json")
        packet["source"]["object_ref"] = (
            "AAMkAGI2TAAA=" + "AbCdEf0123456789_+/=" * 4
        )
        packet["source"]["version_ref"] = 'etag:W/"CQAAABYAAABase64LikeOpaqueId=="'
        packet["source_key"] = source_key_for(packet["source"])
        packet["source"]["fingerprint"] = fingerprint_for(packet["source"])
        projected = validate_capture_packet(packet)
        self.assertEqual(
            projected["source"]["object_ref"], packet["source"]["object_ref"]
        )


class CaptureServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.stack = WorkStack(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def test_ingest_duplicate_replay_conflict_and_no_raw_storage(self):
        packet = fixture("capture-packet-v1.fixture.json")
        result = self.stack.ingest_capture(packet, "ingest.key.0001")
        self.assertEqual(result["status"], 201)
        self.assertNotIn("body", json.dumps(self.store.load("captures.json")).casefold())

        replay = self.stack.ingest_capture(packet, "ingest.key.0001")
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])

        duplicate = self.stack.ingest_capture(packet, "ingest.key.0002")
        self.assertEqual(duplicate["status"], 200)
        self.assertTrue(duplicate["body"]["meta"]["duplicate"])
        self.assertEqual(len(self.store.load("captures.json")["captures"]), 1)

        altered = copy.deepcopy(packet)
        altered["normalized"]["summary"] += " Changed."
        with self.assertRaises(IdempotencyConflictError):
            self.stack.ingest_capture(altered, "ingest.key.0001")

    def test_update_keeps_action_id_and_rejects_stale_and_equal_time(self):
        packet = fixture("capture-packet-v1.fixture.json")
        first = self.stack.ingest_capture(packet, "ingest.key.1001")["body"]["data"]
        original_action_id = first["normalized"]["action_items"][0]["id"]

        updated = copy.deepcopy(packet)
        updated["source"]["version_ref"] = "change-key:demo-v2"
        updated["source"]["retrieved_at"] = "2026-08-29T09:00:00Z"
        updated["source"]["fingerprint"] = fingerprint_for(updated["source"])
        second = self.stack.ingest_capture(updated, "ingest.key.1002")["body"]["data"]
        self.assertEqual(second["revision"], 1)
        self.assertEqual(second["normalized"]["action_items"][0]["id"], original_action_id)

        with self.assertRaises(StaleCaptureError):
            self.stack.ingest_capture(
                fixture("capture-packet-v1.stale.json"), "ingest.key.1003"
            )

        equal = copy.deepcopy(updated)
        equal["source"]["version_ref"] = "change-key:conflict"
        equal["source"]["fingerprint"] = fingerprint_for(equal["source"])
        with self.assertRaises(SourceRevisionConflictError):
            self.stack.ingest_capture(equal, "ingest.key.1004")

    def test_submicrosecond_retrieval_order_is_not_truncated(self):
        first = fixture("capture-packet-v1.fixture.json")
        first["source"]["version_ref"] = "change-key:fraction-1"
        first["source"]["retrieved_at"] = "2026-08-29T09:00:00.123456789Z"
        first["source"]["fingerprint"] = fingerprint_for(first["source"])
        self.stack.ingest_capture(first, "ingest.fraction.0001")

        newer = copy.deepcopy(first)
        newer["source"]["version_ref"] = "change-key:fraction-2"
        newer["source"]["retrieved_at"] = "2026-08-29T09:00:00.123456790Z"
        newer["source"]["fingerprint"] = fingerprint_for(newer["source"])
        updated = self.stack.ingest_capture(newer, "ingest.fraction.0002")

        self.assertEqual(updated["status"], 200)
        self.assertEqual(updated["body"]["data"]["revision"], 1)

    def test_link_conversion_and_dismiss_are_retry_safe(self):
        capture = self.stack.ingest_capture(
            fixture("capture-packet-v1.fixture.json"), "flow.key.0001"
        )["body"]["data"]
        task = self.stack.add_task("Existing context task")
        linked = self.stack.link_capture(capture["id"], task["id"], "flow.key.0002")
        self.assertEqual(linked["body"]["data"]["linked_task_ids"], [task["id"]])
        duplicate_link = self.stack.link_capture(capture["id"], task["id"], "flow.key.0003")
        self.assertTrue(duplicate_link["body"]["meta"]["duplicate"])

        action_id = capture["normalized"]["action_items"][0]["id"]
        converted = self.stack.convert_capture_action(
            capture["id"], action_id, [], "flow.key.0004"
        )
        self.assertEqual(converted["status"], 201)
        repeated = self.stack.convert_capture_action(
            capture["id"], action_id, [], "flow.key.0005"
        )
        self.assertEqual(repeated["status"], 200)
        self.assertEqual(repeated["body"]["data"]["uid"], converted["body"]["data"]["uid"])
        self.assertEqual(len(self.stack.list_tasks(status="all")), 2)

        dismissed = self.stack.dismiss_capture(capture["id"], "flow.key.0006")
        self.assertEqual(dismissed["body"]["data"]["status"], "dismissed")
        dismissed_again = self.stack.dismiss_capture(capture["id"], "flow.key.0007")
        self.assertTrue(dismissed_again["body"]["meta"]["duplicate"])

    def test_rejected_packet_is_not_persisted(self):
        with self.assertRaises(CaptureValidationError):
            self.stack.ingest_capture(
                fixture("capture-packet-v1.negative-raw.json"), "negative.key.001"
            )
        self.assertEqual(self.store.load("captures.json")["captures"], [])
        self.assertNotIn(
            "WORKSTACK_RAW_CANARY_DO_NOT_STORE",
            json.dumps(self.store.load("activity.json")),
        )

    def test_canary_in_retained_locator_is_not_persisted_or_recorded(self):
        packet = fixture("capture-packet-v1.fixture.json")
        packet["source"]["object_ref"] = "RAW_CANARY_DO_NOT_STORE"
        packet["source_key"] = source_key_for(packet["source"])
        packet["source"]["fingerprint"] = fingerprint_for(packet["source"])

        with self.assertRaises(CaptureValidationError) as raised:
            self.stack.ingest_capture(packet, "negative.key.locator")
        self.assertEqual(raised.exception.code, "raw_content_suspected")

        captures = json.dumps(self.store.load("captures.json"))
        activity = json.dumps(self.store.load("activity.json"))
        self.assertNotIn("RAW_CANARY_DO_NOT_STORE", captures)
        self.assertNotIn("RAW_CANARY_DO_NOT_STORE", activity)
        self.assertEqual(self.store.load("captures.json")["captures"], [])
        self.assertEqual(self.store.load("activity.json")["idempotency"], [])


class StoreRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        self.store.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def _journal(self, value: dict, digest: str) -> dict:
        return {
            "version": 1,
            "operation_id": "test-replay",
            "created_at": "2026-08-29T10:00:00Z",
            "writes": [{"name": "backlog.json", "value": value, "sha256": digest}],
        }

    def _backlog(self, title: str) -> dict:
        del title
        return {"version": 3, "tasks": []}

    def test_valid_journal_replays_complete_value(self):
        value = self._backlog("Recovered")
        digest = canonical_digest(value)
        self.store.journal_path.write_text(
            json.dumps(self._journal(value, digest)), encoding="utf-8"
        )
        Store(self.root).initialize()
        self.assertEqual(Store(self.root).load("backlog.json"), value)
        self.assertFalse(self.store.journal_path.exists())

    def test_same_store_load_replays_a_pending_valid_journal(self):
        value = self._backlog("Recovered")
        self.store.journal_path.write_text(
            json.dumps(self._journal(value, canonical_digest(value))), encoding="utf-8"
        )
        self.assertEqual(self.store.load("backlog.json"), value)
        self.assertFalse(self.store.journal_path.exists())

    def test_nested_writer_refuses_to_overwrite_a_pending_journal(self):
        recovered = self._backlog("Journal wins")
        journal = self._journal(recovered, canonical_digest(recovered))
        with self.store.transaction():
            self.store._atomic_write_locked(self.store.journal_path, journal)
            with self.assertRaises(StoreCorruptError):
                self.store.save_many(
                    {"notes.json": {"version": 1, "notes": [{"id": "N-0001"}]}}
                )
        self.assertTrue(self.store.journal_path.exists())
        self.assertEqual(self.store.load("backlog.json"), recovered)
        self.assertFalse(self.store.journal_path.exists())
        self.assertEqual(self.store.load("notes.json")["notes"], [])

    def test_partial_convert_replays_all_targets_exactly_once(self):
        stack = WorkStack(self.store)
        capture = stack.ingest_capture(
            fixture("capture-packet-v1.fixture.json"), "recovery.ingest.0001"
        )["body"]["data"]
        action_id = capture["normalized"]["action_items"][0]["id"]
        conversion_key = "recovery.convert.0001"
        original_atomic_write = self.store._atomic_write_locked

        class SimulatedProcessCrash(BaseException):
            pass

        def crash_after_capture_replace(path: Path, value: object) -> None:
            original_atomic_write(path, value)
            if path == self.store.path("captures.json"):
                raise SimulatedProcessCrash()

        with mock.patch.object(
            self.store,
            "_atomic_write_locked",
            side_effect=crash_after_capture_replace,
        ):
            with self.assertRaises(SimulatedProcessCrash):
                stack.convert_capture_action(
                    capture["id"], action_id, [], conversion_key
        )

        self.assertTrue(self.store.journal_path.exists())
        partial_backlog = json.loads(
            self.store.path("backlog.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(partial_backlog["tasks"]), 1)
        partial_capture = json.loads(
            self.store.path("captures.json").read_text(encoding="utf-8")
        )["captures"][0]
        self.assertEqual(len(partial_capture["converted_task_ids"]), 1)
        partial_activity = json.loads(
            self.store.path("activity.json").read_text(encoding="utf-8")
        )
        self.assertFalse(
            any(
                item.get("key") == conversion_key
                for item in partial_activity["idempotency"]
            )
        )

        recovered_store = Store(self.root)
        recovered = WorkStack(recovered_store)
        self.assertFalse(recovered_store.journal_path.exists())

        tasks = recovered.list_tasks(status="all")
        captures = recovered_store.load("captures.json")["captures"]
        activity = recovered_store.load("activity.json")
        converted_events = [
            event
            for event in activity["activity"]
            if event.get("type") == "capture.action_converted"
        ]
        conversion_records = [
            item
            for item in activity["idempotency"]
            if item.get("key") == conversion_key
        ]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0]["converted_task_ids"], [tasks[0]["id"]])
        recovered_action = captures[0]["normalized"]["action_items"][0]
        self.assertEqual(recovered_action["task_id"], tasks[0]["id"])
        self.assertEqual(len(converted_events), 1)
        self.assertEqual(converted_events[0]["task_id"], tasks[0]["id"])
        self.assertEqual(len(conversion_records), 1)

        replay = recovered.convert_capture_action(
            capture["id"], action_id, [], conversion_key
        )
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(len(recovered.list_tasks(status="all")), 1)
        replayed_activity = recovered_store.load("activity.json")
        self.assertEqual(
            sum(
                event.get("type") == "capture.action_converted"
                for event in replayed_activity["activity"]
            ),
            1,
        )
        self.assertEqual(
            sum(
                item.get("key") == conversion_key
                for item in replayed_activity["idempotency"]
            ),
            1,
        )

    def test_bad_journal_and_bad_store_json_fail_closed(self):
        original = self.store.load("backlog.json")
        value = self._backlog("Corrupt digest")
        self.store.journal_path.write_text(
            json.dumps(self._journal(value, "sha256:" + "0" * 64)), encoding="utf-8"
        )
        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()
        self.assertEqual(
            json.loads(self.store.path("backlog.json").read_text(encoding="utf-8")),
            original,
        )
        self.assertTrue(self.store.journal_path.exists())
        self.store.journal_path.unlink()
        self.store.path("backlog.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(StoreCorruptError):
            Store(self.root).initialize()
        self.assertEqual(self.store.path("backlog.json").read_text(encoding="utf-8"), "{broken")

    def test_server_lease_excludes_other_store_writer(self):
        with self.store.server_lease():
            with self.assertRaises(StoreLockedError):
                Store(self.root).save("notes.json", {"version": 1, "notes": []})

    def test_demo_seed_is_explicit_and_never_overwrites_runtime_data(self):
        self.assertTrue(self.store.seed_demo(DEMO_DATA))
        self.assertEqual(len(self.store.load("backlog.json")["tasks"]), 30)
        with self.assertRaises(ValueError):
            self.store.seed_demo(DEMO_DATA)
        self.assertEqual(len(self.store.load("backlog.json")["tasks"]), 30)


if __name__ == "__main__":
    unittest.main()

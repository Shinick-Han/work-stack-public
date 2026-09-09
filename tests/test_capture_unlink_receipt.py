from __future__ import annotations

import json
import unittest
import uuid

from workstack.capture import SHA256_RE, canonical_digest
from workstack.capture_unlink_receipt import (
    EVENT_TYPE,
    FORMAT,
    RECEIPT_FIELDS,
    CaptureUnlinkReceiptError,
    admit_receipt_id,
    build_receipt,
    capture_row_digest,
    derive_receipt_id,
    find_unlink_receipt,
    receipt_id_for_idempotency_key,
    record_committed_unlink_receipt,
    serialize_receipt,
    validate_receipt,
)


WORKSPACE = "11111111-1111-4111-8111-111111111111"
OTHER_WORKSPACE = "22222222-2222-4222-8222-222222222222"
TASK_UID = "33333333-3333-4333-8333-333333333333"
KEY = "unlink.receipt.0001"
DIGEST = "sha256:" + ("ab" * 32)


def _receipt(**overrides: object) -> dict[str, object]:
    workspace = str(overrides.get("workspace_uid", WORKSPACE))
    key = str(overrides.get("idempotency_key", KEY))
    value: dict[str, object] = {
        "after_digest": DIGEST,
        "after_revision": 2,
        "before_revision": 1,
        "capture_id": "C-0001",
        "commit_state": "committed",
        "format": FORMAT,
        "idempotency_key": key,
        "receipt_id": derive_receipt_id(workspace_uid=workspace, idempotency_key=key),
        "schema_version": 1,
        "status_before": "linked",
        "task_id": "T-0001",
        "task_uid": TASK_UID,
        "workspace_uid": workspace,
    }
    value.update(overrides)
    return value


def _activity(*events: dict[str, object]) -> dict[str, object]:
    return {"activity": list(events)}


def _event(receipt: dict[str, object], **overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "id": "E-000001",
        "type": EVENT_TYPE,
        "created_at": "2026-09-09T00:00:00Z",
        "capture_id": receipt["capture_id"],
        "task_id": receipt["task_id"],
        "details": {"receipt": serialize_receipt(receipt).decode("utf-8")},
    }
    event.update(overrides)
    return event


class CaptureUnlinkReceiptClosedTest(unittest.TestCase):
    def test_closed_fields_round_trip_and_derive_receipt_id(self) -> None:
        receipt = validate_receipt(_receipt())
        self.assertEqual(set(receipt), set(RECEIPT_FIELDS))
        self.assertEqual(
            receipt["receipt_id"],
            derive_receipt_id(workspace_uid=WORKSPACE, idempotency_key=KEY),
        )
        self.assertEqual(serialize_receipt(receipt), serialize_receipt(_receipt()))
        self.assertEqual(admit_receipt_id(receipt["receipt_id"]), receipt["receipt_id"])
        self.assertRegex(receipt["after_digest"], SHA256_RE.pattern)

    def test_unknown_fields_bool_revision_and_noncanonical_values_are_malformed(
        self,
    ) -> None:
        cases = (
            {**_receipt(), "extra": "x"},
            {k: v for k, v in _receipt().items() if k != "task_uid"},
            {**_receipt(), "before_revision": True},
            {**_receipt(), "after_revision": 1.0},
            {**_receipt(), "after_revision": 4},
            {**_receipt(), "capture_id": "c-0001"},
            {**_receipt(), "task_id": "T-1"},
            {**_receipt(), "after_digest": "sha256:" + ("AB" * 32)},
            {**_receipt(), "status_before": "open"},
            {**_receipt(), "commit_state": "planned"},
            {**_receipt(), "schema_version": "1"},
            {**_receipt(), "workspace_uid": "11111111-1111-4111-8111-11111111111"},
            {**_receipt(), "receipt_id": str(uuid.UUID(int=0))},
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(CaptureUnlinkReceiptError) as raised:
                    validate_receipt(value)
                self.assertEqual(raised.exception.code, "malformed")

    def test_build_receipt_digests_the_raw_post_unlink_row(self) -> None:
        capture = {
            "id": "C-0001",
            "revision": 2,
            "status": "inbox",
            "linked_task_ids": [],
            "source": {"title": "one"},
        }
        task = {"id": "T-0001", "uid": TASK_UID}
        receipt = build_receipt(
            workspace_uid=WORKSPACE,
            capture=capture,
            task=task,
            status_before="linked",
            before_revision=1,
            idempotency_key=KEY,
        )
        self.assertEqual(receipt["after_digest"], capture_row_digest(capture))
        self.assertEqual(receipt["after_digest"], canonical_digest(dict(capture)))
        self.assertNotEqual(
            receipt["after_digest"],
            canonical_digest({"id": "C-0001", "revision": 2}),
        )

    def test_envelope_mismatch_foreign_workspace_and_noncanonical_blob_cannot_authorize(
        self,
    ) -> None:
        receipt = validate_receipt(_receipt())
        mismatched = _event(receipt, capture_id="C-0002")
        with self.assertRaises(CaptureUnlinkReceiptError) as mismatched_error:
            find_unlink_receipt(
                _activity(mismatched),
                receipt["receipt_id"],
                workspace_uid=WORKSPACE,
                capture_id="C-0001",
            )
        self.assertEqual(mismatched_error.exception.code, "malformed")

        pretty = json.dumps(receipt, indent=2)
        skipped = _event(receipt, details={"receipt": pretty})
        with self.assertRaises(CaptureUnlinkReceiptError) as skipped_error:
            find_unlink_receipt(
                _activity(skipped),
                receipt["receipt_id"],
                workspace_uid=WORKSPACE,
                capture_id="C-0001",
            )
        self.assertEqual(skipped_error.exception.code, "not_found")

        foreign = validate_receipt(_receipt(workspace_uid=OTHER_WORKSPACE))
        with self.assertRaises(CaptureUnlinkReceiptError) as foreign_error:
            find_unlink_receipt(
                _activity(_event(foreign)),
                foreign["receipt_id"],
                workspace_uid=WORKSPACE,
                capture_id="C-0001",
            )
        self.assertEqual(foreign_error.exception.code, "not_found")

    def test_same_id_conflicting_blob_is_conflict_and_identical_remint_is_stable(
        self,
    ) -> None:
        first = validate_receipt(_receipt())
        activity = _activity()
        recorded = record_committed_unlink_receipt(
            activity, first, "2026-09-09T00:00:00Z"
        )
        again = record_committed_unlink_receipt(
            activity, first, "2026-09-09T00:00:01Z"
        )
        self.assertEqual(serialize_receipt(recorded), serialize_receipt(again))
        self.assertEqual(len(activity["activity"]), 1)

        conflicted = dict(first)
        conflicted["status_before"] = "dismissed"
        with self.assertRaises(CaptureUnlinkReceiptError) as raised:
            record_committed_unlink_receipt(
                activity, conflicted, "2026-09-09T00:00:02Z"
            )
        self.assertEqual(raised.exception.code, "idempotency_conflict")
        self.assertEqual(len(activity["activity"]), 1)

        duplicate_event = _event(conflicted, id="E-000002")
        with self.assertRaises(CaptureUnlinkReceiptError) as found:
            find_unlink_receipt(
                _activity(_event(first), duplicate_event),
                first["receipt_id"],
                workspace_uid=WORKSPACE,
                capture_id="C-0001",
            )
        self.assertEqual(found.exception.code, "idempotency_conflict")


def _ordinary_unlinked() -> dict[str, object]:
    return {
        "id": "E-000099",
        "type": "capture.unlinked",
        "created_at": "2026-09-09T00:00:00Z",
        "capture_id": "C-0001",
        "task_id": "T-0001",
        "details": {},
    }


def _pretty_event(receipt: dict[str, object], **overrides: object) -> dict[str, object]:
    return _event(receipt, details={"receipt": json.dumps(receipt, indent=2)}, **overrides)


def _invalid_closed_body_event(
    receipt: dict[str, object], **overrides: object
) -> dict[str, object]:
    invalid = dict(receipt)
    invalid["extra"] = "x"
    blob = json.dumps(invalid, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return _event(receipt, details={"receipt": blob}, **overrides)


def _unidentifiable_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "id": "E-000080",
        "type": EVENT_TYPE,
        "created_at": "2026-09-09T00:00:00Z",
        "capture_id": "C-0001",
        "task_id": "T-0001",
        "details": {"receipt": "{not-json"},
    }
    event.update(overrides)
    return event


class CaptureUnlinkReceiptIdentityAggregationTest(unittest.TestCase):
    def _lookup(self, activity: dict[str, object], receipt_id: object) -> dict[str, object]:
        return find_unlink_receipt(
            activity,
            receipt_id,
            workspace_uid=WORKSPACE,
            capture_id="C-0001",
        )

    def _refuse_lookup(
        self, activity: dict[str, object], receipt_id: object, code: str
    ) -> None:
        with self.assertRaises(CaptureUnlinkReceiptError) as raised:
            self._lookup(activity, receipt_id)
        self.assertEqual(raised.exception.code, code)

    def test_canonical_plus_noncanonical_same_id_refuses_in_either_order(self) -> None:
        receipt = validate_receipt(_receipt())
        pretty = _pretty_event(receipt, id="E-000002")
        canonical = _event(receipt)
        for activity in (
            _activity(canonical, pretty, _ordinary_unlinked()),
            _activity(pretty, canonical, _ordinary_unlinked()),
        ):
            with self.subTest(order=[item["id"] for item in activity["activity"]]):
                self._refuse_lookup(activity, receipt["receipt_id"], "malformed")
                with self.assertRaises(CaptureUnlinkReceiptError) as minted:
                    record_committed_unlink_receipt(
                        activity, receipt, "2026-09-09T00:00:02Z"
                    )
                self.assertEqual(minted.exception.code, "malformed")
                with self.assertRaises(CaptureUnlinkReceiptError) as hint:
                    receipt_id_for_idempotency_key(activity, KEY)
                self.assertEqual(hint.exception.code, "malformed")

    def test_canonical_plus_invalid_closed_body_same_id_refuses(self) -> None:
        receipt = validate_receipt(_receipt())
        invalid = _invalid_closed_body_event(receipt, id="E-000002")
        for activity in (
            _activity(_event(receipt), invalid),
            _activity(invalid, _event(receipt)),
        ):
            with self.subTest(order=[item["id"] for item in activity["activity"]]):
                self._refuse_lookup(activity, receipt["receipt_id"], "malformed")
                with self.assertRaises(CaptureUnlinkReceiptError) as minted:
                    record_committed_unlink_receipt(
                        activity, receipt, "2026-09-09T00:00:02Z"
                    )
                self.assertEqual(minted.exception.code, "malformed")

    def test_canonical_conflicting_and_matching_mint_is_order_independent(self) -> None:
        first = validate_receipt(_receipt())
        conflicted = validate_receipt(_receipt(status_before="dismissed"))
        matching = (
            _activity(_event(first), _event(first, id="E-000002")),
            _activity(_event(first, id="E-000002"), _event(first)),
        )
        for activity in matching:
            with self.subTest(kind="matching", order=[item["id"] for item in activity["activity"]]):
                recorded = record_committed_unlink_receipt(
                    activity, first, "2026-09-09T00:00:02Z"
                )
                self.assertEqual(serialize_receipt(recorded), serialize_receipt(first))
                self.assertEqual(len(activity["activity"]), 2)
                found = self._lookup(activity, first["receipt_id"])
                self.assertEqual(serialize_receipt(found), serialize_receipt(first))
                self.assertEqual(receipt_id_for_idempotency_key(activity, KEY), first["receipt_id"])

        conflicting = (
            _activity(_event(conflicted), _event(first, id="E-000002")),
            _activity(_event(first), _event(conflicted, id="E-000002")),
        )
        for activity in conflicting:
            with self.subTest(kind="conflicting", order=[item["id"] for item in activity["activity"]]):
                with self.assertRaises(CaptureUnlinkReceiptError) as minted:
                    record_committed_unlink_receipt(
                        activity, first, "2026-09-09T00:00:02Z"
                    )
                self.assertEqual(minted.exception.code, "idempotency_conflict")
                self._refuse_lookup(activity, first["receipt_id"], "idempotency_conflict")

    def test_valid_canonical_only_ordinary_activity_and_unrelated_taint_do_not_block(
        self,
    ) -> None:
        receipt = validate_receipt(_receipt())
        other = validate_receipt(_receipt(idempotency_key="unlink.receipt.other"))
        activity = _activity(
            _ordinary_unlinked(),
            _event(receipt),
            _pretty_event(other, id="E-000003"),
            _unidentifiable_event(id="E-000004"),
        )
        found = self._lookup(activity, receipt["receipt_id"])
        self.assertEqual(serialize_receipt(found), serialize_receipt(receipt))
        self.assertEqual(receipt_id_for_idempotency_key(activity, KEY), receipt["receipt_id"])
        recorded = record_committed_unlink_receipt(
            activity, receipt, "2026-09-09T00:00:02Z"
        )
        self.assertEqual(serialize_receipt(recorded), serialize_receipt(receipt))
        self.assertEqual(len(activity["activity"]), 4)

    def test_malformed_alone_and_unidentifiable_blob_stay_closed_no_authority(self) -> None:
        receipt = validate_receipt(_receipt())
        self._refuse_lookup(_activity(_pretty_event(receipt)), receipt["receipt_id"], "not_found")
        self._refuse_lookup(
            _activity(_invalid_closed_body_event(receipt)),
            receipt["receipt_id"],
            "not_found",
        )
        self._refuse_lookup(
            _activity(_unidentifiable_event()),
            receipt["receipt_id"],
            "not_found",
        )
        self.assertIsNone(receipt_id_for_idempotency_key(_activity(_pretty_event(receipt)), KEY))
        self.assertIsNone(
            receipt_id_for_idempotency_key(_activity(_unidentifiable_event()), KEY)
        )

    def test_envelope_mismatch_with_canonical_sibling_cannot_authorize(self) -> None:
        receipt = validate_receipt(_receipt())
        mismatched = _event(receipt, capture_id="C-0002", id="E-000002")
        self._refuse_lookup(
            _activity(_event(receipt), mismatched, _ordinary_unlinked()),
            receipt["receipt_id"],
            "malformed",
        )
        self._refuse_lookup(
            _activity(mismatched, _event(receipt)),
            receipt["receipt_id"],
            "malformed",
        )


def _extra_details_event(receipt: dict[str, object], **overrides: object) -> dict[str, object]:
    """A byte-canonical receipt hidden inside a widened details envelope."""

    details = {
        "receipt": serialize_receipt(receipt).decode("utf-8"),
        "extra": "x",
    }
    return _event(receipt, details=details, **overrides)


def _respelled_id_event(
    receipt: dict[str, object], spelling: str, **overrides: object
) -> dict[str, object]:
    """The same receipt whose body names its identity in a noncanonical spelling."""

    body = dict(receipt)
    body["receipt_id"] = spelling
    blob = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return _event(receipt, details={"receipt": blob}, **overrides)


def _unrecoverable_id_event(
    receipt: dict[str, object], value: object, **overrides: object
) -> dict[str, object]:
    body = dict(receipt)
    body["receipt_id"] = value
    blob = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return _event(receipt, details={"receipt": blob}, **overrides)


def _spellings(receipt_id: str) -> dict[str, str]:
    parsed = uuid.UUID(receipt_id)
    return {
        "uppercase": receipt_id.upper(),
        "hex": parsed.hex,
        "braced": "{{{}}}".format(receipt_id),
        "urn": parsed.urn,
    }


class CaptureUnlinkReceiptSettledIdentityTest(unittest.TestCase):
    """Root-settled bounded identity matrix (R33-IDENTITY-SETTLEMENT-v2)."""

    def setUp(self) -> None:
        self.receipt = validate_receipt(_receipt())
        self.receipt_id = str(self.receipt["receipt_id"])

    def _lookup(self, activity: dict[str, object], receipt_id: object) -> dict[str, object]:
        return find_unlink_receipt(
            activity,
            receipt_id,
            workspace_uid=WORKSPACE,
            capture_id="C-0001",
        )

    def _refuse_all_entry_points(self, activity: dict[str, object], code: str) -> None:
        """Lookup, mint and the V4 replay hint refuse without touching activity."""

        before = json.dumps(activity, sort_keys=True)
        with self.assertRaises(CaptureUnlinkReceiptError) as found:
            self._lookup(activity, self.receipt_id)
        self.assertEqual(found.exception.code, code)
        with self.assertRaises(CaptureUnlinkReceiptError) as minted:
            record_committed_unlink_receipt(activity, self.receipt, "2026-09-09T00:00:02Z")
        self.assertEqual(minted.exception.code, "malformed")
        if code == "malformed":
            with self.assertRaises(CaptureUnlinkReceiptError) as hint:
                receipt_id_for_idempotency_key(activity, KEY)
            self.assertEqual(hint.exception.code, "malformed")
        else:
            self.assertIsNone(receipt_id_for_idempotency_key(activity, KEY))
        self.assertEqual(json.dumps(activity, sort_keys=True), before)

    def _siblings(self) -> dict[str, dict[str, object]]:
        siblings = {
            "extra-details": _extra_details_event(self.receipt, id="E-000002"),
        }
        for name, spelling in _spellings(self.receipt_id).items():
            siblings[name] = _respelled_id_event(self.receipt, spelling, id="E-000002")
        return siblings

    def test_same_id_tainted_sibling_blocks_canonical_in_either_order(self) -> None:
        canonical = _event(self.receipt)
        for name, sibling in self._siblings().items():
            for order in (
                (canonical, sibling, _ordinary_unlinked()),
                (sibling, canonical, _ordinary_unlinked()),
            ):
                with self.subTest(sibling=name, order=[item["id"] for item in order]):
                    self._refuse_all_entry_points(_activity(*order), "malformed")

    def test_same_id_tainted_sibling_alone_supplies_no_authority(self) -> None:
        for name, sibling in self._siblings().items():
            with self.subTest(sibling=name):
                self._refuse_all_entry_points(
                    _activity(sibling, _ordinary_unlinked()), "not_found"
                )

    def test_unrecoverable_identities_stay_unidentifiable_and_do_not_block(self) -> None:
        nonrfc = "11111111-1111-4111-c111-111111111111"
        cases = {
            "nil": str(uuid.UUID(int=0)),
            "non-rfc4122": nonrfc,
            "non-string": 17,
            "oversized": "u" * 49,
            "not-a-uuid": "not-a-uuid",
        }
        for name, value in cases.items():
            with self.subTest(identity=name):
                activity = _activity(
                    _unrecoverable_id_event(self.receipt, value, id="E-000002"),
                    _event(self.receipt),
                )
                found = self._lookup(activity, self.receipt_id)
                self.assertEqual(serialize_receipt(found), serialize_receipt(self.receipt))
                self.assertEqual(
                    receipt_id_for_idempotency_key(activity, KEY), self.receipt_id
                )
                recorded = record_committed_unlink_receipt(
                    activity, self.receipt, "2026-09-09T00:00:02Z"
                )
                self.assertEqual(serialize_receipt(recorded), serialize_receipt(self.receipt))
                self.assertEqual(len(activity["activity"]), 2)

    def test_different_identity_taint_and_duplicates_leave_the_wanted_id_valid(self) -> None:
        other = validate_receipt(_receipt(idempotency_key="unlink.receipt.other"))
        activity = _activity(
            _ordinary_unlinked(),
            _event(self.receipt),
            _event(self.receipt, id="E-000002"),
            _extra_details_event(other, id="E-000003"),
            _respelled_id_event(
                other, str(other["receipt_id"]).upper(), id="E-000004"
            ),
            _unidentifiable_event(id="E-000005"),
        )
        found = self._lookup(activity, self.receipt_id)
        self.assertEqual(serialize_receipt(found), serialize_receipt(self.receipt))
        self.assertEqual(receipt_id_for_idempotency_key(activity, KEY), self.receipt_id)
        recorded = record_committed_unlink_receipt(
            activity, self.receipt, "2026-09-09T00:00:02Z"
        )
        self.assertEqual(serialize_receipt(recorded), serialize_receipt(self.receipt))
        self.assertEqual(len(activity["activity"]), 6)

    def test_recovered_spellings_are_never_admitted_as_receipt_identity(self) -> None:
        """Taint recovery must not relax strict admission or canonical validity."""

        refused = dict(_spellings(self.receipt_id))
        refused["nil"] = str(uuid.UUID(int=0))
        refused["non-rfc4122"] = "11111111-1111-4111-c111-111111111111"
        for name, spelling in refused.items():
            with self.subTest(spelling=name):
                with self.assertRaises(CaptureUnlinkReceiptError) as admitted:
                    admit_receipt_id(spelling)
                self.assertEqual(admitted.exception.code, "malformed")
                with self.assertRaises(CaptureUnlinkReceiptError) as validated:
                    validate_receipt({**_receipt(), "receipt_id": spelling})
                self.assertEqual(validated.exception.code, "malformed")
                with self.assertRaises(CaptureUnlinkReceiptError) as requested:
                    self._lookup(_activity(_event(self.receipt)), spelling)
                self.assertEqual(requested.exception.code, "malformed")

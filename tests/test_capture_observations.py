"""Focused tests for the pure historical capture-observation model.

Nothing here opens a Store, reads a wall clock, or names a live corpus.
Every instant is supplied by the test. Protocol admission is the existing
verification validators, called with the stored accepted_at rather than now.
"""

from __future__ import annotations

import ast
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workstack.capture_observations import (  # noqa: E402
    MAX_OBSERVATION_BYTES,
    MAX_OBSERVATIONS,
    MAX_OBSERVATIONS_BYTES,
    OBSERVATION_SCHEMA,
    CaptureObservationError,
    plan_capture_observations_upgrade,
    plan_observation_upsert,
    validate_observation,
    validate_observations,
)
from workstack.knowledge_verification_protocol import (  # noqa: E402
    REQUEST_SCHEMA,
    RESULT_SCHEMA,
)

WORKSPACE = "8f14e45f-ce8a-4b0e-9c2a-1d1f0a3b5c77"
OTHER_WORKSPACE = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
UPSTREAM = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
ALIAS = "od-primary"
DIGEST = "sha256:" + ("ab" * 32)
NAS_VERSION = "sha256-" + ("a" * 64)
REQUESTED_AT = "2026-09-09T12:00:00Z"
EXPIRES_AT = "2026-09-09T12:01:00Z"
ACCEPTED_AT = "2026-09-09T12:00:10Z"
CHECKED_AT = "2026-09-09T12:00:05Z"


def _nonce(index: int) -> str:
    return "00000000-0000-4000-8000-{:012d}".format(index)


def _entry(document_ref: str, source_type: str, expected: str | None) -> dict[str, object]:
    return {
        "document_ref": document_ref,
        "source_type": source_type,
        "expected_source_version": expected,
    }


def _request(
    *,
    verification_id: str,
    capture_id: str,
    workspace_uid: str = WORKSPACE,
    evidence: list[dict[str, object]] | None = None,
    requested_at: str = REQUESTED_AT,
    expires_at: str = EXPIRES_AT,
) -> dict[str, object]:
    return {
        "schema": REQUEST_SCHEMA,
        "verification_id": verification_id,
        "binding": {
            "workspace_uid": workspace_uid,
            "capture_id": capture_id,
            "capture_revision": 3,
        },
        "connection": {
            "alias": ALIAS,
            "upstream_workspace_uid": UPSTREAM,
            "policy_revision": 7,
        },
        "corpus_refs": ["engineering"],
        "evidence": evidence
        or [_entry("docalpha0001", "nas.file", NAS_VERSION)],
        "requested_at": requested_at,
        "expires_at": expires_at,
    }


def _result_entry(
    asked: dict[str, object], *, status: str, code: str, observed: str | None
) -> dict[str, object]:
    return {
        "document_ref": asked["document_ref"],
        "source_type": asked["source_type"],
        "expected_source_version": asked["expected_source_version"],
        "observed_source_version": observed,
        "status": status,
        "code": code,
    }


def _result(
    request: dict[str, object],
    *,
    status: str = "current",
    code: str = "hash_matched",
    observed: str | None = NAS_VERSION,
    checked_at: str = CHECKED_AT,
) -> dict[str, object]:
    evidence = [
        _result_entry(item, status=status, code=code, observed=observed)
        for item in request["evidence"]  # type: ignore[union-attr]
    ]
    return {
        "schema": RESULT_SCHEMA,
        "verification_id": request["verification_id"],
        "checked_at": checked_at,
        "evidence": evidence,
    }


def _observation(
    *,
    capture_id: str = "C-0042",
    verification_id: str = _nonce(1),
    accepted_at: str = ACCEPTED_AT,
    requested_at: str = REQUESTED_AT,
    expires_at: str = EXPIRES_AT,
    workspace_uid: str = WORKSPACE,
    status: str = "current",
    code: str = "hash_matched",
    source_type: str = "nas.file",
    expected: str | None = NAS_VERSION,
    observed: str | None = NAS_VERSION,
    digest: str = DIGEST,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    request = _request(
        verification_id=verification_id,
        capture_id=capture_id,
        workspace_uid=workspace_uid,
        evidence=[_entry("docalpha0001", source_type, expected)],
        requested_at=requested_at,
        expires_at=expires_at,
    )
    record: dict[str, object] = {
        "schema": OBSERVATION_SCHEMA,
        "capture_digest": digest,
        "verifier_alias": ALIAS,
        "accepted_at": accepted_at,
        "request": request,
        "result": _result(
            request,
            status=status,
            code=code,
            observed=observed,
            checked_at=accepted_at,
        ),
    }
    if extra:
        record.update(extra)
    return record


def _compact(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


class _ModelCase(unittest.TestCase):
    def refuse(self, code: str, call: object) -> CaptureObservationError:
        with self.assertRaises(CaptureObservationError) as caught:
            call()  # type: ignore[operator]
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        return caught.exception


class ObservationAdmissionTests(_ModelCase):
    def test_a_nas_current_observation_normalises_to_the_closed_record(self) -> None:
        admitted = validate_observation(_observation(), workspace_uid=WORKSPACE)
        self.assertEqual(set(admitted), set(_observation()))
        self.assertEqual(admitted["schema"], OBSERVATION_SCHEMA)
        self.assertEqual(admitted["verifier_alias"], ALIAS)
        self.assertEqual(admitted["request"]["connection"]["alias"], ALIAS)
        self.assertEqual(admitted["result"]["evidence"][0]["status"], "current")
        self.assertEqual(admitted["result"]["evidence"][0]["code"], "hash_matched")

    def test_a_notion_unverifiable_observation_is_admitted(self) -> None:
        record = _observation(
            status="unverifiable",
            code="no_origin_verifier",
            source_type="notion.page",
            expected="notionver0001",
            observed=None,
        )
        admitted = validate_observation(record, workspace_uid=WORKSPACE)
        self.assertEqual(admitted["result"]["evidence"][0]["status"], "unverifiable")
        self.assertIsNone(admitted["result"]["evidence"][0]["observed_source_version"])

    def test_historical_acceptance_does_not_consult_present_time(self) -> None:
        admitted = validate_observation(_observation(), workspace_uid=WORKSPACE)
        self.assertEqual(admitted["accepted_at"], ACCEPTED_AT)
        source = (ROOT / "workstack" / "capture_observations.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
        self.assertNotIn("now", names)
        self.assertNotIn("utcnow", names)
        self.assertNotIn("time", names)

    def test_expired_at_acceptance_is_refused(self) -> None:
        self.refuse(
            "verification_expired",
            lambda: validate_observation(
                _observation(accepted_at=EXPIRES_AT), workspace_uid=WORKSPACE
            ),
        )

    def test_a_nonce_mismatch_is_refused(self) -> None:
        record = _observation()
        record["result"]["verification_id"] = _nonce(9)  # type: ignore[index]
        self.refuse(
            "verification_id_mismatch",
            lambda: validate_observation(record, workspace_uid=WORKSPACE),
        )

    def test_reordered_result_evidence_is_refused(self) -> None:
        request = _request(
            verification_id=_nonce(2),
            capture_id="C-0042",
            evidence=[
                _entry("docalpha0001", "nas.file", NAS_VERSION),
                _entry("docbeta.0002", "nas.file", NAS_VERSION),
            ],
        )
        result = _result(request)
        result["evidence"] = list(reversed(result["evidence"]))  # type: ignore[arg-type]
        record = {
            "schema": OBSERVATION_SCHEMA,
            "capture_digest": DIGEST,
            "verifier_alias": ALIAS,
            "accepted_at": ACCEPTED_AT,
            "request": request,
            "result": result,
        }
        self.refuse(
            "verification_evidence_mismatch",
            lambda: validate_observation(record, workspace_uid=WORKSPACE),
        )

    def test_unknown_and_secret_shaped_fields_refuse_without_content(self) -> None:
        secret = "sk-live-canary-do-not-echo"
        error = self.refuse(
            "unknown_field",
            lambda: validate_observation(
                _observation(extra={"token": secret, "command": "/bin/verifier"}),
                workspace_uid=WORKSPACE,
            ),
        )
        self.assertNotIn(secret, str(error))
        self.assertNotIn("verifier", str(error))

    def test_a_cross_workspace_record_is_refused(self) -> None:
        self.refuse(
            "workspace_mismatch",
            lambda: validate_observation(
                _observation(workspace_uid=OTHER_WORKSPACE),
                workspace_uid=WORKSPACE,
            ),
        )

    def test_a_mismatched_verifier_alias_is_refused(self) -> None:
        record = _observation()
        record["verifier_alias"] = "other-alias"
        self.refuse(
            "verifier_alias_mismatch",
            lambda: validate_observation(record, workspace_uid=WORKSPACE),
        )

    def test_an_individually_oversized_record_refuses_without_evicting(self) -> None:
        existing = [_observation(capture_id="C-0001", verification_id=_nonce(1))]
        fat_refs = [
            _entry("n" * 256, "nas.file", "v" * 256) for _ in range(10)
        ]
        incoming = _observation(capture_id="C-0002", verification_id=_nonce(2))
        incoming["request"]["evidence"] = fat_refs  # type: ignore[index]
        incoming["request"]["corpus_refs"] = ["c{}".format(i) for i in range(8)]  # type: ignore[index]
        incoming["result"]["evidence"] = [  # type: ignore[index]
            _result_entry(item, status="current", code="hash_matched", observed="v" * 256)
            for item in fat_refs
        ]
        before = copy.deepcopy(existing)
        small = validate_observation(existing[0], workspace_uid=WORKSPACE)
        with patch(
            "workstack.capture_observations.MAX_OBSERVATION_BYTES",
            max(len(_compact(small)) + 64, 2048),
        ):
            self.refuse(
                "observation_too_large",
                lambda: plan_observation_upsert(
                    existing, incoming, workspace_uid=WORKSPACE
                ),
            )
        self.assertEqual(existing, before)


class ObservationListTests(_ModelCase):
    def test_the_list_is_canonical_sorted_and_rejects_duplicates(self) -> None:
        later = _observation(capture_id="C-0009", verification_id=_nonce(9))
        earlier = _observation(capture_id="C-0002", verification_id=_nonce(2))
        ordered = validate_observations([later, earlier], workspace_uid=WORKSPACE)
        self.assertEqual(
            [item["request"]["binding"]["capture_id"] for item in ordered],
            ["C-0002", "C-0009"],
        )
        self.refuse(
            "duplicate_capture_id",
            lambda: validate_observations(
                [
                    _observation(capture_id="C-0002", verification_id=_nonce(2)),
                    _observation(capture_id="C-0002", verification_id=_nonce(3)),
                ],
                workspace_uid=WORKSPACE,
            ),
        )


class UpsertTests(_ModelCase):
    def test_newer_accepted_at_replaces_the_same_capture(self) -> None:
        first = _observation(verification_id=_nonce(1), accepted_at=ACCEPTED_AT)
        newer = _observation(
            verification_id=_nonce(2),
            accepted_at="2026-09-09T12:00:20Z",
            digest="sha256:" + ("cd" * 32),
        )
        planned = plan_observation_upsert([first], newer, workspace_uid=WORKSPACE)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["request"]["verification_id"], _nonce(2))

    def test_an_older_accepted_at_is_refused_as_stale(self) -> None:
        current = _observation(
            verification_id=_nonce(2), accepted_at="2026-09-09T12:00:20Z"
        )
        older = _observation(verification_id=_nonce(1), accepted_at=ACCEPTED_AT)
        self.refuse(
            "stale_observation",
            lambda: plan_observation_upsert([current], older, workspace_uid=WORKSPACE),
        )

    def test_identical_verification_id_replay_is_idempotent(self) -> None:
        record = _observation()
        first = plan_observation_upsert([], record, workspace_uid=WORKSPACE)
        second = plan_observation_upsert(first, record, workspace_uid=WORKSPACE)
        self.assertEqual(first, second)

    def test_the_same_verification_id_with_different_content_is_a_collision(self) -> None:
        first = _observation(capture_id="C-0001", verification_id=_nonce(1))
        conflict = _observation(capture_id="C-0002", verification_id=_nonce(1))
        self.refuse(
            "observation_conflict",
            lambda: plan_observation_upsert([first], conflict, workspace_uid=WORKSPACE),
        )
        mutated = _observation(capture_id="C-0001", verification_id=_nonce(1))
        mutated["capture_digest"] = "sha256:" + ("ef" * 32)
        self.refuse(
            "observation_conflict",
            lambda: plan_observation_upsert([first], mutated, workspace_uid=WORKSPACE),
        )

    def test_equal_accepted_at_with_a_new_verification_id_replaces(self) -> None:
        first = _observation(verification_id=_nonce(1), accepted_at=ACCEPTED_AT)
        second = _observation(verification_id=_nonce(2), accepted_at=ACCEPTED_AT)
        planned = plan_observation_upsert([first], second, workspace_uid=WORKSPACE)
        self.assertEqual(planned[0]["request"]["verification_id"], _nonce(2))

    def test_count_eviction_keeps_the_new_record(self) -> None:
        rows = [
            _observation(
                capture_id="C-{:04d}".format(2000 + index),
                verification_id=_nonce(1000 + index),
                accepted_at="2026-09-09T12:00:{:02d}Z".format(index % 50),
            )
            for index in range(MAX_OBSERVATIONS)
        ]
        incoming = _observation(
            capture_id="C-2999",
            verification_id=_nonce(9),
            accepted_at="2026-09-09T12:00:40Z",
        )
        planned = plan_observation_upsert(rows, incoming, workspace_uid=WORKSPACE)
        self.assertEqual(len(planned), MAX_OBSERVATIONS)
        ids = [item["request"]["binding"]["capture_id"] for item in planned]
        self.assertIn("C-2999", ids)
        self.assertEqual(ids, sorted(ids))
        self.assertNotIn("C-2000", ids)

    def test_timezone_offsets_evict_by_instant_not_lexically(self) -> None:
        early_offset = _observation(
            capture_id="C-9000",
            verification_id=_nonce(50),
            requested_at="2026-09-09T09:59:30Z",
            expires_at="2026-09-09T10:00:30Z",
            accepted_at="2026-09-09T15:00:00+05:00",
        )
        later_zulu = _observation(
            capture_id="C-1000",
            verification_id=_nonce(51),
            requested_at="2026-09-09T11:59:30Z",
            expires_at="2026-09-09T12:00:30Z",
            accepted_at="2026-09-09T12:00:00Z",
        )
        fillers = [
            _observation(
                capture_id="C-{:04d}".format(4000 + index),
                verification_id=_nonce(2000 + index),
                requested_at="2026-09-09T12:59:30Z",
                expires_at="2026-09-09T13:00:30Z",
                accepted_at="2026-09-09T13:00:00Z",
            )
            for index in range(MAX_OBSERVATIONS - 2)
        ]
        incoming = _observation(
            capture_id="C-5000",
            verification_id=_nonce(52),
            requested_at="2026-09-09T13:59:30Z",
            expires_at="2026-09-09T14:00:30Z",
            accepted_at="2026-09-09T14:00:00Z",
        )
        planned = plan_observation_upsert(
            [early_offset, later_zulu, *fillers], incoming, workspace_uid=WORKSPACE
        )
        ids = [item["request"]["binding"]["capture_id"] for item in planned]
        self.assertIn("C-5000", ids)
        self.assertIn("C-1000", ids)
        self.assertNotIn("C-9000", ids)

    def test_compact_byte_eviction_uses_real_utf8_json(self) -> None:
        first = validate_observation(
            _observation(capture_id="C-0001", verification_id=_nonce(1)),
            workspace_uid=WORKSPACE,
        )
        second = _observation(capture_id="C-0002", verification_id=_nonce(2))
        budget = len(_compact([first])) + 8
        self.assertLess(budget, MAX_OBSERVATIONS_BYTES)
        with patch("workstack.capture_observations.MAX_OBSERVATIONS_BYTES", budget):
            planned = plan_observation_upsert([first], second, workspace_uid=WORKSPACE)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["request"]["binding"]["capture_id"], "C-0002")
        self.assertLessEqual(len(_compact(planned)), budget)

    def test_tie_break_uses_verification_id_then_capture_id(self) -> None:
        stamp = "2026-09-09T12:00:00Z"
        low = _observation(
            capture_id="C-0003", verification_id=_nonce(1), accepted_at=stamp
        )
        high = _observation(
            capture_id="C-0002", verification_id=_nonce(2), accepted_at=stamp
        )
        incoming = _observation(
            capture_id="C-0004",
            verification_id=_nonce(3),
            accepted_at="2026-09-09T12:00:20Z",
        )
        with patch("workstack.capture_observations.MAX_OBSERVATIONS", 2):
            planned = plan_observation_upsert(
                [low, high], incoming, workspace_uid=WORKSPACE
            )
        ids = [item["request"]["binding"]["capture_id"] for item in planned]
        self.assertEqual(ids, ["C-0002", "C-0004"])

    def test_upsert_does_not_mutate_its_inputs(self) -> None:
        existing = [_observation(capture_id="C-0001", verification_id=_nonce(1))]
        incoming = _observation(capture_id="C-0002", verification_id=_nonce(2))
        snapshot_existing = copy.deepcopy(existing)
        snapshot_incoming = copy.deepcopy(incoming)
        plan_observation_upsert(existing, incoming, workspace_uid=WORKSPACE)
        self.assertEqual(existing, snapshot_existing)
        self.assertEqual(incoming, snapshot_incoming)


class ContainerUpgradeTests(_ModelCase):
    def test_version1_upgrade_preserves_captures_as_a_json_value(self) -> None:
        captures = [{"id": "C-0001", "schema_version": "1.1", "note": "keep"}]
        document = {"version": 1, "captures": captures}
        snapshot = copy.deepcopy(document)
        planned = plan_capture_observations_upgrade(document)
        self.assertEqual(document, snapshot)
        self.assertIsNot(planned["captures"], document["captures"])
        self.assertEqual(_compact(planned["captures"]), _compact(captures))
        self.assertEqual(planned["version"], 2)
        self.assertEqual(planned["observations"], [])
        self.assertEqual(set(planned), {"version", "captures", "observations"})
        planned["captures"].append({"id": "C-9999"})
        self.assertEqual(document["captures"], snapshot["captures"])

    def test_non_list_captures_are_refused(self) -> None:
        for captures in (None, 42, "captures", {"id": "C-0001"}, ("C-0001",), True):
            with self.subTest(captures=captures):
                document = {"version": 1, "captures": captures}
                snapshot = copy.deepcopy(document)
                self.refuse(
                    "invalid_container",
                    lambda document=document: plan_capture_observations_upgrade(
                        document
                    ),
                )
                self.assertEqual(document, snapshot)

    def test_version2_and_malformed_containers_are_refused(self) -> None:
        self.refuse(
            "unsupported_container_version",
            lambda: plan_capture_observations_upgrade(
                {"version": 2, "captures": [], "observations": []}
            ),
        )
        self.refuse(
            "unknown_field",
            lambda: plan_capture_observations_upgrade(
                {"version": 1, "captures": [], "observations": []}
            ),
        )
        self.refuse(
            "unsupported_container_version",
            lambda: plan_capture_observations_upgrade({"version": 3, "captures": []}),
        )
        self.refuse(
            "invalid_container",
            lambda: plan_capture_observations_upgrade({"version": True, "captures": []}),
        )


class IsolationTests(unittest.TestCase):
    def test_the_model_imports_no_store_runtime_or_provider(self) -> None:
        source = (ROOT / "workstack" / "capture_observations.py").read_text(
            encoding="utf-8"
        )
        forbidden = {
            "workstack.store",
            "workstack.knowledge_verification_runtime",
            "workstack.knowledge_verification_http",
            "integrations.opendocuments",
        }
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module, forbidden)
                self.assertFalse(
                    node.module.startswith("integrations."),
                    node.module,
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name, forbidden)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

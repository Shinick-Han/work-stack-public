from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from unittest import mock

from workstack.capture import fingerprint_for
from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.capture_reply_repository import V4CaptureReplyRepository
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority


ROOT = Path(__file__).resolve().parents[1]
PACKET_FIXTURE = ROOT / "contracts" / "capture-packet-v1.fixture.json"
STALE_PACKET_FIXTURE = ROOT / "contracts" / "capture-packet-v1.stale.json"
CONTRACT_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "storage-contracts"
    / "capture-reply-v3-contract.json"
)


class SimulatedCommitInterruption(RuntimeError):
    """Test-only crash boundary shared by future backend harnesses."""


@dataclass(frozen=True)
class CaptureReplyState:
    captures: tuple[dict[str, Any], ...]
    replies: tuple[dict[str, Any], ...]
    activity: tuple[dict[str, Any], ...]
    idempotency: tuple[dict[str, Any], ...]


class CaptureReplyContractBackend(Protocol):
    """Semantic harness seam; a future v4 WriteSession driver can replace V3."""

    def ingest(self, packet: dict[str, Any], key: str) -> dict[str, Any]: ...

    def create_task(self, title: str) -> dict[str, Any]: ...

    def link(self, capture_id: str, task_id: str, key: str) -> dict[str, Any]: ...

    def approve(
        self, task_id: str, capture_id: str, body: str, key: str
    ) -> dict[str, Any]: ...

    def apply_receipt(
        self, reply: dict[str, Any], key: str
    ) -> dict[str, Any]: ...

    def state(self) -> CaptureReplyState: ...

    def interrupt_next_commit(self) -> None: ...

    def restart(self) -> None: ...


class InterruptingV3Store(Store):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self._interrupt = False

    def interrupt_next_commit(self) -> None:
        self._interrupt = True

    def _atomic_write_locked(self, path: Path, value: Any) -> None:
        super()._atomic_write_locked(path, value)
        if self._interrupt and path.name in DEFAULTS:
            self._interrupt = False
            raise SimulatedCommitInterruption("SIMULATED_COMMIT_INTERRUPTION")


class V3CaptureReplyBackend:
    """Test adapter for current behavior; it intentionally names v3 files here only."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = InterruptingV3Store(root)
        self.stack = WorkStack(self.store)

    def ingest(self, packet: dict[str, Any], key: str) -> dict[str, Any]:
        return self.stack.ingest_capture(packet, key)

    def create_task(self, title: str) -> dict[str, Any]:
        return self.stack.add_task(title)

    def link(self, capture_id: str, task_id: str, key: str) -> dict[str, Any]:
        return self.stack.link_capture(capture_id, task_id, key)

    def approve(self, task_id: str, capture_id: str, body: str, key: str) -> dict[str, Any]:
        return self.stack.approve_reply(
            {"task_id": task_id, "capture_id": capture_id, "body": body, "approved": True},
            key,
        )

    def apply_receipt(self, reply: dict[str, Any], key: str) -> dict[str, Any]:
        receipt = {
            "schema_version": "1.0", "reply_id": reply["id"],
            "provider": reply["provider"], "outcome": "sent",
            "occurred_at": "2026-08-29T00:15:00Z",
            "body_digest": reply["body_digest"], "target_digest": reply["target_digest"],
            "remote_message_ref": "message:contract-reply-001",
            "web_url": "https://outlook.office.com/mail/deeplink/read/contract-reply-001",
        }
        return self.stack.apply_reply_receipt(reply["id"], receipt, key)

    def state(self) -> CaptureReplyState:
        activity = self.store.load("activity.json")
        return CaptureReplyState(
            captures=tuple(self.store.load("captures.json")["captures"]),
            replies=tuple(self.store.load("replies.json")["replies"]),
            activity=tuple(activity["activity"]),
            idempotency=tuple(activity["idempotency"]),
        )

    def interrupt_next_commit(self) -> None:
        self.store.interrupt_next_commit()

    def restart(self) -> None:
        self.store = InterruptingV3Store(self.root)
        self.stack = WorkStack(self.store)


def _write_v4_conversion(root: Path, conversion: Any) -> None:
    def write(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    write(root / "store.json", canonical_json_bytes(dict(conversion.store)))
    write(root / "workspace.json", canonical_json_bytes(dict(conversion.workspace)))
    for kind, records in conversion.records.items():
        for record in records:
            uid = str(record["uid"])
            write(
                root / "records" / kind / uid[:2] / f"{uid}.json",
                canonical_json_bytes(dict(record)),
            )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for kind, events in conversion.streams.items():
        for event in events:
            grouped.setdefault((kind, str(event["created_at"])[:7]), []).append(
                dict(event)
            )
    for (kind, segment), events in grouped.items():
        write(
            root / "streams" / kind / f"{segment}.ndjson",
            b"".join(
                canonical_json_bytes(event) + b"\n"
                for event in sorted(events, key=lambda value: value["sequence"])
            ),
        )


class V4CaptureReplyBackend:
    """The same contract pointed at the explicit opt-in normalized writer."""

    def __init__(self, root: Path) -> None:
        self.root = root
        source = root.parent / "bootstrap-v3"
        bootstrap = WorkStack(Store(source))
        self.task = bootstrap.add_task("Capture/reply contract Task")
        documents = {name: bootstrap.store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(
            documents, candidate_created_at="2026-08-29T00:00:00Z"
        )
        _write_v4_conversion(root, self.conversion)
        runtime_base = Path(os.environ["WORK_STACK_RUNTIME"])
        self.runtime = resolve_runtime_authority(
            root, runtime_base, str(self.conversion.store["workspace_uid"])
        )
        self.runtime.runtime_root.mkdir(parents=True, exist_ok=True)
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )
        publish_runtime_manifest(
            self.runtime.manifest_path,
            build_v4_manifest(read_v4(root), generation=0),
            expected_digest=None,
        )
        self.clock_tick = 0
        self.repository = self._repository()

    def _clock(self) -> str:
        self.clock_tick += 1
        return f"2026-09-02T00:10:{self.clock_tick:02d}Z"

    def _repository(self) -> V4CaptureReplyRepository:
        return V4CaptureReplyRepository(
            self.root,
            self.runtime,
            task_note_source_indexes=self.conversion.task_note_source_indexes,
            clock=self._clock,
            enable_v4_capture_reply_commands=True,
        )

    def ingest(self, packet: dict[str, Any], key: str) -> dict[str, Any]:
        return self.repository.ingest_capture(packet, key)

    def create_task(self, title: str) -> dict[str, Any]:
        task = copy.deepcopy(self.task)
        task["title"] = title
        return task

    def link(self, capture_id: str, task_id: str, key: str) -> dict[str, Any]:
        return self.repository.link_capture(capture_id, task_id, key)

    def approve(
        self, task_id: str, capture_id: str, body: str, key: str
    ) -> dict[str, Any]:
        return self.repository.approve_reply(
            {"task_id": task_id, "capture_id": capture_id, "body": body, "approved": True},
            key,
        )

    def apply_receipt(self, reply: dict[str, Any], key: str) -> dict[str, Any]:
        receipt = {
            "schema_version": "1.0", "reply_id": reply["id"],
            "provider": reply["provider"], "outcome": "sent",
            "occurred_at": "2026-08-29T00:15:00Z",
            "body_digest": reply["body_digest"], "target_digest": reply["target_digest"],
            "remote_message_ref": "message:contract-reply-001",
            "web_url": "https://outlook.office.com/mail/deeplink/read/contract-reply-001",
        }
        return self.repository.apply_reply_receipt(reply["id"], receipt, key)

    def state(self) -> CaptureReplyState:
        documents = self.repository.state_documents()
        return CaptureReplyState(
            captures=tuple(documents["captures.json"]["captures"]),
            replies=tuple(documents["replies.json"]["replies"]),
            activity=tuple(documents["activity.json"]["activity"]),
            idempotency=tuple(documents["activity.json"]["idempotency"]),
        )

    def interrupt_next_commit(self) -> None:
        fired = False

        def interrupt(transition: str) -> None:
            nonlocal fired
            if not fired and transition.startswith("target_replaced:authority:"):
                fired = True
                raise SimulatedCommitInterruption("SIMULATED_COMMIT_INTERRUPTION")

        self.repository.fault_hook = interrupt

    def restart(self) -> None:
        self.repository = self._repository()


class CaptureReplyContractCases:
    backend: CaptureReplyContractBackend

    def make_backend(self, root: Path) -> CaptureReplyContractBackend:
        raise NotImplementedError

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "authority"
        self.runtime_environment = mock.patch.dict(
            os.environ,
            {"WORK_STACK_RUNTIME": str(Path(self.temporary.name) / "runtime")},
        )
        self.runtime_environment.start()
        self.backend = self.make_backend(self.root)
        self.contract = json.loads(CONTRACT_FIXTURE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.runtime_environment.stop()
        self.temporary.cleanup()
        super().tearDown()  # type: ignore[misc]

    @staticmethod
    def packet(path: Path = PACKET_FIXTURE) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def linked_capture(self) -> tuple[dict[str, Any], dict[str, Any]]:
        created = self.backend.ingest(
            self.packet(), "contract.ingest.linked.0001"
        )["body"]["data"]
        task = self.backend.create_task("Capture/reply contract Task")
        linked = self.backend.link(
            created["id"], task["id"], "contract.link.0001"
        )["body"]["data"]
        return task, linked

    def assert_error_code(self, expected: str, operation: Callable[[], object]) -> None:
        with self.assertRaises(Exception) as caught:  # type: ignore[attr-defined]
            operation()
        self.assertEqual(getattr(caught.exception, "code", None), expected)  # type: ignore[attr-defined]

    def test_lifecycle_freezes_shapes_activity_and_multi_record_effects(self) -> None:
        ingest = self.backend.ingest(self.packet(), "contract.ingest.0001")
        capture = ingest["body"]["data"]
        self.assertEqual(set(ingest), set(self.contract["response_envelope_fields"]))
        self.assertEqual(set(capture), set(self.contract["capture_fields"]))
        self.assertEqual(ingest["status"], 201)
        self.assertEqual(capture["status"], self.contract["states"]["capture_created"])
        self.assertEqual(capture["revision"], self.contract["revision_contract"]["created"])

        task = self.backend.create_task("Capture/reply contract Task")
        link = self.backend.link(capture["id"], task["id"], "contract.link.0001")
        linked = link["body"]["data"]
        self.assertEqual(link["status"], 200)
        self.assertEqual(linked["status"], self.contract["states"]["capture_linked"])
        self.assertEqual(linked["revision"], self.contract["revision_contract"]["linked"])

        approval = self.backend.approve(
            task["id"], linked["id"], "Approved contract reply", "contract.approve.0001"
        )
        reply = approval["body"]["data"]
        self.assertEqual(set(reply), set(self.contract["reply_fields"]))
        self.assertEqual(approval["status"], 201)
        self.assertEqual(reply["state"], self.contract["states"]["reply_approved"])
        self.assertEqual(reply["capture_revision"], linked["revision"])

        terminal = self.backend.apply_receipt(reply, "contract.receipt.0001")
        self.assertEqual(terminal["status"], 200)
        self.assertEqual(
            terminal["body"]["data"]["state"],
            self.contract["states"]["reply_terminal"],
        )

        state = self.backend.state()
        domain_events = [
            event["type"]
            for event in state.activity
            if event["type"].startswith(("capture.", "reply."))
        ]
        self.assertEqual(domain_events, self.contract["activity_types"])
        records = {record["key"]: record for record in state.idempotency}
        self.assertIn("response_body", records["contract.ingest.0001"])
        self.assertIn("response_body", records["contract.link.0001"])
        self.assertEqual(
            records["contract.approve.0001"]["response_ref"],
            {"kind": "reply", "id": reply["id"]},
        )
        self.assertEqual(
            records["contract.receipt.0001"]["response_ref"],
            {"kind": "reply", "id": reply["id"]},
        )
        self.assertNotIn("Approved contract reply", json.dumps(records))

    def test_idempotency_replay_and_conflict_are_stable(self) -> None:
        packet = self.packet()
        created = self.backend.ingest(packet, "contract.replay.ingest.0001")
        replay = self.backend.ingest(packet, "contract.replay.ingest.0001")
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(replay["body"]["data"], created["body"]["data"])

        altered = copy.deepcopy(packet)
        altered["normalized"]["summary"] += " conflicting request"
        self.assert_error_code(
            "idempotency_conflict",
            lambda: self.backend.ingest(
                altered, "contract.replay.ingest.0001"
            ),
        )

        task = self.backend.create_task("Replay contract Task")
        linked = self.backend.link(
            created["body"]["data"]["id"], task["id"], "contract.replay.link.0001"
        )["body"]["data"]
        approved = self.backend.approve(
            task["id"], linked["id"], "Frozen reply", "contract.replay.approve.0001"
        )
        reply_replay = self.backend.approve(
            task["id"], linked["id"], "Frozen reply", "contract.replay.approve.0001"
        )
        self.assertEqual(reply_replay["status"], 200)
        self.assertTrue(reply_replay["body"]["meta"]["replayed"])
        self.assertEqual(
            reply_replay["body"]["data"]["id"], approved["body"]["data"]["id"]
        )
        self.assert_error_code(
            "idempotency_conflict",
            lambda: self.backend.approve(
                task["id"],
                linked["id"],
                "Different reply",
                "contract.replay.approve.0001",
            ),
        )
        self.assertEqual(len(self.backend.state().captures), 1)
        self.assertEqual(len(self.backend.state().replies), 1)

    def test_source_revision_conflicts_are_cas_like_and_non_mutating(self) -> None:
        packet = self.packet()
        first = self.backend.ingest(packet, "contract.revision.0001")["body"]["data"]
        action_id = first["normalized"]["action_items"][0]["id"]
        updated = copy.deepcopy(packet)
        updated["source"]["version_ref"] = "change-key:contract-v2"
        updated["source"]["retrieved_at"] = "2026-08-29T09:00:00Z"
        updated["source"]["fingerprint"] = fingerprint_for(updated["source"])
        second = self.backend.ingest(updated, "contract.revision.0002")["body"]["data"]
        self.assertEqual(second["revision"], self.contract["revision_contract"]["source_updated"])
        self.assertEqual(second["normalized"]["action_items"][0]["id"], action_id)

        self.assert_error_code(
            "stale_capture",
            lambda: self.backend.ingest(
                self.packet(STALE_PACKET_FIXTURE), "contract.revision.stale.0001"
            ),
        )
        equal_time = copy.deepcopy(updated)
        equal_time["source"]["version_ref"] = "change-key:contract-conflict"
        equal_time["source"]["fingerprint"] = fingerprint_for(equal_time["source"])
        self.assert_error_code(
            "source_revision_conflict",
            lambda: self.backend.ingest(
                equal_time, "contract.revision.conflict.0001"
            ),
        )
        current = self.backend.state().captures[0]
        self.assertEqual(current["revision"], second["revision"])
        failed_keys = {"contract.revision.stale.0001", "contract.revision.conflict.0001"}
        self.assertTrue(failed_keys.isdisjoint(
            record["key"] for record in self.backend.state().idempotency
        ))

    def test_restart_preserves_records_and_reference_replay(self) -> None:
        task, capture = self.linked_capture()
        approved = self.backend.approve(
            task["id"], capture["id"], "Restart reply", "contract.restart.approve.0001"
        )
        before = self.backend.state()

        self.backend.restart()

        self.assertEqual(self.backend.state(), before)
        replay = self.backend.approve(
            task["id"], capture["id"], "Restart reply", "contract.restart.approve.0001"
        )
        self.assertEqual(replay["status"], 200)
        self.assertTrue(replay["body"]["meta"]["replayed"])
        self.assertEqual(
            replay["body"]["data"]["id"], approved["body"]["data"]["id"]
        )

    def test_interrupted_capture_and_reply_commits_recover_all_effects(self) -> None:
        packet = self.packet()
        self.backend.interrupt_next_commit()
        with self.assertRaises(SimulatedCommitInterruption):  # type: ignore[attr-defined]
            self.backend.ingest(packet, "contract.crash.ingest.0001")
        self.backend.restart()
        recovered = self.backend.state()
        self.assertEqual(len(recovered.captures), 1)
        self.assertIn("capture.ingested", [event["type"] for event in recovered.activity])
        self.assertIn(
            "contract.crash.ingest.0001",
            [record["key"] for record in recovered.idempotency],
        )

        task = self.backend.create_task("Interrupted reply contract Task")
        linked = self.backend.link(
            recovered.captures[0]["id"], task["id"], "contract.crash.link.0001"
        )["body"]["data"]
        self.backend.interrupt_next_commit()
        with self.assertRaises(SimulatedCommitInterruption):  # type: ignore[attr-defined]
            self.backend.approve(
                task["id"], linked["id"], "Recovered reply", "contract.crash.approve.0001"
            )
        self.backend.restart()
        recovered = self.backend.state()
        self.assertEqual(len(recovered.replies), 1)
        self.assertIn("reply.approved", [event["type"] for event in recovered.activity])
        self.assertIn(
            "contract.crash.approve.0001",
            [record["key"] for record in recovered.idempotency],
        )
        replay = self.backend.approve(
            task["id"], linked["id"], "Recovered reply", "contract.crash.approve.0001"
        )
        self.assertTrue(replay["body"]["meta"]["replayed"])


class V3CaptureReplyContractTests(CaptureReplyContractCases, unittest.TestCase):
    def make_backend(self, root: Path) -> CaptureReplyContractBackend:
        return V3CaptureReplyBackend(root)


class V4CaptureReplyContractTests(CaptureReplyContractCases, unittest.TestCase):
    def make_backend(self, root: Path) -> CaptureReplyContractBackend:
        return V4CaptureReplyBackend(root)

    def test_v4_commands_are_default_off_without_filesystem_touch(self) -> None:
        before = sorted(path.relative_to(self.root.parent) for path in self.root.parent.rglob("*"))
        with self.assertRaisesRegex(Exception, "v4_capture_reply_commands_not_enabled"):
            V4CaptureReplyRepository(self.root, self.backend.runtime)  # type: ignore[attr-defined]
        after = sorted(path.relative_to(self.root.parent) for path in self.root.parent.rglob("*"))
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()

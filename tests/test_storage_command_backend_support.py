from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store
from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.capture_reply_repository import (
    CaptureReplyRepositoryError,
    V4CaptureReplyRepository,
)
from workstack.storage.command_backend_support import (
    V4CommandBackendSupportError,
    load_verified_command_baseline,
    materialized_authority_proposal,
)
from workstack.storage.journal import JournalTarget
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.storage.task_repository import TaskRepositoryError, V4TaskRepository


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _write_conversion(root: Path, conversion: object) -> None:
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
    streams: dict[tuple[str, str], list[dict]] = {}
    for kind, events in conversion.streams.items():
        for event in events:
            streams.setdefault((kind, str(event["created_at"])[:7]), []).append(
                dict(event)
            )
    for (kind, segment), events in streams.items():
        write(
            root / "streams" / kind / f"{segment}.ndjson",
            b"".join(
                canonical_json_bytes(event) + b"\n"
                for event in sorted(events, key=lambda item: item["sequence"])
            ),
        )


class CommandBackendProposalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_materialization_applies_delete_excludes_runtime_and_cleans_up(self) -> None:
        authority = self.root / "authority"
        authority.mkdir()
        old = b"old"
        (authority / "delete.json").write_bytes(old)
        (authority / "keep.json").write_bytes(old)
        targets = (
            JournalTarget.delete("delete.json", expected_digest=_digest(old)),
            JournalTarget.replace("keep.json", b"new", expected_digest=_digest(old)),
            JournalTarget.replace(
                "idempotency.json", b"runtime-only", expected_digest=None,
                scope="runtime",
            ),
        )
        proposal_parent = self.root / "proposals"
        with materialized_authority_proposal(
            authority, proposal_parent, targets, prefix="support-test-"
        ) as proposal:
            temporary = proposal.parent
            self.assertFalse((proposal / "delete.json").exists())
            self.assertEqual((proposal / "keep.json").read_bytes(), b"new")
            self.assertFalse((proposal / "idempotency.json").exists())
            self.assertTrue(temporary.exists())
        self.assertFalse(temporary.exists())

    def test_materialization_cleans_up_after_caller_failure(self) -> None:
        authority = self.root / "authority"
        authority.mkdir()
        parent = self.root / "proposals"
        with self.assertRaisesRegex(RuntimeError, "stop"):
            with materialized_authority_proposal(authority, parent, ()):
                raise RuntimeError("stop")
        self.assertEqual(list(parent.iterdir()), [])

    def test_materialization_cleans_up_after_target_application_failure(self) -> None:
        authority = self.root / "authority"
        authority.mkdir()
        parent = self.root / "proposals"
        missing = JournalTarget.delete(
            "missing.json", expected_digest=_digest(b"expected")
        )
        with self.assertRaises(FileNotFoundError):
            with materialized_authority_proposal(authority, parent, (missing,)):
                self.fail("a missing delete target must fail before yielding")
        self.assertEqual(list(parent.iterdir()), [])


class CommandBackendBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "v3"
        store = Store(source)
        WorkStack(store)
        documents = {name: store.load(name) for name in DEFAULTS}
        self.conversion = convert_v3_documents(
            documents, candidate_created_at="2026-09-01T00:00:00Z"
        )
        self.authority = self.root / "v4"
        _write_conversion(self.authority, self.conversion)
        self.runtime = resolve_runtime_authority(
            self.authority,
            self.root / "runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        self.runtime.runtime_root.mkdir(parents=True, exist_ok=True)
        self.runtime.idempotency_path.write_bytes(
            canonical_json_bytes(dict(self.conversion.idempotency_ledger))
        )
        publish_runtime_manifest(
            self.runtime.manifest_path,
            build_v4_manifest(read_v4(self.authority), generation=0),
            expected_digest=None,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_manifest_stale(self) -> None:
        path = self.authority / "workspace.json"
        workspace = json.loads(path.read_text(encoding="utf-8"))
        workspace["name"] = "Changed outside the command backend"
        path.write_bytes(canonical_json_bytes(workspace))

    def test_verified_baseline_refuses_stale_runtime_manifest(self) -> None:
        baseline = load_verified_command_baseline(self.authority, self.runtime)
        self.assertEqual(baseline.generation, 0)
        self._make_manifest_stale()
        with self.assertRaises(V4CommandBackendSupportError) as raised:
            load_verified_command_baseline(self.authority, self.runtime)
        self.assertEqual(raised.exception.code, "runtime_manifest_stale")

    def test_command_boundaries_preserve_content_free_stale_refusal(self) -> None:
        self._make_manifest_stale()
        repositories = (
            (
                V4CaptureReplyRepository(
                    self.authority,
                    self.runtime,
                    task_note_source_indexes=self.conversion.task_note_source_indexes,
                    enable_v4_capture_reply_commands=True,
                ),
                CaptureReplyRepositoryError,
            ),
            (
                V4TaskRepository(
                    self.authority,
                    self.runtime,
                    task_note_source_indexes=self.conversion.task_note_source_indexes,
                    enable_v4_task_commands=True,
                ),
                TaskRepositoryError,
            ),
        )
        for repository, error_type in repositories:
            with self.subTest(repository=type(repository).__name__):
                with self.assertRaises(error_type) as raised:
                    repository.state_documents()
                self.assertEqual(raised.exception.code, "runtime_manifest_stale")


if __name__ == "__main__":
    unittest.main()

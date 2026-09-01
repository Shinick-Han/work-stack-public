from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.journal import JournalTarget
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest, read_runtime_manifest
from workstack.storage.migration_conversion import V4Conversion, convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.storage.write_session import (
    V4WriteSessionError,
    execute_write_session,
    recover_write_session,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T00:00:00Z"


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _conversion() -> V4Conversion:
    documents = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in FIXTURE.glob("*.json")
    }
    return convert_v3_documents(documents, candidate_created_at=CREATED_AT)


def _write_conversion(root: Path, conversion: V4Conversion) -> None:
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
    grouped = {}
    for kind, events in conversion.streams.items():
        for event in events:
            grouped.setdefault((kind, str(event["created_at"])[:7]), []).append(event)
    for (kind, segment), events in sorted(grouped.items()):
        body = b"".join(
            canonical_json_bytes(dict(event)) + b"\n"
            for event in sorted(events, key=lambda item: int(item["sequence"]))
        )
        write(root / "streams" / kind / f"{segment}.ndjson", body)


class V4WriteSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.authority = self.base / "authority"
        self.authority.mkdir()
        self.conversion = _conversion()
        _write_conversion(self.authority, self.conversion)
        self.runtime = resolve_runtime_authority(
            self.authority,
            self.base / "runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        baseline = build_v4_manifest(read_v4(self.authority), generation=0)
        publish_runtime_manifest(
            self.runtime.manifest_path, baseline, expected_digest=None
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_code(self, code: str, action) -> None:
        with self.assertRaises(V4WriteSessionError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def proposal(self, count: int = 2):
        proposed_root = self.base / "proposed"
        if proposed_root.exists():
            shutil.rmtree(proposed_root)
        shutil.copytree(self.authority, proposed_root)
        task_paths = sorted(proposed_root.glob("records/tasks/*/*.json"))[:count]
        targets = []
        for index, proposed_path in enumerate(task_paths, 1):
            relative = proposed_path.relative_to(proposed_root).as_posix()
            authority_path = self.authority / relative
            current_body = authority_path.read_bytes()
            value = json.loads(proposed_path.read_text(encoding="utf-8"))
            value["title"] = f"Committed title {index}"
            value["revision"] += 1
            proposed_body = canonical_json_bytes(value)
            proposed_path.write_bytes(proposed_body)
            targets.append(
                JournalTarget.replace(
                    relative,
                    proposed_body,
                    expected_digest=_digest(current_body),
                )
            )
        manifest = build_v4_manifest(read_v4(proposed_root), generation=1)
        return tuple(sorted(targets, key=lambda target: (target.scope, target.artifact))), manifest

    def execute(self, targets, manifest, hook=None):
        return execute_write_session(
            self.runtime,
            targets,
            manifest,
            operation_id="operation-0001",
            created_at="2026-09-01T03:00:00Z",
            fault_hook=hook,
        )

    def test_commit_publishes_exact_generation_and_removes_journal(self) -> None:
        targets, proposed = self.proposal()
        ledger = JournalTarget.replace(
            self.runtime.idempotency_path.name,
            canonical_json_bytes(dict(self.conversion.idempotency_ledger)),
            expected_digest=None,
            scope="runtime",
        )

        result = self.execute(tuple(sorted((*targets, ledger), key=lambda item: (item.scope, item.artifact))), proposed)

        self.assertFalse(result.recovered)
        self.assertEqual(result.generation, 1)
        self.assertEqual(result.manifest.digest, proposed.digest)
        self.assertFalse(self.runtime.journal_path.exists())
        self.assertEqual(
            self.runtime.idempotency_path.read_bytes(), ledger.proposed_bytes
        )
        self.assertEqual(
            read_runtime_manifest(self.runtime.manifest_path).manifest.digest,
            proposed.digest,
        )
        self.assertEqual(build_v4_manifest(read_v4(self.authority), generation=1).digest, proposed.digest)

    def test_partial_target_replacement_recovers_exactly_once(self) -> None:
        targets, proposed = self.proposal()
        first_transition = f"target_replaced:authority:{targets[0].artifact}"

        def fail(transition: str) -> None:
            if transition == first_transition:
                raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.execute(targets, proposed, fail)

        self.assertTrue(self.runtime.journal_path.exists())
        self.assertEqual(_digest((self.authority / targets[0].artifact).read_bytes()), targets[0].proposed_digest)
        self.assertEqual(_digest((self.authority / targets[1].artifact).read_bytes()), targets[1].expected_digest)
        self.runtime.journal_path.with_name(
            self.runtime.journal_path.name + ".stage"
        ).write_bytes(b"interrupted phase publication")

        recovered = recover_write_session(self.runtime)

        self.assertTrue(recovered.recovered)
        self.assertEqual(recovered.manifest.digest, proposed.digest)
        self.assertFalse(self.runtime.journal_path.exists())
        for target in targets:
            self.assertEqual(
                _digest((self.authority / target.artifact).read_bytes()),
                target.proposed_digest,
            )

    def test_manifest_published_before_interruption_is_completed_without_rewrite(self) -> None:
        targets, proposed = self.proposal()

        def fail(transition: str) -> None:
            if transition == "manifest_published":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.execute(targets, proposed, fail)
        persisted = read_runtime_manifest(self.runtime.manifest_path)
        self.assertEqual(persisted.manifest.digest, proposed.digest)
        self.assertTrue(self.runtime.journal_path.exists())

        recovered = recover_write_session(self.runtime)

        self.assertTrue(recovered.recovered)
        self.assertEqual(recovered.generation, 1)
        self.assertFalse(self.runtime.journal_path.exists())

    def test_generation_phase_interruption_removes_only_the_verified_journal(self) -> None:
        targets, proposed = self.proposal()

        def fail(transition: str) -> None:
            if transition == "journal_generation-published":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.execute(targets, proposed, fail)
        self.assertEqual(
            read_runtime_manifest(self.runtime.manifest_path).manifest.digest,
            proposed.digest,
        )
        self.assertTrue(self.runtime.journal_path.exists())

        recovered = recover_write_session(self.runtime)

        self.assertTrue(recovered.recovered)
        self.assertEqual(recovered.manifest.digest, proposed.digest)
        self.assertFalse(self.runtime.journal_path.exists())

    def test_unrelated_external_change_fails_closed_and_retains_journal(self) -> None:
        targets, proposed = self.proposal()

        def fail(transition: str) -> None:
            if transition == f"target_replaced:authority:{targets[0].artifact}":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.execute(targets, proposed, fail)
        unrelated = next(self.authority.glob("records/notes/*/*.json"))
        value = json.loads(unrelated.read_text(encoding="utf-8"))
        value["text"] = "External unowned edit"
        value["revision"] += 1
        unrelated.write_bytes(canonical_json_bytes(value))

        self.assert_code(
            "UNRELATED_ARTIFACT_CHANGED",
            lambda: recover_write_session(self.runtime),
        )
        self.assertTrue(self.runtime.journal_path.exists())

    def test_ambiguous_target_change_fails_closed_and_retains_journal(self) -> None:
        targets, proposed = self.proposal()

        def fail(transition: str) -> None:
            if transition == "journal_applying":
                raise RuntimeError("injected")

        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.execute(targets, proposed, fail)
        path = self.authority / targets[0].artifact
        value = json.loads(path.read_text(encoding="utf-8"))
        value["title"] = "A third, unowned value"
        value["revision"] += 2
        path.write_bytes(canonical_json_bytes(value))

        self.assert_code(
            "TARGET_STATE_AMBIGUOUS",
            lambda: recover_write_session(self.runtime),
        )
        self.assertTrue(self.runtime.journal_path.exists())

    def test_caller_manifest_mismatch_is_refused_before_journal_creation(self) -> None:
        targets, proposed = self.proposal()
        changed = JournalTarget.replace(
            targets[0].artifact,
            targets[0].proposed_bytes + b" ",
            expected_digest=targets[0].expected_digest,
        )

        self.assert_code(
            "TARGET_PROPOSED_DIGEST_MISMATCH",
            lambda: self.execute((changed, *targets[1:]), proposed),
        )
        self.assertFalse(self.runtime.journal_path.exists())
        self.assertEqual(read_runtime_manifest(self.runtime.manifest_path).generation, 0)

    def test_no_pending_journal_is_a_noop(self) -> None:
        self.assertIsNone(recover_write_session(self.runtime))


if __name__ == "__main__":
    unittest.main()

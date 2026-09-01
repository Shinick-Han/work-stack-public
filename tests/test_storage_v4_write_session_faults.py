from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.journal import JournalTarget, MAX_JOURNAL_BYTES
from workstack.storage.lease import StorageLeaseError
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import publish_runtime_manifest, read_runtime_manifest
from workstack.storage.migration_conversion import convert_v3_documents
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from workstack.storage import write_session as writer


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "store-v3" / "populated"
CREATED_AT = "2026-09-01T00:00:00Z"
OPERATION_ID = "fault-operation-0001"


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _tree(root: Path, *, ignore: frozenset[str] = frozenset()) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).as_posix() not in ignore
    }


def _authority_tree(root: Path) -> dict[str, bytes]:
    return {
        artifact: body
        for artifact, body in _tree(root).items()
        if ".workstack-stage-" not in Path(artifact).name
    }


class _Scenario:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.authority = base / "authority"
        self.authority.mkdir()
        documents = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in FIXTURE.glob("*.json")
        }
        self.conversion = convert_v3_documents(
            documents, candidate_created_at=CREATED_AT
        )
        self._write_conversion()
        self.runtime = resolve_runtime_authority(
            self.authority,
            base / "runtime",
            str(self.conversion.store["workspace_uid"]),
        )
        baseline = build_v4_manifest(read_v4(self.authority), generation=0)
        publish_runtime_manifest(
            self.runtime.manifest_path, baseline, expected_digest=None
        )
        self.baseline_manifest = baseline
        self.baseline_authority = _authority_tree(self.authority)
        self.targets, self.proposed = self._proposal()

    def _write_conversion(self) -> None:
        _write(
            self.authority / "store.json",
            canonical_json_bytes(dict(self.conversion.store)),
        )
        _write(
            self.authority / "workspace.json",
            canonical_json_bytes(dict(self.conversion.workspace)),
        )
        for kind, records in self.conversion.records.items():
            for record in records:
                uid = str(record["uid"])
                _write(
                    self.authority / "records" / kind / uid[:2] / f"{uid}.json",
                    canonical_json_bytes(dict(record)),
                )
        grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
        for kind, events in self.conversion.streams.items():
            for event in events:
                grouped.setdefault((kind, str(event["created_at"])[:7]), []).append(
                    dict(event)
                )
        for (kind, segment), events in sorted(grouped.items()):
            body = b"".join(
                canonical_json_bytes(event) + b"\n"
                for event in sorted(events, key=lambda item: int(item["sequence"]))
            )
            _write(self.authority / "streams" / kind / f"{segment}.ndjson", body)

    def _proposal(self):
        proposed_root = self.base / "proposed"
        shutil.copytree(self.authority, proposed_root)
        proposed_path = sorted(proposed_root.glob("records/tasks/*/*.json"))[0]
        relative = proposed_path.relative_to(proposed_root).as_posix()
        current = (self.authority / relative).read_bytes()
        value = json.loads(proposed_path.read_text(encoding="utf-8"))
        value["title"] = "Fault-tested title"
        value["revision"] += 1
        body = canonical_json_bytes(value)
        proposed_path.write_bytes(body)
        authority_target = JournalTarget.replace(
            relative, body, expected_digest=_digest(current)
        )
        ledger_target = JournalTarget.replace(
            self.runtime.idempotency_path.name,
            canonical_json_bytes(dict(self.conversion.idempotency_ledger)),
            expected_digest=None,
            scope="runtime",
        )
        manifest = build_v4_manifest(read_v4(proposed_root), generation=1)
        targets = tuple(
            sorted(
                (authority_target, ledger_target),
                key=lambda target: (target.scope, target.artifact),
            )
        )
        return targets, manifest

    def execute(self, hook=None):
        return writer.execute_write_session(
            self.runtime,
            self.targets,
            self.proposed,
            operation_id=OPERATION_ID,
            created_at="2026-09-01T03:00:00Z",
            fault_hook=hook,
        )

    def assert_baseline(self, case: unittest.TestCase) -> None:
        case.assertEqual(_authority_tree(self.authority), self.baseline_authority)
        state = read_runtime_manifest(self.runtime.manifest_path)
        case.assertEqual(state.manifest.digest, self.baseline_manifest.digest)
        case.assertFalse(self.runtime.idempotency_path.exists())

    def assert_committed(self, case: unittest.TestCase) -> None:
        state = read_runtime_manifest(self.runtime.manifest_path)
        case.assertEqual(state.manifest.digest, self.proposed.digest)
        case.assertEqual(state.generation, 1)
        case.assertEqual(
            self.runtime.idempotency_path.read_bytes(),
            self.targets[1].proposed_bytes,
        )
        case.assertEqual(
            build_v4_manifest(read_v4(self.authority), generation=1).digest,
            self.proposed.digest,
        )


class V4WriteSessionFaultTests(unittest.TestCase):
    def scenario(self, temporary: tempfile.TemporaryDirectory) -> _Scenario:
        return _Scenario(Path(temporary.name))

    def assert_code(self, code: str, action) -> None:
        with self.assertRaises(writer.V4WriteSessionError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_every_emitted_fault_transition_is_recoverable_or_already_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            discovery = _Scenario(Path(directory))
            transitions: list[str] = []
            discovery.execute(transitions.append)
        self.assertEqual(
            transitions,
            [
                "lease_acquired",
                "baseline_verified",
                "journal_prepared",
                "journal_applying",
                f"target_staged:authority:{discovery.targets[0].artifact}",
                f"target_staged:runtime:{discovery.targets[1].artifact}",
                "targets_staged",
                f"target_replaced:authority:{discovery.targets[0].artifact}",
                f"target_replaced:runtime:{discovery.targets[1].artifact}",
                "authority_verified",
                "manifest_published",
                "journal_manifest-published",
                "generation_published",
                "journal_generation-published",
                "journal_removed",
            ],
        )

        for transition in transitions:
            with self.subTest(transition=transition), tempfile.TemporaryDirectory() as directory:
                scenario = _Scenario(Path(directory))

                def fail(current: str) -> None:
                    if current == transition:
                        raise RuntimeError("injected transition")

                with self.assertRaisesRegex(RuntimeError, "injected transition"):
                    scenario.execute(fail)
                if transition in {"lease_acquired", "baseline_verified"}:
                    scenario.assert_baseline(self)
                    self.assertFalse(scenario.runtime.journal_path.exists())
                    continue
                if transition == "journal_removed":
                    scenario.assert_committed(self)
                    self.assertIsNone(writer.recover_write_session(scenario.runtime))
                    continue
                self.assertTrue(scenario.runtime.journal_path.exists())
                recovered = writer.recover_write_session(scenario.runtime)
                self.assertTrue(recovered.recovered)
                scenario.assert_committed(self)
                self.assertFalse(scenario.runtime.journal_path.exists())

    def test_disk_full_during_stage_fsync_retains_journal_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            original = writer._write_stage

            def disk_full(path: Path, body: bytes) -> None:
                with patch.object(
                    writer.os,
                    "fsync",
                    side_effect=OSError(errno.ENOSPC, "disk full"),
                ):
                    original(path, body)

            with patch.object(writer, "_write_stage", side_effect=disk_full):
                self.assert_code("TARGET_STAGE_FAILED", scenario.execute)
            scenario.assert_baseline(self)
            journal_before = scenario.runtime.journal_path.read_bytes()
            recovered = writer.recover_write_session(scenario.runtime)
            self.assertTrue(recovered.recovered)
            self.assertNotEqual(journal_before, b"")
            scenario.assert_committed(self)
            self.assertFalse(scenario.runtime.journal_path.exists())

    def test_stage_write_failure_removes_owned_partial_and_retains_journal(self) -> None:
        class FailedWrite:
            def __init__(self, wrapped) -> None:
                self.wrapped = wrapped

            def __enter__(self):
                return self

            def __exit__(self, *ignored) -> None:
                self.wrapped.close()

            def write(self, body: bytes) -> int:
                raise OSError(errno.EIO, "write failed")

            def flush(self) -> None:
                self.wrapped.flush()

            def fileno(self) -> int:
                return self.wrapped.fileno()

        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            original_stage = writer._write_stage
            original_fdopen = writer.os.fdopen

            def fail_write(path: Path, body: bytes) -> None:
                def wrapped_fdopen(*arguments, **keywords):
                    return FailedWrite(original_fdopen(*arguments, **keywords))

                with patch.object(writer.os, "fdopen", side_effect=wrapped_fdopen):
                    original_stage(path, body)

            with patch.object(writer, "_write_stage", side_effect=fail_write):
                self.assert_code("TARGET_STAGE_FAILED", scenario.execute)
            scenario.assert_baseline(self)
            target = scenario.targets[0]
            stage = writer._stage_path(
                scenario.authority / target.artifact, OPERATION_ID, target
            )
            self.assertFalse(stage.exists())
            journal_before = scenario.runtime.journal_path.read_bytes()
            writer.recover_write_session(scenario.runtime)
            self.assertTrue(journal_before)
            scenario.assert_committed(self)

    def test_journal_fsync_failure_leaves_authority_and_runtime_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            (scenario.runtime.runtime_root / "writer.lock").write_bytes(b"\0")
            original = writer._publish_journal

            def disk_full(path, journal, *, expected_digest):
                with patch.object(
                    writer.os,
                    "fsync",
                    side_effect=OSError(errno.ENOSPC, "disk full"),
                ):
                    return original(path, journal, expected_digest=expected_digest)

            with patch.object(writer, "_publish_journal", side_effect=disk_full):
                self.assert_code("JOURNAL_PUBLISH_FAILED", scenario.execute)
            scenario.assert_baseline(self)
            self.assertFalse(scenario.runtime.journal_path.exists())
            self.assertFalse(
                scenario.runtime.journal_path.with_name(
                    scenario.runtime.journal_path.name + ".stage"
                ).exists()
            )

    def test_locked_target_replace_retains_exact_journal_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            target_path = (
                scenario.authority / scenario.targets[0].artifact
            ).resolve(strict=False)
            original = writer.os.replace

            def locked(source, destination):
                if Path(destination).resolve(strict=False) == target_path:
                    raise PermissionError(errno.EACCES, "locked")
                return original(source, destination)

            with patch.object(writer.os, "replace", side_effect=locked):
                self.assert_code("TARGET_REPLACE_FAILED", scenario.execute)
            scenario.assert_baseline(self)
            journal_before = scenario.runtime.journal_path.read_bytes()
            writer.recover_write_session(scenario.runtime)
            scenario.assert_committed(self)
            self.assertTrue(journal_before)

    def test_malformed_and_oversized_journals_fail_closed_byte_exact(self) -> None:
        cases = ((b"{", "JOURNAL_JSON_INVALID"), (b"x" * (MAX_JOURNAL_BYTES + 1), "JOURNAL_BYTE_LIMIT_EXCEEDED"))
        for body, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                scenario = _Scenario(Path(directory))
                scenario.runtime.journal_path.write_bytes(body)
                self.assert_code(code, lambda: writer.recover_write_session(scenario.runtime))
                scenario.assert_baseline(self)
                self.assertEqual(scenario.runtime.journal_path.read_bytes(), body)

    def test_duplicate_recovery_is_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))

            def fail(transition: str) -> None:
                if transition.startswith("target_replaced:authority:"):
                    raise RuntimeError("interrupt")

            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                scenario.execute(fail)
            first = writer.recover_write_session(scenario.runtime)
            second = writer.recover_write_session(scenario.runtime)
            self.assertEqual(first.operation_id, OPERATION_ID)
            self.assertTrue(first.recovered)
            self.assertIsNone(second)
            scenario.assert_committed(self)

    def test_owned_stage_residue_is_rewritten_but_conflicting_residue_is_refused(self) -> None:
        for conflict in (False, True):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                scenario = _Scenario(Path(directory))

                def fail(transition: str) -> None:
                    if transition == "journal_applying":
                        raise RuntimeError("interrupt")

                with self.assertRaisesRegex(RuntimeError, "interrupt"):
                    scenario.execute(fail)
                target = scenario.targets[0]
                target_path = scenario.authority / target.artifact
                stage = writer._stage_path(target_path, OPERATION_ID, target)
                stage.write_bytes(b"conflict" if conflict else target.proposed_bytes)
                if conflict:
                    journal_before = scenario.runtime.journal_path.read_bytes()
                    self.assert_code(
                        "STAGE_FILE_CONFLICT",
                        lambda: writer.recover_write_session(scenario.runtime),
                    )
                    scenario.assert_baseline(self)
                    self.assertEqual(
                        scenario.runtime.journal_path.read_bytes(), journal_before
                    )
                    self.assertEqual(stage.read_bytes(), b"conflict")
                else:
                    writer.recover_write_session(scenario.runtime)
                    scenario.assert_committed(self)
                    self.assertFalse(stage.exists())

    def test_initial_journal_stage_residue_blocks_new_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            residue = scenario.runtime.journal_path.with_name(
                scenario.runtime.journal_path.name + ".stage"
            )
            residue.write_bytes(b"unowned residue")
            self.assert_code("JOURNAL_STAGE_EXISTS", scenario.execute)
            scenario.assert_baseline(self)
            self.assertEqual(residue.read_bytes(), b"unowned residue")

    def test_runtime_ledger_replacement_recovers_from_post_replace_fault(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            transition = (
                f"target_replaced:runtime:{scenario.runtime.idempotency_path.name}"
            )

            def fail(current: str) -> None:
                if current == transition:
                    raise RuntimeError("interrupt")

            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                scenario.execute(fail)
            self.assertEqual(
                scenario.runtime.idempotency_path.read_bytes(),
                scenario.targets[1].proposed_bytes,
            )
            journal_before = scenario.runtime.journal_path.read_bytes()
            writer.recover_write_session(scenario.runtime)
            self.assertTrue(journal_before)
            scenario.assert_committed(self)

    def test_delete_target_unlinks_and_recovers_from_absent_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            proposed_root = scenario.base / "delete-proposed"
            shutil.copytree(scenario.authority, proposed_root)
            target_path = sorted(proposed_root.glob("records/notes/*/*.json"))[0]
            artifact = target_path.relative_to(proposed_root).as_posix()
            expected_body = (scenario.authority / artifact).read_bytes()
            target_path.unlink()
            proposed = build_v4_manifest(read_v4(proposed_root), generation=1)
            target = JournalTarget.delete(artifact, expected_digest=_digest(expected_body))

            def fail(transition: str) -> None:
                if transition == f"target_deleted:authority:{artifact}":
                    raise RuntimeError("interrupt")

            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                writer.execute_write_session(
                    scenario.runtime,
                    (target,),
                    proposed,
                    operation_id="delete-operation-0001",
                    created_at="2026-09-01T04:00:00Z",
                    fault_hook=fail,
                )
            self.assertFalse((scenario.authority / artifact).exists())
            journal_before = scenario.runtime.journal_path.read_bytes()
            result = writer.recover_write_session(scenario.runtime)
            self.assertTrue(result.recovered)
            self.assertTrue(journal_before)
            self.assertFalse(scenario.runtime.journal_path.exists())
            self.assertEqual(
                build_v4_manifest(read_v4(scenario.authority), generation=1).digest,
                proposed.digest,
            )

    def test_second_process_writer_lease_contention_refuses_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            ready = scenario.base / "lease-ready"
            release = scenario.base / "lease-release"
            code = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from workstack.storage.lease import StorageWriterLease\n"
                "lease=StorageWriterLease(Path(sys.argv[1])); lease.acquire()\n"
                "Path(sys.argv[2]).write_text('ready', encoding='utf-8')\n"
                "deadline=time.time()+15\n"
                "while not Path(sys.argv[3]).exists() and time.time()<deadline: time.sleep(.02)\n"
                "lease.release()\n"
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(scenario.runtime.runtime_root / "writer.lock"),
                    str(ready),
                    str(release),
                ],
                cwd=ROOT,
            )
            try:
                deadline = time.time() + 8
                while not ready.exists() and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                with self.assertRaises(StorageLeaseError):
                    scenario.execute()
                scenario.assert_baseline(self)
                self.assertFalse(scenario.runtime.journal_path.exists())
            finally:
                release.write_text("release", encoding="utf-8")
                process.wait(timeout=10)
            self.assertEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()

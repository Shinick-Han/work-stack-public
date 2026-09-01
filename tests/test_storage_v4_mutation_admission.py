from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from workstack.storage.canonical import canonical_json_bytes
from workstack.storage.manifest import build_v4_manifest
from workstack.storage.manifest_store import read_runtime_manifest
from workstack.storage.mutation_repository import (
    V4MutationAdmissionError,
    admit_experimental_v4_mutation_repository,
)
from workstack.storage.reader import read_v4
from workstack.storage.runtime import resolve_runtime_authority
from tests.test_storage_v4_write_session_faults import _Scenario


class V4MutationAdmissionTests(unittest.TestCase):
    def assert_code(self, code: str, action) -> None:
        with self.assertRaises(V4MutationAdmissionError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def admit(self, scenario: _Scenario):
        return admit_experimental_v4_mutation_repository(
            scenario.authority,
            scenario.runtime,
            allow_v4_mutation=True,
        )

    def test_default_off_refuses_before_touching_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "authority"
            runtime = Path(directory) / "runtime"
            self.assert_code(
                "V4_MUTATION_OPT_IN_REQUIRED",
                lambda: admit_experimental_v4_mutation_repository(root, None),
            )
            self.assertFalse(root.exists())
            self.assertFalse(runtime.exists())

    def test_runtime_authority_and_manifest_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            self.assert_code(
                "RUNTIME_AUTHORITY_REQUIRED",
                lambda: admit_experimental_v4_mutation_repository(
                    scenario.authority, None, allow_v4_mutation=True
                ),
            )
            scenario.runtime.manifest_path.unlink()
            self.assert_code(
                "RUNTIME_MANIFEST_MISSING", lambda: self.admit(scenario)
            )

    def test_runtime_must_bind_exact_workspace_and_authority_location(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            wrong_workspace = resolve_runtime_authority(
                scenario.authority,
                scenario.base / "other-runtime",
                "11111111-1111-1111-1111-111111111111",
            )
            self.assert_code(
                "RUNTIME_WORKSPACE_MISMATCH",
                lambda: admit_experimental_v4_mutation_repository(
                    scenario.authority,
                    wrong_workspace,
                    allow_v4_mutation=True,
                ),
            )
            forged = replace(
                scenario.runtime,
                authority_key="authority-" + "0" * 32,
            )
            self.assert_code(
                "RUNTIME_AUTHORITY_MISMATCH",
                lambda: admit_experimental_v4_mutation_repository(
                    scenario.authority, forged, allow_v4_mutation=True
                ),
            )

    def test_mixed_format_and_plain_v3_are_never_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            (scenario.authority / "backlog.json").write_text(
                '{"private":"not echoed"}', encoding="utf-8"
            )
            self.assert_code("AMBIGUOUS_STORAGE_FORMAT", lambda: self.admit(scenario))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "legacy"
            root.mkdir()
            runtime = resolve_runtime_authority(
                root,
                Path(directory) / "runtime",
                "11111111-1111-1111-1111-111111111111",
            )
            self.assert_code(
                "V4_AUTHORITY_REQUIRED",
                lambda: admit_experimental_v4_mutation_repository(
                    root, runtime, allow_v4_mutation=True
                ),
            )

    def test_stale_manifest_is_refused_without_writing_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            target = scenario.authority / scenario.targets[0].artifact
            value = json.loads(target.read_text(encoding="utf-8"))
            value["title"] = "external valid mutation"
            value["revision"] += 1
            target.write_bytes(canonical_json_bytes(value))
            manifest_before = scenario.runtime.manifest_path.read_bytes()

            self.assert_code("RUNTIME_MANIFEST_STALE", lambda: self.admit(scenario))

            self.assertEqual(
                scenario.runtime.manifest_path.read_bytes(), manifest_before
            )
            self.assertFalse(scenario.runtime.journal_path.exists())

    def test_unambiguous_pending_journal_recovers_before_admission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))

            def fail(transition: str) -> None:
                if transition.startswith("target_replaced:authority:"):
                    raise RuntimeError("interrupt")

            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                scenario.execute(fail)
            session = self.admit(scenario)

            self.assertIsNotNone(session.recovered)
            self.assertTrue(session.recovered.recovered)
            self.assertEqual(session.generation, 1)
            self.assertEqual(session.manifest.digest, scenario.proposed.digest)
            self.assertFalse(scenario.runtime.journal_path.exists())
            task_uid = Path(scenario.targets[0].artifact).stem
            task = next(
                value
                for value in session.repository.read().records["tasks"]
                if value["uid"] == task_uid
            )
            self.assertEqual(task["title"], "Fault-tested title")
            scenario.assert_committed(self)

    def test_ambiguous_pending_recovery_is_retained_and_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))

            def fail(transition: str) -> None:
                if transition == "journal_applying":
                    raise RuntimeError("interrupt")

            with self.assertRaisesRegex(RuntimeError, "interrupt"):
                scenario.execute(fail)
            target = scenario.authority / scenario.targets[0].artifact
            value = json.loads(target.read_text(encoding="utf-8"))
            value["title"] = "ambiguous external mutation"
            value["revision"] += 3
            target.write_bytes(canonical_json_bytes(value))
            journal_before = scenario.runtime.journal_path.read_bytes()

            self.assert_code("PENDING_RECOVERY_REFUSED", lambda: self.admit(scenario))

            self.assertEqual(scenario.runtime.journal_path.read_bytes(), journal_before)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["title"],
                "ambiguous external mutation",
            )

    def test_session_delegates_generic_commit_and_advances_its_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            session = self.admit(scenario)

            result = session.commit(
                scenario.targets,
                scenario.proposed,
                operation_id="admitted-operation-0001",
                created_at="2026-09-01T05:00:00Z",
            )

            self.assertEqual(result.generation, 1)
            self.assertEqual(session.generation, 1)
            self.assertEqual(session.manifest.digest, scenario.proposed.digest)
            self.assertEqual(
                read_runtime_manifest(scenario.runtime.manifest_path).manifest.digest,
                scenario.proposed.digest,
            )

    def test_session_refuses_when_another_writer_advanced_its_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = _Scenario(Path(directory))
            session = self.admit(scenario)
            scenario.execute()
            self.assertEqual(
                build_v4_manifest(read_v4(scenario.authority), generation=1).digest,
                scenario.proposed.digest,
            )

            self.assert_code(
                "MUTATION_SESSION_STALE",
                lambda: session.commit(
                    scenario.targets,
                    scenario.proposed,
                    operation_id="stale-operation-0001",
                    created_at="2026-09-01T06:00:00Z",
                ),
            )


if __name__ == "__main__":
    unittest.main()

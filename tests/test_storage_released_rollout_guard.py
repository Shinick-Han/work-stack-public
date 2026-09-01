from __future__ import annotations

import contextlib
import inspect
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workstack import cli
from workstack.storage.experimental_application import (
    ExperimentalV4ApplicationError,
    create_experimental_v4_application,
)
from workstack.service import WorkStack
from workstack.store import DEFAULTS, Store
from workstack.storage.domain_v4_composition import (
    V4DomainCompositionError,
    compose_experimental_v4_domain,
)
from workstack.storage.repository import (
    RepositoryAdmissionError,
    admit_released_repository,
    admit_test_read_repository,
)


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import v4_activation_binding as V4_ACTIVATION


def _authority_bytes(root: Path) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in sorted(DEFAULTS)}


def _unexpected_call(name: str):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"released path called {name}")

    return fail


class ReleasedStartupGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_startup_and_restart_preserve_existing_v3_bytes_without_migration(self) -> None:
        data = self.root / "existing-v3"
        WorkStack(Store(data))
        expected = _authority_bytes(data)
        forbidden = (
            "plan_v3_migration", "preview_v3_migration", "execute_v3_migration",
            "resume_v3_migration", "verify_v3_migration_artifacts",
            "write_v4_backup", "verify_v4_backup", "restore_v4_backup",
        )
        for _restart in range(2):
            with contextlib.ExitStack() as patches:
                for name in forbidden:
                    patches.enter_context(
                        mock.patch.object(cli, name, side_effect=_unexpected_call(name))
                    )
                serve = patches.enter_context(mock.patch.object(cli, "serve"))
                self.assertEqual(
                    cli.main([
                        "--data-dir", str(data), "graph", "serve",
                        "--host", "127.0.0.1", "--port", "0",
                    ]),
                    0,
                )
                serve.assert_called_once()
            self.assertEqual(_authority_bytes(data), expected)
        self.assertFalse((data / "store.json").exists())

    def test_new_released_workspace_is_v3_only(self) -> None:
        data = self.root / "new"
        WorkStack(Store(data))
        self.assertEqual(set(_authority_bytes(data)), set(DEFAULTS))
        self.assertFalse((data / "store.json").exists())
        self.assertEqual(
            json.loads((data / "store-meta.json").read_text(encoding="utf-8"))[
                "store_schema_version"
            ],
            3,
        )
        admission = admit_released_repository(data)
        self.assertEqual((admission.format_version, admission.mode), (3, "released-v3"))

    def test_released_and_default_test_admission_reject_v4_without_touching_it(self) -> None:
        candidate = self.root / "v4-candidate"
        candidate.mkdir()
        marker = candidate / "store.json"
        marker.write_bytes(b"{}")
        before = marker.read_bytes()
        with self.assertRaises(RepositoryAdmissionError) as released:
            admit_released_repository(candidate)
        self.assertEqual(released.exception.code, "V4_NOT_RELEASED")
        with self.assertRaises(RepositoryAdmissionError) as test_default:
            admit_test_read_repository(candidate)
        self.assertEqual(test_default.exception.code, "V4_TEST_OPT_IN_REQUIRED")
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(list(candidate.iterdir()), [marker])


class ExplicitStorageCommandGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "v3"
        WorkStack(Store(self.data))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_explicit_migration_and_backup_commands_bypass_application_startup(self) -> None:
        expected = _authority_bytes(self.data)
        application_forbidden = (
            mock.patch.object(cli, "Store", side_effect=_unexpected_call("Store")),
            mock.patch.object(cli, "WorkStack", side_effect=_unexpected_call("WorkStack")),
        )
        with application_forbidden[0], application_forbidden[1], contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main(["storage", "migration", "plan", str(self.data)]), 0
            )
            self.assertEqual(
                cli.main([
                    "storage", "v4-backup", "verify",
                    str(self.root / "missing.zip"),
                ]),
                2,
            )
        self.assertEqual(_authority_bytes(self.data), expected)
        self.assertFalse(any(self.root.glob("*.workstack-v4-candidate-*")))
        self.assertFalse(any(self.root.glob("*.workstack-v3-backup-*")))


class ReleasedActivationFlagGuardTests(unittest.TestCase):
    def test_every_v4_activation_or_composition_entry_is_default_off(self) -> None:
        defaults = (
            (create_experimental_v4_application, "enable_v4_application"),
            (compose_experimental_v4_domain, "enable_v4_domain"),
            (V4_ACTIVATION.issue_v4_activation_proof, "enable_v4_activation"),
            (V4_ACTIVATION.prepare_v4_activation_receipt, "enable_v4_activation"),
            (V4_ACTIVATION.confirm_v4_activation_after_restart, "enable_v4_activation"),
            (admit_test_read_repository, "allow_v4"),
        )
        for function, parameter in defaults:
            with self.subTest(function=function.__name__):
                self.assertIs(inspect.signature(function).parameters[parameter].default, False)

        with self.assertRaises(ExperimentalV4ApplicationError) as application:
            create_experimental_v4_application(
                self.id(), None, clock=lambda: "", uid_factory=lambda: ""
            )
        self.assertEqual(application.exception.code, "V4_APPLICATION_OPT_IN_REQUIRED")
        with self.assertRaises(V4DomainCompositionError) as domain:
            compose_experimental_v4_domain(self.id(), None, clock=lambda: "")
        self.assertEqual(domain.exception.code, "V4_DOMAIN_OPT_IN_REQUIRED")
        with self.assertRaises(V4_ACTIVATION.V4ActivationDisabledError):
            V4_ACTIVATION.issue_v4_activation_proof(
                None, None, registry_digest="invalid"  # type: ignore[arg-type]
            )
        with self.assertRaises(V4_ACTIVATION.V4ActivationDisabledError):
            V4_ACTIVATION.prepare_v4_activation_receipt(
                None,  # type: ignore[arg-type]
                previous_registry_digest="invalid",
                rollback_artifact_digest="invalid",
            )
        with self.assertRaises(V4_ACTIVATION.V4ActivationDisabledError):
            V4_ACTIVATION.confirm_v4_activation_after_restart(
                None,  # type: ignore[arg-type]
                None,  # type: ignore[arg-type]
                current_registry_digest="invalid",
                reinspect=lambda: None,  # type: ignore[arg-type]
            )


class WindowsReleaseScriptGuardTests(unittest.TestCase):
    """Freeze the data-bearing PowerShell command contract without executing Windows."""

    def read(self, name: str) -> str:
        return (ROOT / "scripts" / "windows" / name).read_text(encoding="utf-8-sig")

    def test_installer_update_and_start_scripts_expose_no_storage_migration_route(self) -> None:
        scripts = {
            name: self.read(name)
            for name in (
                "Install-WorkStack.ps1", "Update-WorkStack.ps1",
                "Apply-WorkStackUpdate.ps1", "Start-WorkStack.ps1",
            )
        }
        forbidden = (
            "execute_v3_migration", "resume_v3_migration", "preview_v3_migration",
            "enable_v4_activation", "enable_v4_application", "allow_v4_mutation",
        )
        for name, script in scripts.items():
            with self.subTest(name=name):
                folded = script.casefold()
                self.assertTrue(all(token.casefold() not in folded for token in forbidden))
                self.assertNotRegex(folded, r"\bstorage\s+(?:migration|v4-backup)\b")

        start = scripts["Start-WorkStack.ps1"]
        self.assertRegex(
            start,
            r"(?s)'--data-dir'.*?'graph'.*?'serve'.*?'--host'.*?'127\.0\.0\.1'",
        )
        self.assertIn(
            "--data-dir $dataPath maintenance backup --out $backupPath", start
        )
        installer = scripts["Install-WorkStack.ps1"]
        self.assertIn(
            "--data-dir $dataPath maintenance backup --out $backupRoot", installer
        )
        update = scripts["Update-WorkStack.ps1"]
        self.assertIn("-DataDir $dataPath", update)
        self.assertNotIn("run_work_stack.py", update)

    def test_shipping_two_release_smoke_requires_exact_ssot_preservation(self) -> None:
        script = self.read("Test-WorkStackUpgrade.ps1")
        self.assertIn("PreviousSetupPath", script)
        self.assertIn("CandidateSetupPath", script)
        self.assertIn("release-gate-marker.txt", script)
        self.assertIn("Get-SsotByteManifest", script)
        self.assertIn("Assert-SsotByteManifest", script)
        self.assertIn("Authoritative SSOT bytes were not preserved", script)
        self.assertIn("The SSOT marker was not preserved", script)
        self.assertIn("Rollback did not preserve the SSOT marker", script)


if __name__ == "__main__":
    unittest.main()

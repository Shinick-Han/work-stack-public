from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from workstack import cli
from workstack.store import DEFAULTS, Store


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "desktop" / "python-webview-shell" / "workstack_desktop.py"
SPEC = importlib.util.spec_from_file_location("workstack_desktop_registry_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    SPEC.loader.exec_module(MODULE)

# The desktop module puts its own directory on sys.path; this is the very
# module instance whose functions the host calls.
import connection_registry_startup as STARTUP  # noqa: E402


PROFILE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"


def bare_host(root: Path):
    host = object.__new__(MODULE.WorkStackDesktopHost)
    host.state_root = root
    host.options = argparse.Namespace(url=None)
    host.local_startup_selection = None
    host._origin = mock.Mock(side_effect=lambda url: url.rstrip("/"))
    host._local_runtime_config = mock.Mock(return_value=(
        {"data_dir": str(root / "legacy"), "port": 8765}, root / "config.json"
    ))
    # An empty state root with an absent data_dir is a fresh installation; these
    # hosts mock the registry, so the first-launch bootstrap is stubbed here and
    # exercised for real in DesktopFreshInstallBootstrapTest.
    host._initialize_fresh_local_store = mock.Mock()
    return host


class DesktopConnectionRegistryStartupTest(unittest.TestCase):
    def test_registry_startup_is_on_by_default_with_explicit_recovery_opt_out(self) -> None:
        self.assertTrue(MODULE.connection_registry_startup_enabled({}))
        self.assertTrue(MODULE.connection_registry_startup_enabled({"WORKSTACK_CONNECTION_REGISTRY_V1": "1"}))
        self.assertFalse(MODULE.connection_registry_startup_enabled({"WORKSTACK_CONNECTION_REGISTRY_V1": "0"}))

    def test_registry_startup_stays_dark_until_release_gate_is_enabled(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.startup_error = None
        host.startup_ready = mock.Mock()
        host.connection_registry_startup_enabled = False
        host._prepare_connection_registry_runtime = mock.Mock()
        host._initialize_fresh_local_store = mock.Mock()
        host._ensure_server = mock.Mock()

        host._prepare_server()

        host._prepare_connection_registry_runtime.assert_not_called()
        host._initialize_fresh_local_store.assert_not_called()
        host._ensure_server.assert_called_once()

    def test_prepare_server_selects_registry_before_starting_runtime(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.startup_error = None
        host.startup_ready = mock.Mock()
        host.connection_registry_startup_enabled = True
        host._prepare_connection_registry_runtime = mock.Mock()
        host._ensure_server = mock.Mock()
        host._confirm_pending_connection_registry_activation = mock.Mock()
        calls = mock.Mock()
        calls.attach_mock(host._prepare_connection_registry_runtime, "select")
        calls.attach_mock(host._ensure_server, "start")
        calls.attach_mock(
            host._confirm_pending_connection_registry_activation, "confirm"
        )

        host._prepare_server()

        self.assertEqual(
            calls.mock_calls,
            [mock.call.select(), mock.call.start(), mock.call.confirm()],
        )
        host.startup_ready.set.assert_called_once()

    def test_pending_activation_is_not_confirmed_when_server_sync_is_invalid(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.state_root = Path("C:/state")
        host.connection_registry_digest = "sha256:" + "1" * 64
        host.connection_registry_mutations = mock.Mock()
        host._runtime_expected_workspace_id = mock.Mock(return_value=WORKSPACE_ID)
        host._server_sync_matches_expected = mock.Mock(return_value=False)
        pending = types.SimpleNamespace(activation_id=PROFILE_ID)

        with mock.patch.object(
            MODULE, "pending_activation_for_registry", return_value=pending
        ):
            with self.assertRaisesRegex(RuntimeError, "remains pending"):
                host._confirm_pending_connection_registry_activation()

        host.connection_registry_mutations.confirm.assert_not_called()

    def test_pending_activation_confirms_only_after_exact_in_sync_server(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.state_root = Path("C:/state")
        host.connection_registry_digest = "sha256:" + "1" * 64
        host.connection_registry_mutations = mock.Mock()
        host._runtime_expected_workspace_id = mock.Mock(return_value=WORKSPACE_ID)
        host._server_sync_matches_expected = mock.Mock(return_value=True)
        pending = types.SimpleNamespace(activation_id=PROFILE_ID)

        with mock.patch.object(
            MODULE, "pending_activation_for_registry", return_value=pending
        ):
            host._confirm_pending_connection_registry_activation()

        host.connection_registry_mutations.confirm.assert_called_once_with(
            PROFILE_ID,
            expected_registry_digest="sha256:" + "1" * 64,
        )

    def test_server_sync_requires_exact_in_sync_workspace_identity(self) -> None:
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.workstack_url = "http://127.0.0.1:8765/"

        def response(payload: dict[str, object]) -> mock.MagicMock:
            result = mock.MagicMock()
            result.status = 200
            result.read.return_value = json.dumps(payload).encode("utf-8")
            result.__enter__.return_value = result
            return result

        cases = (
            ({"data": {"state": "invalid", "workspace_id": WORKSPACE_ID,
                        "candidate_workspace_id": "22222222-2222-4222-8222-222222222222"}}, False),
            ({"data": {"state": "in-sync", "workspace_id": WORKSPACE_ID,
                        "candidate_workspace_id": "22222222-2222-4222-8222-222222222222"}}, False),
            ({"data": {"state": "in-sync", "workspace_id": WORKSPACE_ID,
                        "candidate_workspace_id": WORKSPACE_ID}}, True),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload), mock.patch.object(
                MODULE.urllib.request, "urlopen", return_value=response(payload)
            ):
                self.assertEqual(
                    host._server_sync_matches_expected(WORKSPACE_ID), expected
                )

    def test_local_active_profile_overrides_data_and_backup_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = bare_host(root)
            selection = MODULE.LocalStartupSelection(
                profile_id=PROFILE_ID,
                label="Local",
                expected_workspace_id=WORKSPACE_ID,
                data_dir=root / "workspace-a",
                backup_dir=root / "workspace-backups" / PROFILE_ID,
                live_updates=True,
            )
            host._configured_url = mock.Mock(return_value="http://127.0.0.1:8765/")
            registry = mock.sentinel.registry
            with mock.patch.object(
                MODULE, "ensure_connection_registry", return_value=registry
            ) as ensure, mock.patch.object(
                MODULE, "select_active_profile_for_startup", return_value=selection
            ), mock.patch.object(
                MODULE, "registry_digest", return_value="sha256:" + "1" * 64
            ), mock.patch.object(
                MODULE,
                "current_registry_snapshot",
                return_value=(registry, "sha256:" + "1" * 64),
            ), mock.patch.object(MODULE, "export_active_legacy_mirror"):
                host._prepare_connection_registry_runtime()

            ensure.assert_called_once()
            self.assertIs(host.local_startup_selection, selection)
            self.assertIsNone(host.remote_profile)
            self.assertEqual(host.active_connection_draft, {"storage_mode": "local"})
            host._configured_url.assert_called_once()

    def test_remote_active_profile_drives_tunnel_fields_and_runtime_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = bare_host(root)
            selection = MODULE.SshStartupSelection(
                profile_id=PROFILE_ID,
                label="Remote",
                expected_workspace_id=WORKSPACE_ID,
                ssh_host_alias="work-linux",
                remote_app_dir="/srv/workstack/app",
                remote_data_dir="/srv/workstack/ssot",
                preferred_forward_port=18765,
                remote_port=8765,
                live_updates=True,
            )
            runtime = MODULE.RemoteConnectionProfile(
                "work-linux", "/srv/workstack/app", "/srv/workstack/ssot",
                29123, WORKSPACE_ID, 8765,
            )
            registry = mock.sentinel.registry
            with mock.patch.object(
                MODULE, "ensure_connection_registry", return_value=registry
            ), mock.patch.object(
                MODULE, "select_active_profile_for_startup", return_value=selection
            ), mock.patch.object(
                MODULE, "profile_with_runtime_forward_port", return_value=runtime
            ), mock.patch.object(
                MODULE, "registry_digest", return_value="sha256:" + "1" * 64
            ), mock.patch.object(
                MODULE,
                "current_registry_snapshot",
                return_value=(registry, "sha256:" + "1" * 64),
            ), mock.patch.object(MODULE, "export_active_legacy_mirror"):
                host._prepare_connection_registry_runtime()

            self.assertIsNone(host.local_startup_selection)
            self.assertIs(host.remote_profile, runtime)
            self.assertEqual(host.workstack_url, "http://127.0.0.1:29123/")
            self.assertEqual(host.active_connection_draft["workspace_id"], WORKSPACE_ID)

    def test_local_runtime_config_uses_profile_scoped_paths_without_rewriting_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(
                '{"port":8765,"data_dir":"C:/legacy","backup_dir":"C:/legacy-backup","backup_retention":7}',
                encoding="utf-8",
            )
            original = config_path.read_bytes()
            host = object.__new__(MODULE.WorkStackDesktopHost)
            host.state_root = root
            host.install_root = root
            host.local_startup_selection = MODULE.LocalStartupSelection(
                PROFILE_ID, "Local", WORKSPACE_ID, root / "ssot", root / "backups", True
            )

            config, loaded_path = host._local_runtime_config()

            self.assertEqual(loaded_path, config_path)
            self.assertEqual(config["data_dir"], str(root / "ssot"))
            self.assertEqual(config["backup_dir"], str(root / "backups"))
            self.assertEqual(config_path.read_bytes(), original)


REGISTRY_AUTHORITY_NAMES = (
    STARTUP.REGISTRY_FILE,
    STARTUP.MIGRATION_INTENT_FILE,
    STARTUP.MIGRATION_RECEIPT_FILE,
    STARTUP.LEGACY_BACKUP_FILE,
    STARTUP.LEGACY_ABSENT_MARKER,
)


class DesktopFreshInstallBootstrapTest(unittest.TestCase):
    """A wholly fresh installation gets its first local Store before the registry binds it.

    Everything below the host is real: ``_local_runtime_config``,
    ``_prepare_connection_registry_runtime``, ``ensure_connection_registry``,
    ``select_active_profile_for_startup`` and the legacy mirror export.  Only the
    bundled runtime is replaced, by an in-process stand-in that runs the real
    ``workstack.cli`` maintenance entry with the exact argv the host passes, so
    the Store on disk is the product's own.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = mock.patch.dict(
            os.environ, {"WORK_STACK_RUNTIME": str(self.root / "runtime")}
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.bootstrap_calls: list[list[str]] = []
        self.order: list[str] = []
        self.build("default")

    def build(self, tag: str) -> None:
        base = self.root / tag
        self.state_root = base / "state"
        self.state_root.mkdir(parents=True)
        self.install = base / "install"
        (self.install / "runtime").mkdir(parents=True)
        (self.install / "runtime" / "python.exe").write_bytes(b"")
        (self.install / "run_work_stack.py").write_bytes(b"")
        self.data_dir = base / "data"
        self.backup_dir = base / "backups"
        self.write_config(self.data_dir)
        self.bootstrap_calls.clear()
        self.order.clear()

    def write_config(self, data_dir: Path) -> None:
        (self.state_root / "config.json").write_text(
            json.dumps(
                {
                    "port": 8765,
                    "data_dir": str(data_dir),
                    "backup_dir": str(self.backup_dir),
                    "backup_retention": 7,
                }
            ),
            encoding="utf-8",
        )

    def host(self):
        host = object.__new__(MODULE.WorkStackDesktopHost)
        host.state_root = self.state_root
        host.install_root = self.install
        host.options = argparse.Namespace(url=None)
        host.local_startup_selection = None
        host.connection_registry_startup_enabled = True
        host._origin = mock.Mock(side_effect=lambda url: url.rstrip("/"))
        host._trace = mock.Mock()
        host._configured_url = mock.Mock(return_value="http://127.0.0.1:8765/")
        host._read_registry_remote_identity = mock.Mock(
            side_effect=AssertionError("local bootstrap must not read a remote identity")
        )
        return host

    def bundled_runtime(self, *, stderr: bytes = b"", returncode: int | None = None):
        """Stand in for ``runtime/python.exe run_work_stack.py`` without a process."""

        def run(argv, **kwargs):
            self.bootstrap_calls.append([str(part) for part in argv])
            self.order.append("bootstrap")
            self.assertEqual(
                argv[:2],
                [
                    str(self.install / "runtime" / "python.exe"),
                    str(self.install / "run_work_stack.py"),
                ],
            )
            self.assertEqual(
                argv[2:],
                ["--data-dir", str(self.data_dir.resolve()), "maintenance", "initialize"],
            )
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["check"], False)
            self.assertEqual(
                kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            if returncode is not None:
                kwargs["stderr"].write(stderr)
                return subprocess.CompletedProcess(argv, returncode)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = cli.main(list(argv[2:]))
            kwargs["stdout"].write(captured.getvalue().encode("utf-8"))
            return subprocess.CompletedProcess(argv, code)

        return mock.patch.object(MODULE.subprocess, "run", side_effect=run)

    def forbidden_runtime(self):
        return mock.patch.object(
            MODULE.subprocess,
            "run",
            side_effect=AssertionError("the bundled runtime must not be started"),
        )

    def registry_files(self) -> list[str]:
        return sorted(
            name for name in REGISTRY_AUTHORITY_NAMES if (self.state_root / name).exists()
        )

    def test_fresh_install_creates_the_first_workspace_before_registry_migration(self) -> None:
        for case in ("absent", "empty"):
            with self.subTest(data_dir=case):
                self.build(case)
                if case == "empty":
                    self.data_dir.mkdir()
                host = self.host()

                with self.bundled_runtime():
                    try:
                        host._prepare_connection_registry_runtime()
                    except RuntimeError as error:
                        self.fail(
                            "fresh installation could not start: {}; bundled runtime "
                            "invocations: {}".format(error, self.bootstrap_calls)
                        )

                self.assertEqual(len(self.bootstrap_calls), 1)
                for name in DEFAULTS:
                    self.assertTrue((self.data_dir / name).is_file(), name)
                workspace_id = json.loads(
                    (self.data_dir / "workspace.json").read_text(encoding="utf-8")
                )["id"]
                parsed = uuid.UUID(workspace_id)
                self.assertEqual(str(parsed), workspace_id)
                self.assertNotEqual(parsed.int, 0)
                self.assertEqual(
                    self.registry_files(),
                    sorted(
                        (
                            STARTUP.REGISTRY_FILE,
                            STARTUP.MIGRATION_INTENT_FILE,
                            STARTUP.MIGRATION_RECEIPT_FILE,
                            STARTUP.LEGACY_ABSENT_MARKER,
                        )
                    ),
                )
                registry = STARTUP.load_connection_registry(self.state_root)
                self.assertIsNotNone(registry)
                self.assertEqual(len(registry.profiles), 1)
                profile = registry.profiles[0]
                self.assertIsInstance(profile, STARTUP.LocalConnectionProfile)
                self.assertEqual(profile.expected_workspace_id, workspace_id)
                self.assertEqual(registry.active_profile_id, profile.profile_id)
                self.assertEqual(
                    host.local_startup_selection.data_dir, self.data_dir.resolve()
                )
                self.assertEqual(host.active_connection_draft, {"storage_mode": "local"})
                self.assertIsNone(host.remote_profile)
                receipt = json.loads(
                    (self.state_root / "logs" / "initialize.out.log").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(receipt["workspace_id"], workspace_id)
                self.assertFalse(self.backup_dir.exists())

    def test_prepare_server_orders_bootstrap_before_migration_then_server(self) -> None:
        host = self.host()
        host.startup_error = None
        host.startup_ready = mock.Mock()
        host._ensure_server = mock.Mock(side_effect=lambda: self.order.append("start"))
        host._confirm_pending_connection_registry_activation = mock.Mock(
            side_effect=lambda: self.order.append("confirm")
        )
        real_ensure = MODULE.ensure_connection_registry

        def migrate(*args, **kwargs):
            self.order.append("migrate")
            return real_ensure(*args, **kwargs)

        with self.bundled_runtime(), mock.patch.object(
            MODULE, "ensure_connection_registry", side_effect=migrate
        ):
            host._prepare_server()

        if host.startup_error is not None:
            self.fail(f"startup failed: {host.startup_error!r}; order={self.order}")
        self.assertEqual(self.order, ["bootstrap", "migrate", "start", "confirm"])
        host.startup_ready.set.assert_called_once()

    def test_bootstrap_failure_fails_closed_without_registry_or_store_writes(self) -> None:
        for case in ("absent", "empty"):
            with self.subTest(data_dir=case):
                self.build(case)
                if case == "empty":
                    self.data_dir.mkdir()
                host = self.host()

                with self.bundled_runtime(stderr=b"error: simulated refusal\n", returncode=2):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "could not create its first workspace: error: simulated refusal",
                    ):
                        host._prepare_connection_registry_runtime()

                self.assertEqual(len(self.bootstrap_calls), 1)
                self.assertEqual(self.registry_files(), [])
                if case == "empty":
                    self.assertEqual(list(self.data_dir.iterdir()), [])
                else:
                    self.assertFalse(self.data_dir.exists())
                self.assertEqual(
                    (self.state_root / "logs" / "initialize.err.log").read_bytes(),
                    b"error: simulated refusal\n",
                )

    def test_incomplete_installation_is_reported_before_any_subprocess(self) -> None:
        (self.install / "runtime" / "python.exe").unlink()
        host = self.host()
        before = sorted(path.name for path in self.state_root.iterdir())

        with self.forbidden_runtime():
            with self.assertRaisesRegex(RuntimeError, "installation is incomplete"):
                host._prepare_connection_registry_runtime()

        self.assertEqual(sorted(path.name for path in self.state_root.iterdir()), before)
        self.assertFalse(self.data_dir.exists())

    def test_existing_complete_store_is_migrated_without_bootstrap(self) -> None:
        readiness = Store(self.data_dir).initialize()
        original = (self.data_dir / "workspace.json").read_bytes()
        host = self.host()

        with self.forbidden_runtime():
            host._prepare_connection_registry_runtime()

        registry = STARTUP.load_connection_registry(self.state_root)
        self.assertEqual(
            registry.profiles[0].expected_workspace_id, readiness.workspace_uid
        )
        self.assertEqual((self.data_dir / "workspace.json").read_bytes(), original)
        self.assertEqual(host.local_startup_selection.data_dir, self.data_dir.resolve())

    def test_partial_store_is_not_repaired_and_keeps_the_fail_closed_error(self) -> None:
        eight = sorted(set(DEFAULTS) - {"store-meta.json"})
        cases = (
            (
                "workspace-only",
                {"workspace.json": json.dumps({"version": 2, "id": WORKSPACE_ID, "name": "x"})},
            ),
            ("journal-only", {".workstack-journal.json": "{}"}),
            ("eight-without-store-meta", {name: "{}" for name in eight}),
            ("desktop-ini", {"desktop.ini": "[.ShellClassInfo]"}),
        )
        for name, contents in cases:
            with self.subTest(case=name):
                self.build(name)
                self.data_dir.mkdir()
                for filename, text in contents.items():
                    (self.data_dir / filename).write_text(text, encoding="utf-8")
                before = {path.name: path.read_bytes() for path in self.data_dir.iterdir()}
                host = self.host()

                with self.forbidden_runtime():
                    with self.assertRaisesRegex(RuntimeError, "required Store file"):
                        host._prepare_connection_registry_runtime()

                self.assertEqual(self.registry_files(), [])
                self.assertEqual(
                    {path.name: path.read_bytes() for path in self.data_dir.iterdir()},
                    before,
                )
                self.assertFalse((self.data_dir / "store-meta.json").exists())

    def test_existing_registry_suppresses_bootstrap_for_a_stale_empty_config_dir(self) -> None:
        selected = self.root / "default" / "selected"
        Store(selected).initialize()
        self.write_config(selected)
        with self.forbidden_runtime():
            self.host()._prepare_connection_registry_runtime()
        self.assertTrue((self.state_root / STARTUP.REGISTRY_FILE).is_file())

        stale = self.data_dir
        stale.mkdir()
        self.write_config(stale)
        host = self.host()

        with self.forbidden_runtime():
            host._prepare_connection_registry_runtime()

        self.assertEqual(host.local_startup_selection.data_dir, selected.resolve())
        self.assertEqual(list(stale.iterdir()), [])

    def test_legacy_ssh_draft_never_triggers_local_bootstrap(self) -> None:
        self.data_dir.mkdir()
        (self.state_root / STARTUP.LEGACY_CONNECTION_FILE).write_text(
            json.dumps(
                {
                    "storage_mode": "ssh-remote",
                    "ssh_host_alias": "work-linux",
                    "remote_app_dir": "/srv/workstack/app",
                    "remote_data_dir": "/srv/workstack/ssot",
                    "local_forward_port": 18765,
                    "workspace_id": WORKSPACE_ID,
                    "remote_port": 8765,
                }
            ),
            encoding="utf-8",
        )
        host = self.host()
        host._read_registry_remote_identity = mock.Mock(return_value=WORKSPACE_ID)
        runtime = MODULE.RemoteConnectionProfile(
            "work-linux", "/srv/workstack/app", "/srv/workstack/ssot",
            29123, WORKSPACE_ID, 8765,
        )

        with self.forbidden_runtime(), mock.patch.object(
            MODULE, "profile_with_runtime_forward_port", return_value=runtime
        ):
            host._prepare_connection_registry_runtime()

        self.assertIs(host.remote_profile, runtime)
        self.assertIsNone(host.local_startup_selection)
        self.assertEqual(list(self.data_dir.iterdir()), [])

    def test_legacy_local_singleton_draft_still_bootstraps_and_is_backed_up_once(self) -> None:
        self.data_dir.mkdir()
        draft = b'{"storage_mode": "local"}'
        (self.state_root / STARTUP.LEGACY_CONNECTION_FILE).write_bytes(draft)
        host = self.host()

        with self.bundled_runtime():
            host._prepare_connection_registry_runtime()

        self.assertEqual(len(self.bootstrap_calls), 1)
        self.assertEqual((self.state_root / STARTUP.LEGACY_BACKUP_FILE).read_bytes(), draft)
        receipt = json.loads(
            (self.state_root / STARTUP.MIGRATION_RECEIPT_FILE).read_text(encoding="utf-8")
        )
        self.assertTrue(receipt["legacy_existed"])
        self.assertTrue((self.data_dir / "workspace.json").is_file())
        self.assertEqual(host.local_startup_selection.data_dir, self.data_dir.resolve())


if __name__ == "__main__":
    unittest.main()

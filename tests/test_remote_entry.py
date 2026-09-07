"""Wave 1 R1: checked-in remote_entry probe/serve contract. No live SSH."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_entry as ENTRY  # noqa: E402


WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
RUNTIME_SESSION_TOKEN = "r5pending-token-not-enforced-01"


def _valid_probe_payload() -> dict[str, object]:
    return {
        "workspace_id": WORKSPACE_ID,
        "product_version": "1.0.7",
        "protocol_version": 1,
    }


def _write_probe_fixture(root: Path) -> tuple[Path, Path]:
    app = root / "app"
    data = root / "data"
    (app / "workstack").mkdir(parents=True)
    data.mkdir()
    (app / "workstack" / "__init__.py").write_text(
        '__version__ = "1.0.7"\nREMOTE_PROTOCOL_VERSION = 1\n',
        encoding="utf-8",
    )
    (data / "workspace.json").write_text(
        json.dumps({"id": WORKSPACE_ID, "name": "probe", "version": 2}),
        encoding="utf-8",
    )
    (data / "store-meta.json").write_text(
        json.dumps({"schema_version": 3}),
        encoding="utf-8",
    )
    return app, data


def _write_lookalike_module(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('malicious lookalike executed', encoding='utf-8')\n"
        "raise RuntimeError('malicious lookalike executed')\n",
        encoding="utf-8",
    )


def _write_adversarial_import_tree(root: Path) -> dict[str, Path]:
    markers = {
        "contract": root / "lookalike-contract-executed.txt",
        "workstack": root / "lookalike-workstack-executed.txt",
        "runner": root / "lookalike-runner-executed.txt",
    }
    _write_lookalike_module(root / "remote_command_contract.py", markers["contract"])
    _write_lookalike_module(root / "workstack" / "__init__.py", markers["workstack"])
    _write_lookalike_module(root / "run_work_stack.py", markers["runner"])
    return markers


def _write_serve_import_stub(app: Path) -> Path:
    (app / "workstack" / "cli.py").write_text(
        "def main() -> int:\n    return 0\n",
        encoding="utf-8",
    )
    (app / "run_work_stack.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "CHECKOUT_ROOT = Path(__file__).resolve(strict=True).parent\n"
        "sys.path.insert(0, str(CHECKOUT_ROOT))\n"
        "sys.dont_write_bytecode = True\n"
        "from workstack.cli import main\n"
        "import workstack.cli\n"
        "print(Path(workstack.cli.__file__).resolve())\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    return app / "workstack" / "cli.py"


def _run_isolated_entry(
    cwd: Path, argv: list[str], *, pythonpath: Path
) -> subprocess.CompletedProcess[bytes]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(pythonpath)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-I", "-B", str(SHELL / "remote_entry.py"), *argv],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


class RemoteEntryTest(unittest.TestCase):
    def test_probe_emits_exact_schema_bounded_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            home_marker = Path.home() / ".workstack-r1-remote-entry-should-not-exist"
            self.assertFalse(home_marker.exists())
            payload = ENTRY.run_probe(app, data)
            self.assertFalse(home_marker.exists())
        self.assertTrue(payload.endswith(b"\n"))
        self.assertLessEqual(len(payload), ENTRY.MAX_STDOUT_BYTES)
        decoded = json.loads(payload.decode("utf-8"))
        self.assertEqual(
            decoded,
            {
                "workspace_id": WORKSPACE_ID,
                "product_version": "1.0.7",
                "protocol_version": 1,
            },
        )
        self.assertEqual(set(decoded), set(ENTRY.PROBE_KEYS))

    def test_extra_or_malformed_probe_output_is_refused(self) -> None:
        valid = {
            "workspace_id": WORKSPACE_ID,
            "product_version": "1.0.7",
            "protocol_version": 1,
        }
        encoded = ENTRY.encode_probe_stdout(valid)
        self.assertEqual(ENTRY.refuse_extra_stdout(encoded), valid)
        with self.assertRaises(ENTRY.EntryError):
            ENTRY.refuse_extra_stdout(encoded + b'{"extra":1}\n')
        with self.assertRaises(ENTRY.EntryError):
            ENTRY.refuse_extra_stdout(b"not-json\n")
        with self.assertRaises(ENTRY.EntryError):
            ENTRY.encode_probe_stdout({**valid, "extra": True})

    def test_missing_identity_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ENTRY.EntryError):
                ENTRY.run_probe(root / "missing-app", root / "missing-data")

    def test_probe_argv_requires_app_and_data(self) -> None:
        parsed = ENTRY.parse_remote_entry_argv(
            ["probe", "--app-dir", "/srv/workstack/app", "--data-dir", "/srv/workstack/ssot"]
        )
        self.assertEqual(parsed.command, "probe")
        with self.assertRaises(ENTRY.EntryError) as caught:
            ENTRY.parse_remote_entry_argv(["probe"])
        self.assertIn("REMOTE_PROTOCOL_INVALID", str(caught.exception))

    def test_serve_argv_requires_fixed_flags_and_does_not_exec_without_runner(self) -> None:
        parsed = ENTRY.parse_remote_entry_argv(
            [
                "serve",
                "--app-dir",
                "/srv/workstack/app",
                "--data-dir",
                "/srv/workstack/ssot",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
                "--public-port",
                "18765",
                "--session-token",
                RUNTIME_SESSION_TOKEN,
                "--exit-with-parent",
            ]
        )
        self.assertEqual(parsed.command, "serve")
        self.assertTrue(parsed.exit_with_parent)
        self.assertEqual(parsed.session_token, RUNTIME_SESSION_TOKEN)
        with tempfile.TemporaryDirectory() as directory:
            parsed.app_dir = directory
            with self.assertRaises(ENTRY.EntryError) as caught:
                ENTRY.run_serve(parsed)
            self.assertIn("REMOTE_APP_MISMATCH", str(caught.exception))

    def test_main_probe_writes_stdout_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            from io import BytesIO, StringIO
            from unittest import mock

            stdout = BytesIO()
            stderr = StringIO()
            with mock.patch.object(ENTRY.sys, "stdout", mock.Mock(buffer=stdout)):
                with mock.patch.object(ENTRY.sys, "stderr", stderr):
                    code = ENTRY.main(
                        ["probe", "--app-dir", str(app), "--data-dir", str(data)]
                    )
            self.assertEqual(code, 0)
            self.assertEqual(stderr.getvalue(), "")
            decoded = json.loads(stdout.getvalue().decode("utf-8"))
            self.assertEqual(decoded["workspace_id"], WORKSPACE_ID)

    def test_oversized_sparse_metadata_is_rejected_before_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            target = data / "workspace.json"
            with target.open("wb") as handle:
                handle.seek(ENTRY.MAX_METADATA_FILE_BYTES + 1)
                handle.write(b"}")
            with self.assertRaisesRegex(ENTRY.EntryError, "too large"):
                ENTRY.run_probe(app, data)

    def test_non_literal_version_assignment_is_stable_app_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            (app / "workstack" / "__init__.py").write_text(
                '__version__ = __import__("os").popen("echo pwned").read()\n'
                "REMOTE_PROTOCOL_VERSION = 1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ENTRY.EntryError, "REMOTE_APP_MISMATCH"):
                ENTRY.run_probe(app, data)

    def test_invalid_constant_type_is_stable_app_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            (app / "workstack" / "__init__.py").write_text(
                "__version__ = 107\nREMOTE_PROTOCOL_VERSION = 1\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ENTRY.EntryError, "REMOTE_APP_MISMATCH"):
                ENTRY.run_probe(app, data)

    def test_cr_only_extra_probe_output_is_refused(self) -> None:
        encoded = ENTRY.encode_probe_stdout(_valid_probe_payload())
        with self.assertRaisesRegex(ENTRY.EntryError, "extra probe output"):
            ENTRY.refuse_extra_stdout(encoded[:-1] + b"\r")
        with self.assertRaisesRegex(ENTRY.EntryError, "extra probe output"):
            ENTRY.refuse_extra_stdout(encoded.rstrip(b"\n") + b"\r\n")

    def test_malformed_argv_is_bounded_diagnostic_without_traceback(self) -> None:
        stderr = StringIO()
        with mock.patch.object(ENTRY.sys, "stderr", stderr):
            code = ENTRY.main(["probe"])
        self.assertEqual(code, 2)
        text = stderr.getvalue()
        self.assertIn("REMOTE_PROTOCOL_INVALID", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("usage:", text.lower())

    def test_all_zero_session_token_is_rejected_and_not_forwarded(self) -> None:
        argv = [
            "serve",
            "--app-dir",
            "/srv/workstack/app",
            "--data-dir",
            "/srv/workstack/ssot",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--public-port",
            "18765",
            "--session-token",
            "0" * 32,
        ]
        with self.assertRaisesRegex(ENTRY.EntryError, "REMOTE_SESSION_TOKEN_INVALID"):
            ENTRY.parse_remote_entry_argv(argv)

    def test_serve_does_not_forward_session_token_to_exec_argv(self) -> None:
        argv = [
            "serve",
            "--app-dir",
            "/srv/workstack/app",
            "--data-dir",
            "/srv/workstack/ssot",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--public-port",
            "18765",
            "--session-token",
            RUNTIME_SESSION_TOKEN,
            "--exit-with-parent",
        ]
        parsed = ENTRY.parse_remote_entry_argv(argv)
        with tempfile.TemporaryDirectory() as directory:
            app, data = _write_probe_fixture(Path(directory))
            (app / "run_work_stack.py").write_text("print(1)\n", encoding="utf-8")
            parsed.app_dir = str(app)
            parsed.data_dir = str(data)
            with mock.patch.object(ENTRY.os, "execv") as execv:
                ENTRY.run_serve(parsed)
            forwarded = execv.call_args.args[1]
            owner = (data / ENTRY.OWNER_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("--session-token", forwarded)
        self.assertNotIn(RUNTIME_SESSION_TOKEN, forwarded)
        self.assertNotIn(RUNTIME_SESSION_TOKEN, owner)
        self.assertEqual(ENTRY.R5_OWNERSHIP_NOT_IMPLEMENTED, "R5_OWNERSHIP_NOT_IMPLEMENTED")
        self.assertIn("stop-owned", ENTRY.__doc__ or "")

    def test_isolated_relative_argv_loads_checked_in_sibling_not_modulenotfound(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "desktop/python-webview-shell/remote_entry.py",
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        stderr = result.stderr.decode("utf-8", "replace")
        self.assertNotIn("ModuleNotFoundError", stderr)
        self.assertNotIn("No module named 'remote_command_contract'", stderr)
        self.assertEqual(result.returncode, 2)
        self.assertIn("REMOTE_PROTOCOL_INVALID", stderr)

    def test_isolated_probe_uses_trusted_sibling_not_cwd_or_pythonpath_lookalike(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            fixture = root / "fixture"
            outside.mkdir()
            markers = _write_adversarial_import_tree(outside)
            app, data = _write_probe_fixture(fixture)
            result = _run_isolated_entry(
                outside,
                ["probe", "--app-dir", str(app), "--data-dir", str(data)],
                pythonpath=outside,
            )
            stderr = result.stderr.decode("utf-8", "replace")
            self.assertEqual(result.returncode, 0, stderr)
            self.assertEqual(result.stderr, b"")
            self.assertTrue(result.stdout.endswith(b"\n"))
            self.assertEqual(
                json.loads(result.stdout.decode("utf-8")),
                {
                    "workspace_id": WORKSPACE_ID,
                    "product_version": "1.0.7",
                    "protocol_version": 1,
                },
            )
            for marker in markers.values():
                self.assertFalse(marker.exists(), marker)

    def test_isolated_serve_imports_fixture_product_not_lookalike_without_live_serve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            fixture = root / "fixture"
            outside.mkdir()
            markers = _write_adversarial_import_tree(outside)
            app, data = _write_probe_fixture(fixture)
            cli_path = _write_serve_import_stub(app)
            result = _run_isolated_entry(
                outside,
                [
                    "serve",
                    "--app-dir",
                    str(app),
                    "--data-dir",
                    str(data),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8765",
                    "--public-port",
                    "18765",
                    "--session-token",
                    RUNTIME_SESSION_TOKEN,
                ],
                pythonpath=outside,
            )
            stderr = result.stderr.decode("utf-8", "replace")
            self.assertEqual(result.returncode, 0, stderr)
            self.assertEqual(result.stderr, b"")
            self.assertEqual(
                result.stdout.decode("utf-8").splitlines(),
                [str(cli_path.resolve())],
            )
            owner = (data / ENTRY.OWNER_FILENAME).read_text(encoding="utf-8")
            self.assertNotIn(RUNTIME_SESSION_TOKEN, owner)
            for marker in markers.values():
                self.assertFalse(marker.exists(), marker)


if __name__ == "__main__":
    unittest.main()

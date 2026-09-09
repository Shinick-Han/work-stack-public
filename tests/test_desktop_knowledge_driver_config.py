"""Optional Windows owner ``knowledge_drivers_config`` launch-contract oracles."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
START = ROOT / "scripts" / "windows" / "Start-WorkStack.ps1"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import owner_launch_config as LAUNCH  # noqa: E402

DESKTOP_SPEC = importlib.util.spec_from_file_location(
    "workstack_desktop_knowledge_driver_config", SHELL / "workstack_desktop.py"
)
assert DESKTOP_SPEC is not None and DESKTOP_SPEC.loader is not None
DESKTOP = importlib.util.module_from_spec(DESKTOP_SPEC)
sys.modules[DESKTOP_SPEC.name] = DESKTOP
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    DESKTOP_SPEC.loader.exec_module(DESKTOP)

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
PYTHON = r"C:\install\runtime\python.exe"
ENTRY = r"C:\install\run_work_stack.py"
DATA = r"C:\data"
PORT = 8765
OLD_ARGV = [
    PYTHON,
    ENTRY,
    "--data-dir",
    DATA,
    "graph",
    "serve",
    "--host",
    "127.0.0.1",
    "--port",
    str(PORT),
]
ABSENT_CONFIG = {"port": PORT, "data_dir": DATA, "backup_dir": r"C:\backups"}
REFUSALS = (
    None,
    "",
    "   ",
    12,
    True,
    ["C:\\drivers.json"],
    {"path": r"C:\drivers.json"},
    r"drivers.json",
    r".\drivers.json",
    r"..\drivers.json",
    r"C:drivers.json",
    r"C:",
    r"\drivers.json",
    "/drivers.json",
    r"%LOCALAPPDATA%\drivers.json",
    "C:\\drivers\n.json",
    "C:\\drivers\x00.json",
)
PS_FUNCTIONS = (
    "ConvertTo-WindowsCommandLineArgument",
    "Test-DriveAbsoluteOwnerConfigPath",
    "Test-UncOwnerConfigBody",
    "Test-WindowsAbsoluteOwnerConfigPath",
    "Resolve-KnowledgeDriversConfigArguments",
)
_PWSH_UTF8 = (
    "$null = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
    "$OutputEncoding = [Console]::OutputEncoding\n"
)


def _powershell() -> str | None:
    for name in ("powershell.exe", "pwsh.exe", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _with_utf8_stdio(body: str) -> str:
    text = body.lstrip("\ufeff")
    if not text.lstrip().lower().startswith("param"):
        return _PWSH_UTF8 + text
    start = text.lower().find("param")
    paren = text.find("(", start)
    if paren < 0:
        return _PWSH_UTF8 + text
    depth = 0
    for index, char in enumerate(text[paren:], start=paren):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[: index + 1] + "\n" + _PWSH_UTF8 + text[index + 1 :]
    return _PWSH_UTF8 + text


def _extract_script() -> str:
    return r"""
param([string]$Path, [string]$Names)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
if ($errors) { throw (($errors | ForEach-Object { $_.Message }) -join '; ') }
$wanted = $Names -split ','
$found = @{}; $seen = @{}
foreach ($function in $ast.FindAll({
    param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true)) {
    if ($wanted -contains $function.Name) {
        if ($seen.ContainsKey($function.Name)) { $seen[$function.Name] = $seen[$function.Name] + 1 }
        else { $seen[$function.Name] = 1 }
        $found[$function.Name] = $function.Extent.Text
    }
}
foreach ($name in $wanted) {
    if (-not $found.ContainsKey($name)) { throw "required function is missing: $name" }
    if ($seen[$name] -ne 1) { throw "required function is not unique: $name" }
}
($wanted | ForEach-Object { $found[$_] }) -join "`n"
"""


def _run_powershell(body: str, arguments: list[str] | None = None) -> subprocess.CompletedProcess[bytes]:
    host = _powershell()
    if host is None:
        raise unittest.SkipTest("PowerShell is not available")
    with tempfile.TemporaryDirectory(prefix="ws-kd-ps-") as directory:
        script = Path(directory) / "probe.ps1"
        script.write_text(_with_utf8_stdio(body), encoding="utf-8")
        return subprocess.run(
            [host, "-NoProfile", "-NonInteractive", "-File", str(script), *(arguments or [])],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )


def _extract_ps_functions() -> str:
    completed = _run_powershell(_extract_script(), [str(START), ",".join(PS_FUNCTIONS)])
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))
    return completed.stdout.decode("utf-8")


def _command_line_to_argv(command_line: str) -> list[str]:
    import ctypes
    from ctypes import wintypes

    func = ctypes.windll.shell32.CommandLineToArgvW
    func.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    func.restype = ctypes.POINTER(wintypes.LPWSTR)
    argc = ctypes.c_int()
    argv = func(command_line, ctypes.byref(argc))
    if not argv:
        raise OSError("CommandLineToArgvW failed")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _serve_argv(config: dict[str, object]) -> list[str]:
    return LAUNCH.graph_serve_argv(PYTHON, ENTRY, DATA, PORT, config)


class HelperContractTest(unittest.TestCase):
    def test_missing_field_keeps_the_old_argv_exact(self) -> None:
        self.assertEqual(_serve_argv(ABSENT_CONFIG), OLD_ARGV)
        self.assertEqual(_serve_argv({**ABSENT_CONFIG, "extra": 1}), OLD_ARGV)

    def test_absolute_path_is_one_unmodified_argv_pair(self) -> None:
        path = r"C:\Users\예\My Drivers\file\"name.json"
        argv = _serve_argv({**ABSENT_CONFIG, "knowledge_drivers_config": path})
        self.assertEqual(argv[:-2], OLD_ARGV)
        self.assertEqual(argv[-2:], ["--knowledge-drivers-config", path])
        self.assertEqual(len(argv), len(OLD_ARGV) + 2)

    def test_unc_and_forward_slash_drive_paths_are_forwarded(self) -> None:
        unc = r"\\server\share\drivers.json"
        slash = "D:/drivers/config.json"
        env_literal = r"C:\%USERPROFILE%\drivers.json"
        self.assertEqual(
            _serve_argv({**ABSENT_CONFIG, "knowledge_drivers_config": unc})[-1],
            unc,
        )
        self.assertEqual(
            _serve_argv({**ABSENT_CONFIG, "knowledge_drivers_config": slash})[-1],
            slash,
        )
        forwarded = _serve_argv({**ABSENT_CONFIG, "knowledge_drivers_config": env_literal})[-1]
        self.assertEqual(forwarded, env_literal)
        self.assertIn("%USERPROFILE%", forwarded)
        self.assertNotEqual(forwarded, os.path.expandvars(env_literal))

    def test_bad_fields_are_content_free_refusals(self) -> None:
        for value in REFUSALS:
            with self.subTest(value=value):
                with self.assertRaises(LAUNCH.OwnerLaunchConfigError) as caught:
                    _serve_argv({**ABSENT_CONFIG, "knowledge_drivers_config": value})
                message = str(caught.exception)
                self.assertEqual(message, LAUNCH.REFUSAL)
                if isinstance(value, str) and value:
                    self.assertNotIn(value, message)


class DesktopEnsureServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ws-kd-host-")
        self.root = Path(self.temp.name)
        self.install = self.root / "install"
        (self.install / "runtime").mkdir(parents=True)
        (self.install / "runtime" / "python.exe").write_bytes(b"")
        (self.install / "run_work_stack.py").write_bytes(b"")
        self.state = self.root / "state"
        self.state.mkdir()
        self.data = self.root / "data"
        self.backups = self.root / "backups"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_config(self, extra: dict[str, object] | None = None) -> dict[str, object]:
        config: dict[str, object] = {
            "port": PORT,
            "data_dir": str(self.data),
            "backup_dir": str(self.backups),
            "backup_retention": 7,
        }
        if extra:
            config.update(extra)
        (self.state / "config.json").write_text(json.dumps(config), encoding="utf-8")
        return config

    def host(self):
        host = object.__new__(DESKTOP.WorkStackDesktopHost)
        host.remote_profile = None
        host.state_root = self.state
        host.install_root = self.install
        host.workstack_url = f"http://127.0.0.1:{PORT}/"
        host._trace = mock.Mock()
        host.server_process = None
        host.server_started_by_host = False
        host.server_pid = None
        return host

    def test_missing_field_popen_argv_matches_old_contract(self) -> None:
        self.write_config()
        host = self.host()
        host._is_ready = mock.Mock(side_effect=[False, True])
        host._loopback_port_listening = mock.Mock(return_value=False)
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 4242
        with (
            mock.patch.object(DESKTOP.subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(DESKTOP.subprocess, "run") as backup,
        ):
            host._ensure_server()
        backup.assert_not_called()
        argv = popen.call_args.args[0]
        expected = LAUNCH.graph_serve_argv(
            str(self.install / "runtime" / "python.exe"),
            str(self.install / "run_work_stack.py"),
            str(self.data.resolve()),
            PORT,
            self.write_config(),
        )
        self.assertEqual(argv, expected)
        self.assertEqual(argv[4:10], ["graph", "serve", "--host", "127.0.0.1", "--port", str(PORT)])
        self.assertNotIn("--knowledge-drivers-config", argv)
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["cwd"], self.install)

    def test_path_with_spaces_is_one_popen_argument(self) -> None:
        path = str(self.root / "My Drivers" / "예" / 'file"name.json')
        self.write_config({"knowledge_drivers_config": path})
        host = self.host()
        host._is_ready = mock.Mock(side_effect=[False, True])
        host._loopback_port_listening = mock.Mock(return_value=False)
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 7
        with mock.patch.object(DESKTOP.subprocess, "Popen", return_value=process) as popen:
            host._ensure_server()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-2:], ["--knowledge-drivers-config", path])
        self.assertEqual(argv.count(path), 1)

    def test_backup_argv_does_not_gain_the_driver_flag(self) -> None:
        path = str((self.root / "drivers.json").resolve())
        self.write_config({"knowledge_drivers_config": path})
        self.data.mkdir()
        (self.data / "workspace.json").write_text(
            json.dumps({"id": WORKSPACE_ID, "name": "W", "version": 2}),
            encoding="utf-8",
        )
        host = self.host()
        host._is_ready = mock.Mock(side_effect=[False, True])
        host._loopback_port_listening = mock.Mock(return_value=False)
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 9
        backup_calls: list[list[str]] = []

        def run(argv, **kwargs):
            backup_calls.append([str(part) for part in argv])
            return subprocess.CompletedProcess(argv, 0)

        with (
            mock.patch.object(DESKTOP.subprocess, "run", side_effect=run),
            mock.patch.object(DESKTOP.subprocess, "Popen", return_value=process) as popen,
        ):
            host._ensure_server()
        self.assertEqual(len(backup_calls), 1)
        self.assertNotIn("--knowledge-drivers-config", backup_calls[0])
        self.assertEqual(popen.call_args.args[0][-2:], ["--knowledge-drivers-config", path])

    def test_bad_field_does_not_backup_mkdir_or_popen(self) -> None:
        self.write_config({"knowledge_drivers_config": r".\relative.json"})
        host = self.host()
        host._is_ready = mock.Mock(return_value=False)
        host._loopback_port_listening = mock.Mock(return_value=False)
        with (
            mock.patch.object(DESKTOP.subprocess, "Popen") as popen,
            mock.patch.object(DESKTOP.subprocess, "run") as backup,
            mock.patch.object(Path, "mkdir") as mkdir,
        ):
            with self.assertRaises(LAUNCH.OwnerLaunchConfigError) as caught:
                host._ensure_server()
        self.assertEqual(str(caught.exception), LAUNCH.REFUSAL)
        self.assertNotIn("relative.json", str(caught.exception))
        popen.assert_not_called()
        backup.assert_not_called()
        mkdir.assert_not_called()
        self.assertFalse(self.data.exists())
        self.assertFalse(self.backups.exists())
        self.assertFalse((self.state / "logs").exists())

    def test_owner_reuse_does_not_take_over_or_apply_settings(self) -> None:
        secret = r".\should-not-run.json"
        self.write_config({"knowledge_drivers_config": secret})
        host = self.host()
        host._is_ready = mock.Mock(return_value=True)
        host._read_local_workspace_identity = mock.Mock(return_value=WORKSPACE_ID)
        host._read_server_workspace_identity = mock.Mock(return_value=WORKSPACE_ID)
        host._select_available_local_port = mock.Mock()
        with (
            mock.patch.object(DESKTOP.subprocess, "Popen") as popen,
            mock.patch.object(DESKTOP, "graph_serve_argv", side_effect=AssertionError("reuse")) as serve,
        ):
            host._ensure_server()
        popen.assert_not_called()
        serve.assert_not_called()
        host._select_available_local_port.assert_not_called()
        self.assertFalse(self.data.exists())

    def test_host_does_not_add_a_webview_or_env_setter(self) -> None:
        source = (SHELL / "workstack_desktop.py").read_text(encoding="utf-8")
        self.assertNotIn("knowledge_drivers_config", source)
        self.assertNotIn("WORKSTACK_KNOWLEDGE_DRIVERS", source)
        self.assertIn("graph_serve_argv", source)


class StartWorkStackContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = START.read_text(encoding="utf-8-sig")

    def test_reuse_is_before_resolve_and_resolve_is_before_mkdir_and_start(self) -> None:
        ready = self.source.index("if (Test-WorkStackReady)")
        resolve = self.source.index("$knowledgeDriversArguments = @(Resolve-KnowledgeDriversConfigArguments")
        mkdir = self.source.index("New-Item -ItemType Directory -Force -Path $dataPath, $backupPath, $logPath")
        start = self.source.index("Start-Process -FilePath $pythonPath -ArgumentList $arguments")
        self.assertLess(ready, resolve)
        self.assertLess(resolve, mkdir)
        self.assertLess(mkdir, start)
        self.assertIn("-WindowStyle Hidden", self.source)
        self.assertIn("ConvertTo-WindowsCommandLineArgument ([string]$raw)", self.source)
        self.assertIn("$argumentTokens -join ' '", self.source)
        self.assertNotIn("ExpandEnvironmentVariables", self.source)
        self.assertNotIn("[Environment]::GetEnvironmentVariable", self.source)

    def test_extracted_quoting_keeps_spaces_nonascii_and_quote_as_one_argv(self) -> None:
        if os.name != "nt":
            self.skipTest("CommandLineToArgvW is a Windows parser")
        extents = _extract_ps_functions()
        path = r"C:\Users\예\My Drivers\file\"name.json"
        encoded = base64.b64encode(path.encode("utf-8")).decode("ascii")
        body = (
            extents
            + "\n$raw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($args[0]))\n"
            + "$config = [pscustomobject]@{ knowledge_drivers_config = $raw }\n"
            + "$tokens = Resolve-KnowledgeDriversConfigArguments -Config $config\n"
            + "Write-Output ($tokens -join ' ')\n"
        )
        completed = _run_powershell(body, [encoded])
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        joined = completed.stdout.decode("utf-8").strip()
        parsed = _command_line_to_argv("python.exe " + joined)
        self.assertEqual(parsed, ["python.exe", "--knowledge-drivers-config", path])

    def test_extracted_missing_field_adds_no_tokens(self) -> None:
        extents = _extract_ps_functions()
        body = (
            extents
            + "\n$config = [pscustomobject]@{ port = 8765 }\n"
            + "$tokens = @(Resolve-KnowledgeDriversConfigArguments -Config $config)\n"
            + "Write-Output $tokens.Count\n"
        )
        completed = _run_powershell(body)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertEqual(completed.stdout.decode("utf-8").strip(), "0")

    def test_script_refuses_a_bad_field_before_mkdir_or_start(self) -> None:
        host = _powershell()
        if host is None:
            self.skipTest("PowerShell is not available")
        with tempfile.TemporaryDirectory(prefix="ws-kd-start-") as directory:
            root = Path(directory)
            install = root / "install"
            (install / "runtime").mkdir(parents=True)
            (install / "runtime" / "python.exe").write_bytes(b"")
            (install / "run_work_stack.py").write_bytes(b"")
            state = root / "state"
            state.mkdir()
            data = root / "data"
            backups = root / "backups"
            port = _free_port()
            config = {
                "port": port,
                "data_dir": str(data),
                "backup_dir": str(backups),
                "backup_retention": 7,
                "knowledge_drivers_config": r".\relative.json",
            }
            (state / "config.json").write_text(json.dumps(config), encoding="utf-8")
            completed = subprocess.run(
                [
                    host,
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(START),
                    "-InstallRoot",
                    str(install),
                    "-StateRoot",
                    str(state),
                    "-NoBrowser",
                ],
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            combined = (
                completed.stdout.decode("utf-8", "replace")
                + completed.stderr.decode("utf-8", "replace")
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(LAUNCH.REFUSAL, combined)
            self.assertNotIn("relative.json", combined)
            self.assertFalse(data.exists())
            self.assertFalse(backups.exists())
            self.assertFalse((state / "logs").exists())

    def test_script_reuses_a_healthy_owner_without_applying_a_bad_field(self) -> None:
        host = _powershell()
        if host is None:
            self.skipTest("PowerShell is not available")
        port = _free_port()
        server = _start_health(port)
        _wait_listening(port)
        try:
            with tempfile.TemporaryDirectory(prefix="ws-kd-reuse-") as directory:
                root = Path(directory)
                install = root / "install"
                (install / "runtime").mkdir(parents=True)
                (install / "runtime" / "python.exe").write_bytes(b"")
                (install / "run_work_stack.py").write_bytes(b"")
                state = root / "state"
                state.mkdir()
                data = root / "data"
                backups = root / "backups"
                config = {
                    "port": port,
                    "data_dir": str(data),
                    "backup_dir": str(backups),
                    "backup_retention": 7,
                    "knowledge_drivers_config": r".\should-not-apply.json",
                }
                (state / "config.json").write_text(json.dumps(config), encoding="utf-8")
                completed = subprocess.run(
                    [
                        host,
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(START),
                        "-InstallRoot",
                        str(install),
                        "-StateRoot",
                        str(state),
                        "-NoBrowser",
                    ],
                    cwd=str(root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                )
                combined = (
                    completed.stdout.decode("utf-8", "replace")
                    + completed.stderr.decode("utf-8", "replace")
                )
                self.assertEqual(completed.returncode, 0, combined)
                self.assertIn("already running", combined)
                self.assertNotIn(LAUNCH.REFUSAL, combined)
                self.assertFalse(data.exists())
                self.assertFalse(backups.exists())
                self.assertFalse((state / "logs").exists())
        finally:
            server.shutdown()
            server.server_close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.bind(("127.0.0.1", 0))
        return int(client.getsockname()[1])


def _wait_listening(port: int) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"health fixture did not listen on 127.0.0.1:{port}")


def _start_health(port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/api/v1/health":
                self.send_error(404)
                return
            body = b'{"data":{"api_version":"v1","status":"ready"}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


if __name__ == "__main__":
    unittest.main()

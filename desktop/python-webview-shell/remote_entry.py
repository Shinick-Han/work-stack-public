"""Checked-in remote entry point for probe, serve, and stop-owned.

Runs on the remote interpreter named by the profile.  It never writes the
home directory and never composes a login-shell script.

Serve creates exclusive owner metadata that stores a token hash, never the
raw session token. stop-owned terminates only the matching live process.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Protocol, Sequence

# Python isolated mode (-I) omits the script directory from sys.path. Admit
# only the resolved directory containing this checked-in file so the sibling
# contract loads from __file__, never cwd, PYTHONPATH, or user site.
_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
sys.path.insert(0, _SHELL_DIR)

from remote_command_contract import (
    R5_OWNERSHIP_NOT_IMPLEMENTED,
    RemoteCommandError,
    require_session_token,
    token_hash,
)


PROBE_KEYS = ("workspace_id", "product_version", "protocol_version")
OWNER_KEYS = (
    "workspace_id",
    "data_dir_digest",
    "app_dir_digest",
    "pid",
    "start_identity",
    "release_id",
    "token_hash",
)
MAX_METADATA_FILE_BYTES = 1_048_576
MAX_STDOUT_BYTES = 4096
MAX_OWNER_BYTES = 4096
MAX_PROC_STAT_BYTES = 4096
OWNER_FILENAME = ".workstack-remote-owner.json"
STORE_META = "store-meta.json"
WORKSPACE_FILE = "workspace.json"
SHA256_HEX_LENGTH = 64


class EntryError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


class ProcessController(Protocol):
    def current_pid(self) -> int:
        ...

    def start_identity(self, pid: int) -> str | None:
        ...

    def is_alive(self, pid: int, start_identity: str) -> bool | None:
        ...

    def terminate(self, pid: int) -> None:
        ...


class IsolatedProcessController:
    """Default controller outside Linux /proc. Never kills the current process."""

    def current_pid(self) -> int:
        return os.getpid()

    def start_identity(self, pid: int) -> str | None:
        if pid == os.getpid():
            return "isolated-self"
        return None

    def is_alive(self, pid: int, start_identity: str) -> bool | None:
        if pid == os.getpid() and start_identity == "isolated-self":
            return True
        return False

    def terminate(self, pid: int) -> None:
        if pid == os.getpid():
            raise EntryError("REMOTE_PROTOCOL_INVALID", "refusing to terminate the current process")
        raise EntryError("REMOTE_PROTOCOL_INVALID", "no process controller terminate on this host")


class LinuxProcController:
    def current_pid(self) -> int:
        return os.getpid()

    def start_identity(self, pid: int) -> str | None:
        return _proc_starttime(pid)

    def is_alive(self, pid: int, start_identity: str) -> bool | None:
        if not start_identity:
            return None
        actual = _proc_starttime(pid)
        if actual is None:
            if Path(f"/proc/{pid}").exists():
                return None
            return False
        return actual == start_identity

    def terminate(self, pid: int) -> None:
        if pid <= 0:
            raise EntryError("REMOTE_PROTOCOL_INVALID", "invalid pid")
        if pid == os.getpid():
            raise EntryError("REMOTE_PROTOCOL_INVALID", "refusing to terminate the current process")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError as error:
            raise EntryError("REMOTE_PROTOCOL_INVALID", "cannot terminate owned pid") from error


_PROCESS_CONTROLLER: ProcessController | None = None


def default_process_controller() -> ProcessController:
    if Path("/proc/self/stat").is_file():
        return LinuxProcController()
    return IsolatedProcessController()


def get_process_controller() -> ProcessController:
    if _PROCESS_CONTROLLER is not None:
        return _PROCESS_CONTROLLER
    return default_process_controller()


def set_process_controller(controller: ProcessController | None) -> ProcessController | None:
    global _PROCESS_CONTROLLER
    previous = _PROCESS_CONTROLLER
    _PROCESS_CONTROLLER = controller
    return previous


def _proc_starttime(pid: int) -> str | None:
    path = Path("/proc") / str(pid) / "stat"
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_PROC_STAT_BYTES)
    except FileNotFoundError:
        return None
    except PermissionError:
        return None
    except OSError:
        return None
    try:
        text = payload.decode("ascii")
    except UnicodeError:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2 :].split()
    if len(fields) < 20:
        return None
    starttime = fields[19]
    if not starttime.isdigit():
        return None
    return starttime


def local_path_digest(path: Path) -> str:
    canonical = str(path)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OwnerReceipt:
    workspace_id: str
    data_dir_digest: str
    app_dir_digest: str
    pid: int
    start_identity: str
    release_id: str
    token_hash: str


class _BoundedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "malformed argv")

    def print_usage(self, file=None) -> None:
        return

    def print_help(self, file=None) -> None:
        return


def parse_remote_entry_argv(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _BoundedArgumentParser(prog="remote_entry.py", add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", add_help=False)
    probe.add_argument("--app-dir", required=True)
    probe.add_argument("--data-dir", required=True)
    serve = sub.add_parser("serve", add_help=False)
    serve.add_argument("--app-dir", required=True)
    serve.add_argument("--data-dir", required=True)
    serve.add_argument("--host", required=True)
    serve.add_argument("--port", required=True, type=int)
    serve.add_argument("--public-port", required=True, type=int)
    serve.add_argument("--session-token", required=True)
    serve.add_argument("--exit-with-parent", action="store_true")
    stop = sub.add_parser("stop-owned", add_help=False)
    stop.add_argument("--data-dir", required=True)
    stop.add_argument("--session-token", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command in {"serve", "stop-owned"}:
        try:
            args.session_token = require_session_token(args.session_token)
        except RemoteCommandError as error:
            raise EntryError(error.code, str(error).split(": ", 1)[-1]) from error
    return args


def _bounded_read(path: Path, *, missing_code: str, missing_detail: str) -> bytes:
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_METADATA_FILE_BYTES + 1)
    except OSError as error:
        raise EntryError(missing_code, missing_detail) from error
    if len(payload) > MAX_METADATA_FILE_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"{path.name} too large")
    return payload


def _bounded_json_object(path: Path) -> dict[object, object]:
    payload = _bounded_read(
        path,
        missing_code="REMOTE_WORKSPACE_MISMATCH",
        missing_detail=f"cannot read {path.name}",
    )
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"{path.name} is not JSON") from error
    if not isinstance(value, dict):
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"{path.name} shape")
    return value


def _product_identity(app_dir: Path) -> tuple[str, int]:
    source_path = app_dir / "workstack" / "__init__.py"
    payload = _bounded_read(
        source_path,
        missing_code="REMOTE_APP_MISMATCH",
        missing_detail="app identity is missing",
    )
    try:
        source = payload.decode("utf-8")
        tree = ast.parse(source)
    except (UnicodeError, SyntaxError) as error:
        raise EntryError("REMOTE_APP_MISMATCH", "app identity is invalid") from error
    constants: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "__version__",
                    "REMOTE_PROTOCOL_VERSION",
                }:
                    if node.value is None:
                        continue
                    try:
                        constants[target.id] = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError, TypeError, MemoryError) as error:
                        raise EntryError("REMOTE_APP_MISMATCH", "app identity is invalid") from error
    version = constants.get("__version__")
    protocol = constants.get("REMOTE_PROTOCOL_VERSION")
    if not isinstance(version, str) or not version:
        raise EntryError("REMOTE_APP_MISMATCH", "product version is missing")
    if type(protocol) is not int:
        raise EntryError("REMOTE_APP_MISMATCH", "protocol version is missing")
    return version, protocol


def build_probe_payload(app_dir: Path, data_dir: Path) -> dict[str, object]:
    workspace = _bounded_json_object(data_dir / WORKSPACE_FILE)
    _bounded_json_object(data_dir / STORE_META)
    workspace_id = workspace.get("id")
    if not isinstance(workspace_id, str) or not workspace_id:
        raise EntryError("REMOTE_WORKSPACE_MISMATCH", "workspace id is missing")
    product_version, protocol_version = _product_identity(app_dir)
    return {
        "workspace_id": workspace_id,
        "product_version": product_version,
        "protocol_version": protocol_version,
    }


def encode_probe_stdout(payload: Mapping[str, object]) -> bytes:
    if set(payload) != set(PROBE_KEYS):
        raise EntryError("REMOTE_PROTOCOL_INVALID", "probe schema")
    try:
        encoded = json.dumps(
            {key: payload[key] for key in PROBE_KEYS},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "probe encoding") from error
    if len(encoded) > MAX_STDOUT_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "probe stdout too large")
    if b"\n" in encoded or b"\r" in encoded:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "probe stdout is not one line")
    return encoded + b"\n"


def refuse_extra_stdout(raw: bytes) -> dict[str, object]:
    if len(raw) > MAX_STDOUT_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "probe stdout too large")
    if b"\r" in raw:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "extra probe output")
    if raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
        raise EntryError("REMOTE_PROTOCOL_INVALID", "extra probe output")
    line = raw[:-1]
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "probe stdout is not JSON") from error
    if not isinstance(value, dict) or set(value) != set(PROBE_KEYS):
        raise EntryError("REMOTE_PROTOCOL_INVALID", "probe schema")
    return value


def owner_receipt_path(data_dir: Path) -> Path:
    return data_dir / OWNER_FILENAME


def _sha256_hex(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != SHA256_HEX_LENGTH:
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"{field} is invalid")
    if any(character not in "0123456789abcdef" for character in value):
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"{field} is invalid")
    return value


def parse_owner_receipt(payload: bytes) -> OwnerReceipt:
    if len(payload) > MAX_OWNER_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt too large")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt is not JSON") from error
    if not isinstance(value, dict) or set(value) != set(OWNER_KEYS):
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt shape")
    workspace_id = value["workspace_id"]
    if not isinstance(workspace_id, str) or not workspace_id or len(workspace_id) > 64:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner workspace identity is invalid")
    pid = value["pid"]
    if type(pid) is not int or pid <= 0:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner pid is invalid")
    start_identity = value["start_identity"]
    if not isinstance(start_identity, str) or not start_identity or len(start_identity) > 64:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner start identity is invalid")
    if any(ord(character) < 32 for character in start_identity):
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner start identity is invalid")
    release_id = value["release_id"]
    if not isinstance(release_id, str) or not release_id or len(release_id) > 64:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner release identity is invalid")
    return OwnerReceipt(
        workspace_id=workspace_id,
        data_dir_digest=_sha256_hex(value["data_dir_digest"], "data_dir_digest"),
        app_dir_digest=_sha256_hex(value["app_dir_digest"], "app_dir_digest"),
        pid=pid,
        start_identity=start_identity,
        release_id=release_id,
        token_hash=_sha256_hex(value["token_hash"], "token_hash"),
    )


def read_owner_receipt(data_dir: Path) -> OwnerReceipt | None:
    path = owner_receipt_path(data_dir)
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_OWNER_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt is unreadable") from error
    if len(payload) > MAX_OWNER_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt too large")
    return parse_owner_receipt(payload)


def encode_owner_receipt(receipt: OwnerReceipt) -> bytes:
    encoded = json.dumps(
        {
            "workspace_id": receipt.workspace_id,
            "data_dir_digest": receipt.data_dir_digest,
            "app_dir_digest": receipt.app_dir_digest,
            "pid": receipt.pid,
            "start_identity": receipt.start_identity,
            "release_id": receipt.release_id,
            "token_hash": receipt.token_hash,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_OWNER_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt too large")
    return encoded


def unlink_owner_receipt(data_dir: Path) -> None:
    try:
        owner_receipt_path(data_dir).unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt could not be removed") from error


def write_owner_receipt_exclusive(data_dir: Path, receipt: OwnerReceipt) -> None:
    path = owner_receipt_path(data_dir)
    encoded = encode_owner_receipt(receipt)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError as error:
        raise EntryError("REMOTE_LOCK_OWNED", "pid=unknown") from error
    except OSError as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt could not be created") from error
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


OwnerState = Literal["absent", "dead", "live", "ambiguous"]


def classify_owner(receipt: OwnerReceipt, controller: ProcessController) -> OwnerState:
    alive = controller.is_alive(receipt.pid, receipt.start_identity)
    if alive is None:
        return "ambiguous"
    if alive is False:
        return "dead"
    return "live"


def reclaim_or_refuse_owner(
    data_dir: Path,
    *,
    expected_workspace_id: str | None = None,
    expected_data_digest: str | None = None,
) -> None:
    receipt = read_owner_receipt(data_dir)
    if receipt is None:
        return
    if expected_data_digest is not None and receipt.data_dir_digest != expected_data_digest:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt identity mismatch")
    if expected_workspace_id is not None and receipt.workspace_id != expected_workspace_id:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt identity mismatch")
    state = classify_owner(receipt, get_process_controller())
    if state == "ambiguous":
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner liveness is ambiguous")
    if state == "live":
        raise EntryError("REMOTE_LOCK_OWNED", f"pid={receipt.pid}")
    unlink_owner_receipt(data_dir)


def run_probe(app_dir: Path, data_dir: Path) -> bytes:
    payload = build_probe_payload(app_dir, data_dir)
    reclaim_or_refuse_owner(
        data_dir,
        expected_workspace_id=str(payload["workspace_id"]),
        expected_data_digest=local_path_digest(data_dir),
    )
    return encode_probe_stdout(payload)


def _build_owner_receipt(
    *,
    app_dir: Path,
    data_dir: Path,
    workspace_id: str,
    release_id: str,
    session_token: str,
) -> OwnerReceipt:
    controller = get_process_controller()
    pid = controller.current_pid()
    start = controller.start_identity(pid)
    if not start:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "process start identity is unavailable")
    return OwnerReceipt(
        workspace_id=workspace_id,
        data_dir_digest=local_path_digest(data_dir),
        app_dir_digest=local_path_digest(app_dir),
        pid=pid,
        start_identity=start,
        release_id=release_id,
        token_hash=token_hash(session_token),
    )


def run_serve(args: argparse.Namespace) -> None:
    if not getattr(args, "session_token", None):
        raise EntryError(
            R5_OWNERSHIP_NOT_IMPLEMENTED,
            "session_token must be supplied by the desktop start attempt",
        )
    app_dir = Path(args.app_dir)
    data_dir = Path(args.data_dir)
    runner = app_dir / "run_work_stack.py"
    if not runner.is_file():
        raise EntryError("REMOTE_APP_MISMATCH", "run_work_stack.py is missing")
    probe_payload = build_probe_payload(app_dir, data_dir)
    reclaim_or_refuse_owner(
        data_dir,
        expected_workspace_id=str(probe_payload["workspace_id"]),
        expected_data_digest=local_path_digest(data_dir),
    )
    receipt = _build_owner_receipt(
        app_dir=app_dir,
        data_dir=data_dir,
        workspace_id=str(probe_payload["workspace_id"]),
        release_id=str(probe_payload["product_version"]),
        session_token=str(args.session_token),
    )
    write_owner_receipt_exclusive(data_dir, receipt)
    argv = [
        sys.executable,
        str(runner),
        "--data-dir",
        str(args.data_dir),
        "graph",
        "serve",
        "--host",
        str(args.host),
        "--port",
        str(args.port),
        "--public-port",
        str(args.public_port),
    ]
    if args.exit_with_parent:
        argv.append("--exit-with-parent")
    os.execv(sys.executable, argv)


def run_stop_owned(data_dir: Path, session_token: str) -> None:
    expected_hash = token_hash(session_token)
    expected_data = local_path_digest(data_dir)
    receipt = read_owner_receipt(data_dir)
    if receipt is None:
        return
    if receipt.data_dir_digest != expected_data:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt identity mismatch")
    controller = get_process_controller()
    state = classify_owner(receipt, controller)
    if state == "ambiguous":
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner liveness is ambiguous")
    own = receipt.token_hash == expected_hash
    if state == "dead":
        unlink_owner_receipt(data_dir)
        return
    if not own:
        raise EntryError("REMOTE_LOCK_OWNED", f"pid={receipt.pid}")
    controller.terminate(receipt.pid)
    unlink_owner_receipt(data_dir)


def _emit_error(error: EntryError) -> int:
    message = str(error)
    if len(message) > 512:
        message = message[:512]
    sys.stderr.write(message + "\n")
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = parse_remote_entry_argv(argv)
        if args.command == "probe":
            sys.stdout.buffer.write(run_probe(Path(args.app_dir), Path(args.data_dir)))
            return 0
        if args.command == "serve":
            run_serve(args)
            return 0
        if args.command == "stop-owned":
            run_stop_owned(Path(args.data_dir), str(args.session_token))
            return 0
        raise EntryError("REMOTE_PROTOCOL_INVALID", "unknown command")
    except EntryError as error:
        return _emit_error(error)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

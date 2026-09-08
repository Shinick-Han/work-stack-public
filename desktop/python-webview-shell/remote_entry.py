"""Checked-in remote entry point for probe, serve, and stop-owned.

Runs on the remote interpreter named by the profile.  It never writes the
home directory and never composes a login-shell script.

Serve creates exclusive owner metadata that stores a token hash, never the
raw session token. stop-owned terminates only the matching live process.

Probe accepts an optional caller session token.  A caller that proves it holds
the live owner's token reads its own session instead of being locked out; a
missing or foreign token stays locked, and neither one can take the receipt.

The receipt itself, its host and boot fencing, the guard that serializes every
receipt mutation, and the bounded confirmation that stop-owned's own process
really exited all live in the sibling ``remote_owner`` module and the two
helpers it owns; this file is the argv, probe and exec boundary.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

# Python isolated mode (-I) omits the script directory from sys.path. Admit
# only the resolved directory containing this checked-in file so the sibling
# contract loads from __file__, never cwd, PYTHONPATH, or user site.
_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
sys.path.insert(0, _SHELL_DIR)

from remote_command_contract import (
    R5_OWNERSHIP_NOT_IMPLEMENTED,
    RemoteCommandError,
    require_session_token,
)
from remote_owner import (
    EntryError,
    OWNER_FILENAME,  # noqa: F401  re-exported: the receipt this entry point writes
    acquire_owner_receipt,
    local_path_digest,
    reclaim_or_refuse_owner,
    remove_published_owner_receipt_if_still_ours,
    run_stop_owned,
)


PROBE_KEYS = ("workspace_id", "product_version", "protocol_version")
MAX_METADATA_FILE_BYTES = 1_048_576
MAX_STDOUT_BYTES = 4096
STORE_META = "store-meta.json"
WORKSPACE_FILE = "workspace.json"


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
    probe.add_argument("--session-token", default=None)
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
    if args.command in {"serve", "stop-owned"} or getattr(args, "session_token", None) is not None:
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


def run_probe(app_dir: Path, data_dir: Path, session_token: str | None = None) -> bytes:
    payload = build_probe_payload(app_dir, data_dir)
    reclaim_or_refuse_owner(
        data_dir,
        expected_workspace_id=str(payload["workspace_id"]),
        expected_data_digest=local_path_digest(data_dir),
        caller_token=session_token,
    )
    return encode_probe_stdout(payload)


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
    # Reclaiming a finished owner and publishing this one are one decision, so
    # they happen inside a single hold of the receipt guard; the guard is
    # released before the exec below and is never inherited by the server.
    receipt = acquire_owner_receipt(
        app_dir=app_dir,
        data_dir=data_dir,
        workspace_id=str(probe_payload["workspace_id"]),
        release_id=str(probe_payload["product_version"]),
        session_token=str(args.session_token),
    )
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
    try:
        os.execv(sys.executable, argv)
    except OSError as error:
        cleanup = remove_published_owner_receipt_if_still_ours(data_dir, receipt)
        detail = "could not exec the remote server"
        if cleanup is not None:
            detail = f"{detail}; {cleanup}"
        raise EntryError("REMOTE_PROTOCOL_INVALID", detail) from error


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
            sys.stdout.buffer.write(
                run_probe(
                    Path(args.app_dir),
                    Path(args.data_dir),
                    getattr(args, "session_token", None),
                )
            )
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

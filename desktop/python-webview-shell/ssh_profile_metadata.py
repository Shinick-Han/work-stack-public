"""Bounded read-only metadata probe for one validated SSH connection profile."""

from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
import uuid
from typing import BinaryIO

from connection_registry import SshConnectionProfile
from profile_inspection import SshProfileMetadata
from ssot_connection import find_ssh_executable


MAX_METADATA_BYTES = 4096
METADATA_TIMEOUT_SECONDS = 15.0
_REMOTE_METADATA_SCRIPT = r"""
import ast,json,pathlib,sys
app=pathlib.Path(sys.argv[1]); data=pathlib.Path(sys.argv[2])
def bounded_json(path):
    with path.open('rb') as stream: payload=stream.read(1048577)
    if len(payload)>1048576: raise RuntimeError('metadata too large')
    value=json.loads(payload.decode('utf-8-sig'))
    if not isinstance(value,dict): raise RuntimeError('metadata shape')
    return value
workspace=bounded_json(data/'workspace.json')
bounded_json(data/'store-meta.json')
source=(app/'workstack'/'__init__.py').read_text(encoding='utf-8')
tree=ast.parse(source)
constants={}
for node in tree.body:
    if isinstance(node,(ast.Assign,ast.AnnAssign)):
        targets=node.targets if isinstance(node,ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target,ast.Name) and target.id in {'__version__','REMOTE_PROTOCOL_VERSION'}:
                constants[target.id]=ast.literal_eval(node.value)
print(json.dumps({'workspace_id':workspace.get('id'),'product_version':constants.get('__version__'),'protocol_version':constants.get('REMOTE_PROTOCOL_VERSION')},separators=(',',':')))
""".strip()


def build_ssh_profile_metadata_command(
    profile: SshConnectionProfile, ssh_executable: str
) -> list[str]:
    """Build fixed-shape argv; profile paths are quoted only as remote argv values."""

    remote_argv = (
        "python3",
        "-I",
        "-B",
        "-c",
        _REMOTE_METADATA_SCRIPT,
        profile.remote_app_dir,
        profile.remote_data_dir,
    )
    remote_command = " ".join(shlex.quote(value) for value in remote_argv)
    return [
        ssh_executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ClearAllForwardings=yes",
        "--",
        profile.ssh_host_alias,
        remote_command,
    ]


def run_remote_profile_metadata_check(
    profile: SshConnectionProfile,
    *,
    ssh_executable: str | None = None,
    timeout: float = METADATA_TIMEOUT_SECONDS,
) -> SshProfileMetadata:
    """Return bounded identity metadata without starting or mutating Work Stack."""

    if not 0.1 <= timeout <= 60:
        raise ValueError("timeout must be between 0.1 and 60 seconds")
    command = build_ssh_profile_metadata_command(
        profile, ssh_executable or find_ssh_executable()
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as error:
        raise RuntimeError("OpenSSH metadata check could not be started") from error
    if process.stdout is None:
        process.kill()
        raise RuntimeError("OpenSSH metadata check did not expose bounded output")
    payload, too_large = _read_process_output(process, process.stdout, timeout)
    if too_large:
        raise RuntimeError("SSH metadata response exceeded the safe limit")
    if process.returncode != 0:
        raise RuntimeError("SSH metadata check failed")
    return _parse_metadata(payload)


def _read_process_output(
    process: subprocess.Popen[bytes], stream: BinaryIO, timeout: float
) -> tuple[bytes, bool]:
    result: list[bytes] = []

    def read() -> None:
        try:
            result.append(stream.read(MAX_METADATA_BYTES + 1))
        except OSError:
            result.append(b"")

    reader = threading.Thread(target=read, name="workstack-ssh-metadata-reader", daemon=True)
    started = time.monotonic()
    reader.start()
    reader.join(timeout)
    try:
        if reader.is_alive():
            _kill_and_reap(process)
            reader.join(timeout=5)
            raise RuntimeError("SSH metadata check timed out")
        payload = result[0] if result else b""
        too_large = len(payload) > MAX_METADATA_BYTES
        if too_large:
            _kill_and_reap(process)
        remaining = max(0.1, timeout - (time.monotonic() - started))
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            _kill_and_reap(process)
            raise RuntimeError("SSH metadata check timed out") from error
        return payload, too_large
    finally:
        stream.close()


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("OpenSSH metadata process could not be stopped safely") from error


def _parse_metadata(payload: bytes) -> SshProfileMetadata:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise RuntimeError("SSH metadata response is invalid") from error
    if not isinstance(value, dict) or set(value) != {
        "workspace_id",
        "product_version",
        "protocol_version",
    }:
        raise RuntimeError("SSH metadata response has an invalid shape")
    workspace_id = value["workspace_id"]
    try:
        parsed = uuid.UUID(workspace_id) if isinstance(workspace_id, str) else None
    except ValueError as error:
        raise RuntimeError("SSH metadata workspace identity is invalid") from error
    if parsed is None or parsed.int == 0 or str(parsed) != workspace_id:
        raise RuntimeError("SSH metadata workspace identity is invalid")
    product_version = value["product_version"]
    protocol_version = value["protocol_version"]
    if (
        not isinstance(product_version, str)
        or not product_version
        or len(product_version) > 64
        or any(ord(character) < 32 for character in product_version)
    ):
        raise RuntimeError("SSH metadata product version is invalid")
    if type(protocol_version) is not int or not 0 <= protocol_version <= 1_000_000:
        raise RuntimeError("SSH metadata protocol version is invalid")
    return SshProfileMetadata(workspace_id, product_version, protocol_version)

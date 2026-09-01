"""Strict, side-effect-bounded SSOT connection profile helpers.

This module deliberately knows nothing about the desktop window lifecycle.  It
validates one small persisted profile, constructs fixed-shape OpenSSH commands,
and performs the read-only readiness check used before a remote session starts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import uuid
from dataclasses import dataclass, replace
from pathlib import Path


REMOTE_CONNECTION_FILE = "remote-connection.json"
SSH_HOST_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,254}$")
LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True)
class RemoteConnectionProfile:
    ssh_host_alias: str
    remote_app_dir: str
    remote_data_dir: str
    local_forward_port: int
    workspace_id: str
    remote_port: int = 8765


def _validated_port(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65_535:
        raise ValueError(f"{field} must be an integer from 1 to 65535")
    return value


def _validated_remote_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute Linux path")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{field} contains an invalid control character")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError(f"{field} must not contain '.' or '..' path segments")
    normalized = value.rstrip("/") or "/"
    if normalized == "/":
        raise ValueError(f"{field} must not be the Linux filesystem root")
    return normalized


def _validated_workspace_id(value: object) -> str:
    workspace_id = str(uuid.UUID(str(value)))
    if workspace_id != value or uuid.UUID(workspace_id).int == 0:
        raise ValueError("workspace_id must be a canonical non-nil UUID")
    return workspace_id


def _validate_local_draft(raw: dict[object, object]) -> dict[str, object]:
    unexpected = set(raw) - {"storage_mode"}
    if unexpected:
        fields = ", ".join(sorted(str(field) for field in unexpected))
        raise RuntimeError(f"Local connection draft has unsupported fields: {fields}")
    return {"storage_mode": "local"}


def _validate_remote_shape(raw: dict[object, object]) -> None:
    required = {
        "storage_mode",
        "ssh_host_alias",
        "remote_app_dir",
        "remote_data_dir",
        "local_forward_port",
        "workspace_id",
    }
    allowed = required | {"remote_port"}
    missing = required - set(raw)
    unexpected = set(raw) - allowed
    if missing:
        raise RuntimeError(f"Remote connection draft is missing: {', '.join(sorted(missing))}")
    if unexpected:
        fields = ", ".join(sorted(str(field) for field in unexpected))
        raise RuntimeError(f"Remote connection draft has unsupported fields: {fields}")


def _validated_alias(value: object) -> str:
    if not isinstance(value, str) or not SSH_HOST_ALIAS_PATTERN.fullmatch(value):
        raise RuntimeError(
            "ssh_host_alias must be a configured OpenSSH alias without spaces or shell characters"
        )
    return value


def _normalize_remote_draft(raw: dict[object, object]) -> dict[str, object]:
    _validate_remote_shape(raw)
    alias = _validated_alias(raw["ssh_host_alias"])
    try:
        return {
            "storage_mode": "ssh-remote",
            "ssh_host_alias": alias,
            "remote_app_dir": _validated_remote_path(raw["remote_app_dir"], "remote_app_dir"),
            "remote_data_dir": _validated_remote_path(raw["remote_data_dir"], "remote_data_dir"),
            "local_forward_port": _validated_port(raw["local_forward_port"], "local_forward_port"),
            "workspace_id": _validated_workspace_id(raw["workspace_id"]),
            "remote_port": _validated_port(raw.get("remote_port", 8765), "remote_port"),
        }
    except (AttributeError, ValueError) as error:
        raise RuntimeError(f"Remote connection draft is invalid: {error}") from error


def validate_connection_draft(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise RuntimeError("Connection draft must contain one JSON object")
    mode = raw.get("storage_mode")
    if mode == "local":
        return _validate_local_draft(raw)
    if mode != "ssh-remote":
        raise RuntimeError("storage_mode must be 'local' or 'ssh-remote'")
    return _normalize_remote_draft(raw)


def connection_profile_from_draft(draft: dict[str, object]) -> RemoteConnectionProfile | None:
    normalized = validate_connection_draft(draft)
    if normalized["storage_mode"] == "local":
        return None
    return RemoteConnectionProfile(
        ssh_host_alias=str(normalized["ssh_host_alias"]),
        remote_app_dir=str(normalized["remote_app_dir"]),
        remote_data_dir=str(normalized["remote_data_dir"]),
        local_forward_port=int(normalized["local_forward_port"]),
        workspace_id=str(normalized["workspace_id"]),
        remote_port=int(normalized["remote_port"]),
    )


def load_connection_draft(state_root: Path) -> dict[str, object]:
    profile_path = state_root / REMOTE_CONNECTION_FILE
    if not profile_path.is_file():
        return {"storage_mode": "local"}
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Remote connection profile is invalid JSON: {profile_path}") from error
    try:
        return validate_connection_draft(raw)
    except RuntimeError as error:
        raise RuntimeError(f"Remote connection profile is invalid: {error}") from error


def save_connection_draft(state_root: Path, raw: object) -> dict[str, object]:
    normalized = validate_connection_draft(raw)
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / REMOTE_CONNECTION_FILE
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_connection_file(temporary, normalized)
        os.replace(temporary, path)
    except OSError as error:
        raise RuntimeError("Could not save SSOT connection settings") from error
    finally:
        temporary.unlink(missing_ok=True)
    return normalized


def _write_connection_file(path: Path, normalized: dict[str, object]) -> None:
    payload = json.dumps(normalized, ensure_ascii=True, separators=(",", ":")) + "\n"
    path.write_text(payload, encoding="utf-8")


def load_remote_connection_profile(state_root: Path) -> RemoteConnectionProfile | None:
    return connection_profile_from_draft(load_connection_draft(state_root))


def find_ssh_executable() -> str:
    executable = shutil.which("ssh.exe") or shutil.which("ssh")
    if not executable:
        raise RuntimeError("OpenSSH client was not found. Enable the Windows OpenSSH Client feature first.")
    return executable


def resolve_runtime_forward_port(preferred_port: int) -> int:
    """Return an available loopback port without inspecting a current occupant.

    Availability is necessarily advisory because the socket is released before
    OpenSSH binds it.  The tunnel command therefore always enables
    ExitOnForwardFailure and treats a bind race as a startup failure.
    """

    preferred = _validated_port(preferred_port, "local_forward_port")
    try:
        return _bind_available_port(preferred)
    except OSError:
        return _bind_available_port(0)


def _bind_available_port(port: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((LOOPBACK_HOST, port))
        return int(listener.getsockname()[1])


def profile_with_runtime_forward_port(profile: RemoteConnectionProfile) -> RemoteConnectionProfile:
    runtime_port = resolve_runtime_forward_port(profile.local_forward_port)
    if runtime_port == profile.local_forward_port:
        return profile
    return replace(profile, local_forward_port=runtime_port)


def build_remote_server_command(profile: RemoteConnectionProfile) -> str:
    runner = f"{profile.remote_app_dir}/run_work_stack.py"
    identity_store = f"{profile.remote_data_dir}/store-meta.json"
    workspace_store = f"{profile.remote_data_dir}/workspace.json"
    arguments = [
        "python3",
        runner,
        "--data-dir",
        profile.remote_data_dir,
        "graph",
        "serve",
        "--host",
        LOOPBACK_HOST,
        "--port",
        str(profile.remote_port),
        "--public-port",
        str(profile.local_forward_port),
        "--exit-with-parent",
    ]
    prefix = (
        f"test -f {shlex.quote(identity_store)} && "
        f"test -f {shlex.quote(workspace_store)} && "
        f"cd -- {shlex.quote(profile.remote_app_dir)} && exec "
    )
    return prefix + " ".join(shlex.quote(argument) for argument in arguments)


def build_ssh_tunnel_command(
    profile: RemoteConnectionProfile, ssh_executable: str
) -> list[str]:
    return [
        ssh_executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-L",
        f"{LOOPBACK_HOST}:{profile.local_forward_port}:{LOOPBACK_HOST}:{profile.remote_port}",
        "--",
        profile.ssh_host_alias,
        build_remote_server_command(profile),
    ]


def build_ssh_check_command(
    profile: RemoteConnectionProfile, ssh_executable: str
) -> list[str]:
    runner = f"{profile.remote_app_dir}/run_work_stack.py"
    remote_check = " && ".join(
        (
            f"test -f {shlex.quote(runner)}",
            f"test -d {shlex.quote(profile.remote_data_dir)}",
            f"test -f {shlex.quote(profile.remote_data_dir + '/store-meta.json')}",
            f"test -f {shlex.quote(profile.remote_data_dir + '/workspace.json')}",
            "command -v python3 >/dev/null 2>&1",
            f"cd -- {shlex.quote(profile.remote_app_dir)}",
            f"python3 {shlex.quote(runner)} --help >/dev/null 2>&1",
        )
    )
    return [
        ssh_executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        profile.ssh_host_alias,
        remote_check,
    ]


def run_remote_connection_check(profile: RemoteConnectionProfile) -> None:
    command = build_ssh_check_command(profile, find_ssh_executable())
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            command,
            check=False,
            timeout=15,
            creationflags=creation_flags,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("SSH connection check timed out after 15 seconds") from error
    except OSError as error:
        raise RuntimeError("OpenSSH connection check could not be started") from error
    _require_successful_check(result)


def _require_successful_check(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or "").strip().splitlines()
    suffix = f" Last SSH message: {detail[-1][:300]}" if detail else ""
    raise RuntimeError(
        "SSH connection check failed. Confirm the host alias, known-host key, "
        "SSH agent, remote paths, and python3." + suffix
    )


def check_remote_connection(state_root: Path) -> int:
    profile = load_remote_connection_profile(state_root.resolve())
    if profile is None:
        raise RuntimeError(
            f"SSH remote mode is not configured in {state_root / REMOTE_CONNECTION_FILE}"
        )
    run_remote_connection_check(profile)
    print(
        json.dumps(
            {
                "status": "ready",
                "storage_mode": "ssh-remote",
                "ssh_host_alias": profile.ssh_host_alias,
                "local_forward_port": profile.local_forward_port,
                "remote_port": profile.remote_port,
                "workspace_id": profile.workspace_id,
            },
            separators=(",", ":"),
        )
    )
    return 0

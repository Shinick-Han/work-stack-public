"""Bounded read-only local SSH transport for remote provisioning facts.

The remote collector is the checked-in sibling ``remote_provision_collector.py``,
streamed on SSH stdin to ``python -I -B -``. This module never concatenates
sources and never imports the collector into the remote interpreter.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from remote_provision_collector import (
    COLLECTOR_COMMAND,
    FACTS_KEYS,
    FROZEN_TARGET_JSON,
    MAX_FACTS_BYTES,
    MAX_IDENTITY_BYTES,
    MAX_SOURCE_BYTES,
    MAX_STDERR_BYTES,
    RECEIPT_NAME,
    ProbeError,
    collect_provision_facts,
    collector_main,
    encode_facts_line,
    parse_collector_argv,
    remote_main,
    _owner_arg,
    _overlap,
    _reject_constant,
    _unique_object,
)


PROBE_TIMEOUT_SECONDS = 15.0
COLLECTOR_FILENAME = "remote_provision_collector.py"
STABLE_PROBE_CODES = frozenset(
    {
        "SSH_AUTH_FAILED",
        "REMOTE_PYTHON_NOT_FOUND",
        "REMOTE_PYTHON_TOO_OLD",
        "REMOTE_PYTHON_REQUIRED",
        "REMOTE_APP_MISMATCH",
        "REMOTE_WORKSPACE_MISMATCH",
        "REMOTE_LOCK_OWNED",
        "REMOTE_PROTOCOL_INVALID",
        "TARGET_OWNERSHIP_MISMATCH",
        "TARGET_REPARSE_AMBIGUITY",
        "INVALID_PROBE",
        "INVALID_FACTS",
        "PROBE_TIMEOUT",
        "PROBE_OVERSIZE",
    }
)


def build_ssh_provision_probe_command(
    profile: object, owner: str, ssh_executable: str
) -> list[str]:
    """Build fixed-shape argv from the shared OpenSSH probe options."""

    from remote_command_contract import (
        RemoteCommandError,
        join_remote_tokens,
        require_remote_python,
        scan_tokens_for_live_ssh,
        validated_posix_path,
    )

    try:
        python = require_remote_python(getattr(profile, "remote_python", None))
        install = validated_posix_path(getattr(profile, "remote_app_dir"), "remote_app_dir")
        data = validated_posix_path(getattr(profile, "remote_data_dir"), "remote_data_dir")
    except RemoteCommandError as error:
        if error.code == "REMOTE_PYTHON_REQUIRED":
            raise ProbeError("REMOTE_PYTHON_REQUIRED") from None
        raise ProbeError("INVALID_PROBE", "probe command is invalid") from None
    user = _owner_arg(owner)
    if _overlap(install, data):
        raise ProbeError("INVALID_PROBE", "install and data roots must be separate")
    tokens = [
        python,
        "-I",
        "-B",
        "-",
        COLLECTOR_COMMAND,
        "--install-root",
        install,
        "--data-root",
        data,
        "--owner",
        user,
    ]
    scan_tokens_for_live_ssh(tokens)
    if not isinstance(ssh_executable, str) or not ssh_executable:
        raise ProbeError("INVALID_PROBE", "ssh executable is required")
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
        str(getattr(profile, "ssh_host_alias")),
        join_remote_tokens(tokens),
    ]


def _require_timeout(timeout: float) -> None:
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise ValueError("timeout must be between 0.1 and 60 seconds")
    if not 0.1 <= float(timeout) <= 60:
        raise ValueError("timeout must be between 0.1 and 60 seconds")


def _module_source() -> bytes:
    path = os.path.join(_SHELL_DIR, COLLECTOR_FILENAME)
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        raise ProbeError("INVALID_PROBE", "probe source could not be read") from None
    if len(payload) > MAX_SOURCE_BYTES:
        raise ProbeError("INVALID_PROBE", "probe source exceeds the stream bound")
    return payload


def _spawn_probe(process_factory: Callable[..., object], command: list[str]) -> object:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = process_factory(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
    except OSError:
        raise ProbeError("INVALID_PROBE", "OpenSSH provision probe could not be started") from None
    return process


def _kill_and_reap(process: object) -> None:
    try:
        killer = getattr(process, "kill")
        waiter = getattr(process, "wait")
        killer()
        waiter(timeout=5)
    except (OSError, subprocess.SubprocessError, TypeError, AttributeError):
        raise ProbeError("INVALID_PROBE", "provision probe process could not be stopped safely") from None


def _write_source(process: object, source: bytes, timeout: float) -> None:
    stream = getattr(process, "stdin", None)
    if stream is None:
        _kill_and_reap(process)
        raise ProbeError("INVALID_PROBE", "provision probe did not expose stdin")
    result: list[str] = []

    def write() -> None:
        try:
            stream.write(source)
        except OSError:
            result.append("write")
        try:
            stream.close()
        except OSError:
            result.append("close")

    writer = threading.Thread(target=write, name="workstack-provision-probe-stdin", daemon=True)
    writer.start()
    writer.join(timeout)
    if writer.is_alive() or result:
        _kill_and_reap(process)
        raise ProbeError("PROBE_TIMEOUT", "provision probe timed out")


def _read_stdout(process: object, timeout: float) -> tuple[bytes, bool]:
    stream = getattr(process, "stdout", None)
    if stream is None:
        _kill_and_reap(process)
        raise ProbeError("INVALID_PROBE", "provision probe did not expose bounded output")
    chunks: list[bytes] = []

    def read() -> None:
        try:
            chunks.append(stream.read(MAX_FACTS_BYTES + 1))
        except OSError:
            chunks.append(b"")

    started = time.monotonic()
    reader = threading.Thread(target=read, name="workstack-provision-probe-stdout", daemon=True)
    reader.start()
    reader.join(timeout)
    try:
        if reader.is_alive():
            _kill_and_reap(process)
            reader.join(timeout=5)
            raise ProbeError("PROBE_TIMEOUT", "provision probe timed out")
        payload = chunks[0] if chunks else b""
        too_large = len(payload) > MAX_FACTS_BYTES
        if too_large:
            _kill_and_reap(process)
        remaining = max(0.1, timeout - (time.monotonic() - started))
        try:
            getattr(process, "wait")(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_and_reap(process)
            raise ProbeError("PROBE_TIMEOUT", "provision probe timed out") from None
        return payload, too_large
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _read_stderr(process: object) -> bytes:
    stream = getattr(process, "stderr", None)
    if stream is None:
        return b""
    try:
        payload = stream.read(MAX_STDERR_BYTES)
    except OSError:
        payload = b""
    try:
        stream.close()
    except OSError:
        pass
    return payload


def _sanitize_failure(stderr_payload: bytes) -> ProbeError:
    try:
        text = stderr_payload.decode("utf-8", errors="replace")
    except UnicodeError:
        return ProbeError("INVALID_PROBE", "provision probe failed")
    line = text.strip().splitlines()[0] if text.strip() else ""
    code = line.split(":", 1)[0].strip()
    if code in STABLE_PROBE_CODES:
        return ProbeError(code, "provision probe failed")
    return ProbeError("INVALID_PROBE", "provision probe failed")


def _exchange_probe(process: object, source: bytes, timeout: float) -> bytes:
    _write_source(process, source, timeout)
    payload, too_large = _read_stdout(process, timeout)
    stderr_payload = _read_stderr(process)
    if too_large:
        raise ProbeError("PROBE_OVERSIZE", "SSH facts response exceeded the safe limit")
    returncode = getattr(process, "returncode", 1)
    if returncode != 0:
        raise _sanitize_failure(stderr_payload)
    return payload


def _require_one_line(payload: bytes) -> bytes:
    if len(payload) > MAX_FACTS_BYTES:
        raise ProbeError("PROBE_OVERSIZE", "SSH facts response exceeded the safe limit")
    if b"\r" in payload or payload.count(b"\n") != 1 or not payload.endswith(b"\n"):
        raise ProbeError("INVALID_FACTS", "facts response is not one JSON line")
    return payload[:-1]


def _mapping_from_parsed(parsed: object) -> dict[str, object]:
    python = getattr(parsed, "python")
    install = getattr(parsed, "install")
    data = getattr(parsed, "data")
    return {
        "os": getattr(parsed, "os"),
        "python": None
        if python is None
        else {"path": python.path, "version": python.version},
        "install": {
            "exists": install.exists,
            "owner": install.owner,
            "symlink": install.symlink,
            "product_version": install.product_version,
            "protocol_version": install.protocol_version,
            "digest": install.digest,
        },
        "data": {
            "exists": data.exists,
            "owner": data.owner,
            "symlink": data.symlink,
            "workspace_id": data.workspace_id,
        },
    }


def _facts_from_stdout(payload: bytes) -> dict[str, object]:
    from remote_provision_plan import PlanError, _parse_facts

    line = _require_one_line(payload)
    try:
        value = json.loads(
            line.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        raise ProbeError("INVALID_FACTS", "facts response is invalid") from None
    if type(value) is not dict:
        raise ProbeError("INVALID_FACTS", "facts response is invalid")
    try:
        parsed = _parse_facts(value)
    except PlanError:
        raise ProbeError("INVALID_FACTS", "facts response is invalid") from None
    return _mapping_from_parsed(parsed)


def run_remote_provision_probe(
    profile: object,
    owner: str,
    *,
    ssh_executable: str | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    process_factory: Callable[..., object] = subprocess.Popen,
) -> dict[str, object]:
    """Run the read-only SSH facts probe with bounded IO and no shell."""

    _require_timeout(timeout)
    executable = ssh_executable
    if executable is None:
        from ssot_connection import find_ssh_executable

        executable = find_ssh_executable()
    command = build_ssh_provision_probe_command(profile, owner, executable)
    source = _module_source()
    process = _spawn_probe(process_factory, command)
    payload = _exchange_probe(process, source, timeout)
    return _facts_from_stdout(payload)

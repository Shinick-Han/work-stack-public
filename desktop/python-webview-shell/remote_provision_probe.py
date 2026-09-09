"""Bounded read-only local SSH transport for remote provisioning facts.

The remote collector is the checked-in sibling ``remote_provision_collector.py``,
streamed on SSH stdin to ``python -I -B -`` together with the checked-in
stdlib-only siblings it imports. The stream installs each sibling as its own
named module before the collector source runs, so no source is concatenated
into a shared namespace and the remote interpreter never needs a file, a
package or a search path. This module never imports the collector into the
remote interpreter.

The default probe is read-only on the remote: it stats and reads bounded
identity documents and never writes. The single exception is the explicit
opt-in ``capability_scratch_parent`` mode, which creates and removes one
disposable directory under the selected application parent to measure atomic
publication. It is never enabled by default and never accepts the data root.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
import zlib
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
    SCRATCH_FLAG,
    ProbeError,
    collect_provision_facts,
    collector_main,
    encode_facts_line,
    parse_collector_argv,
    remote_main,
    revalidate_provision_resolution,
    _owner_arg,
    _overlap,
    _reject_constant,
    _unique_object,
)


PROBE_TIMEOUT_SECONDS = 15.0
COLLECTOR_FILENAME = "remote_provision_collector.py"
COLLECTOR_HELPER_FILENAMES = (
    "remote_provision_contract.py",
    "remote_provision_host.py",
    "remote_provision_identity.py",
    "remote_provision_capability.py",
)
COMPOSITION_PREAMBLE = """import base64 as _ws_b64
import sys as _ws_sys
import types as _ws_types
import zlib as _ws_zlib


def _ws_text(_ws_blob, _ws_max):
    _ws_raw = _ws_zlib.decompressobj().decompress(_ws_b64.b64decode(_ws_blob), _ws_max + 1)
    if len(_ws_raw) > _ws_max:
        raise ValueError("decoded module source exceeds the bound")
    return _ws_raw.decode("utf-8")


def _ws_install(_ws_name, _ws_blob, _ws_max):
    _ws_module = _ws_types.ModuleType(_ws_name)
    _ws_module.__file__ = _ws_name + ".py"
    exec(compile(_ws_text(_ws_blob, _ws_max), _ws_module.__file__, "exec"), _ws_module.__dict__)
    _ws_sys.modules[_ws_name] = _ws_module


try:
"""
COMPOSITION_EPILOGUE = """except Exception:
    _ws_sys.stderr.write("INVALID_PROBE: remote source composition failed\\n")
    raise SystemExit(2)
exec(_ws_code, globals())
"""
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
        "TARGET_RESOLUTION_DRIFT",
        "TARGET_NOEXEC",
        "PUBLICATION_UNAVAILABLE",
        "NFS_PUBLISH_FAILED",
        "INVALID_PROBE",
        "INVALID_FACTS",
        "PROBE_TIMEOUT",
        "PROBE_OVERSIZE",
    }
)


def build_ssh_provision_probe_command(
    profile: object,
    owner: str,
    ssh_executable: str,
    *,
    capability_scratch_parent: str | None = None,
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
    if capability_scratch_parent is not None:
        try:
            scratch = validated_posix_path(capability_scratch_parent, "capability_scratch_parent")
        except RemoteCommandError:
            raise ProbeError("INVALID_PROBE", "probe command is invalid") from None
        if scratch == install or scratch == data or scratch.startswith(data + "/"):
            raise ProbeError("INVALID_PROBE", "scratch parent must be the application parent")
        tokens.extend([SCRATCH_FLAG, scratch])
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


def _module_text(filename: str) -> str:
    path = os.path.join(_SHELL_DIR, filename)
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        raise ProbeError("INVALID_PROBE", "probe source could not be read") from None
    if len(payload) > MAX_SOURCE_BYTES:
        raise ProbeError("INVALID_PROBE", "probe source exceeds the stream bound")
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        raise ProbeError("INVALID_PROBE", "probe source is not valid UTF-8") from None


def _module_blob(filename: str) -> str:
    """Deterministically compress one verified local module for the stream.

    The compressed payload is decompressed again here and compared against the
    exact bytes just read, so the remote only ever receives a payload this
    process has verified round-trips to the checked-in source.
    """

    source = _module_text(filename).encode("utf-8")
    payload = zlib.compress(source, 9)
    if zlib.decompress(payload) != source:
        raise ProbeError("INVALID_PROBE", "probe source could not be composed")
    return base64.b64encode(payload).decode("ascii")


def _module_source() -> bytes:
    """Compose the collector and every sibling it imports into one bounded stream.

    The remote interpreter is started with ``-I -B -``: it has no package, no
    import path we control and no file to read back, and one SSH stdin payload
    must stay within ``MAX_SOURCE_BYTES``. Each checked-in sibling is therefore
    carried as a deterministic zlib/base64 blob of its own source and installed
    in ``sys.modules`` under its own name before the collector source runs as
    ``__main__``. Nothing is concatenated into a shared namespace, a remote
    lookalike module can never be imported instead, the decoded size of every
    module is bounded on the remote by the same ``MAX_SOURCE_BYTES``, and a
    composition failure is reported as a bounded ``INVALID_PROBE`` line rather
    than a traceback. Only these checked-in modules are ever carried.
    """

    parts = [COMPOSITION_PREAMBLE]
    for filename in COLLECTOR_HELPER_FILENAMES:
        parts.append(
            "    _ws_install({}, {}, {})\n".format(
                ascii(filename[: -len(".py")]), ascii(_module_blob(filename)), MAX_SOURCE_BYTES
            )
        )
    parts.append(
        "    _ws_code = compile(_ws_text({}, {}), {}, 'exec')\n".format(
            ascii(_module_blob(COLLECTOR_FILENAME)), MAX_SOURCE_BYTES, ascii(COLLECTOR_FILENAME)
        )
    )
    parts.append(COMPOSITION_EPILOGUE)
    source = "".join(parts).encode("ascii")
    if len(source) > MAX_SOURCE_BYTES:
        raise ProbeError("INVALID_PROBE", "probe source exceeds the stream bound")
    return source


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
    mapping: dict[str, object] = {
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
    resolution = getattr(parsed, "resolution", None)
    if resolution is not None:
        mapping["resolution"] = {
            "install": {
                "configured": resolution.install.configured,
                "canonical": resolution.install.canonical,
                "stable": resolution.install.stable,
            },
            "data": {
                "configured": resolution.data.configured,
                "canonical": resolution.data.canonical,
                "stable": resolution.data.stable,
            },
        }
    host = getattr(parsed, "host", None)
    if host is not None:
        mapping["host"] = {
            "runtime": host.runtime,
            "machine": host.machine,
            "binding": "selected_profile",
        }
    capability = getattr(parsed, "capability", None)
    if capability is not None:
        mapping["capability"] = {
            "publication": capability.publication,
            "method": capability.method,
            "commit": capability.commit,
            "scratch": capability.scratch,
            "filesystem": capability.filesystem,
            "noexec": capability.noexec,
            "nfs_publish": capability.nfs_publish,
        }
    return mapping


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
    capability_scratch_parent: str | None = None,
) -> dict[str, object]:
    """Run the SSH facts probe with bounded IO and no shell.

    Read-only by default. Passing ``capability_scratch_parent`` opts in to the
    one bounded remote write this API performs: a disposable directory created
    and removed under the selected application parent to measure atomic
    publication. The data root and its descendants are never accepted there.
    """

    _require_timeout(timeout)
    executable = ssh_executable
    if executable is None:
        from ssot_connection import find_ssh_executable

        executable = find_ssh_executable()
    command = build_ssh_provision_probe_command(
        profile,
        owner,
        executable,
        capability_scratch_parent=capability_scratch_parent,
    )
    source = _module_source()
    process = _spawn_probe(process_factory, command)
    payload = _exchange_probe(process, source, timeout)
    return _facts_from_stdout(payload)


def revalidate_remote_provision_resolution(
    profile: object,
    owner: str,
    previous_facts: dict[str, object],
    **kwargs: object,
) -> dict[str, object]:
    """Re-probe the selected profile and refuse canonical-target drift.

    Refusal covers both a changed canonical target and a current observation the
    collector itself reported as unstable, so matching canonical strings alone
    never authorize apply.
    """

    from remote_provision_plan import compare_provision_resolution

    current = run_remote_provision_probe(profile, owner, **kwargs)
    code = compare_provision_resolution(previous_facts, current)
    if code:
        raise ProbeError(
            code,
            "configured path resolution is unstable or no longer matches the inspected canonical target",
        )
    return current

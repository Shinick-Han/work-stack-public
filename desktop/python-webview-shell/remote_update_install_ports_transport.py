"""Bounded SSH argv and verified-unpack stdin payload for install ports.

Transactional install reuses ``apply_remote_install`` (and therefore
``build_installer_payload_source``). This module only assembles the verified
unpack stdin program and the OpenSSH argv that carries it, using the same
csh-safe token join and bounded exchange as the provision driver.

The payload embeds the checked-in unpack and installer admission modules.
Admission of archive bytes stays inside ``place_verified_unpack``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from bounded_process_exchange import (  # noqa: E402
    CLEANUP_SETTLED,
    ExchangeError,
    ExchangeOutput,
    NO_OUTPUT,
    NO_STDIN,
    NOT_STARTED,
    OVERSIZE,
    TIMEOUT,
    start_exchange,
)
from remote_command_contract import (  # noqa: E402
    RemoteCommandError,
    join_remote_tokens,
    require_remote_python,
    require_safe_token,
    scan_tokens_for_live_ssh,
    validated_posix_path,
)
from remote_provision_artifact import ArtifactSelection, load_json_object  # noqa: E402
from remote_provision_installer import MAX_ARCHIVE, MAX_SIDECAR  # noqa: E402
from remote_provision_installer import MAX_STDERR, MAX_STDOUT  # noqa: E402


UNPACK_PLACE = "place"
UNPACK_INSPECT = "inspect"
UNPACK_VERIFY = "verify"
UNPACK_OPERATIONS = frozenset({UNPACK_PLACE, UNPACK_INSPECT, UNPACK_VERIFY})
MAX_MODULE_BYTES = 128 * 1024
CHUNK_CHARS = 4096
BOOTSTRAP_BUDGET = 8192
LEAF_NAME = "remote_provision_installer_linux"
ADMISSION_NAME = "remote_provision_installer"
UNPACK_NAME = "remote_verified_unpack"
_BASE64_RE = re.compile(r"\A[A-Za-z0-9+/]*={0,2}\Z")

OPENSSH_BATCH = (
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
)

AMBIGUOUS_EXCHANGE = frozenset({TIMEOUT, OVERSIZE, NO_OUTPUT})
PRE_EFFECT_EXCHANGE = frozenset({NOT_STARTED, NO_STDIN})

_PROLOGUE = """import base64
import json
import sys
import types

_LEAF_NAME = "remote_provision_installer_linux"
_MAIN_NAME = "remote_provision_installer"
_UNPACK_NAME = "remote_verified_unpack"
"""

_EPILOGUE = """

def _load(name, encoded):
    module = types.ModuleType(name)
    module.__file__ = "<" + name + ">"
    module.__loader__ = None
    module.__spec__ = None
    sys.modules[name] = module
    exec(compile(base64.b64decode(encoded), "<" + name + ">", "exec"), module.__dict__)
    return module


def _parse_argv(argv):
    if len(argv) != 3 or argv[1] != "--app-dir":
        return None
    operation = argv[0]
    if operation not in ("place", "inspect", "verify"):
        return None
    return operation, argv[2]


def _main():
    _load(_LEAF_NAME, _LEAF_B64)
    _load(_MAIN_NAME, _MAIN_B64)
    unpack = _load(_UNPACK_NAME, _UNPACK_B64)
    parsed = _parse_argv(sys.argv[1:])
    if parsed is None:
        result = unpack._not_ready("REMOTE_PROTOCOL_INVALID", unpack.PLACEMENT_ABSENT)
    else:
        operation, app_dir = parsed
        archive = base64.b64decode(_ARCHIVE_B64)
        sidecar = base64.b64decode(_SIDECAR_B64)
        if operation == "inspect":
            result = unpack.inspect_unpack_target(app_dir)
        elif operation == "verify":
            result = unpack.verify_unpack_identity(app_dir, archive, sidecar)
        else:
            result = unpack.place_verified_unpack(
                archive_bytes=archive, sidecar_bytes=sidecar, app_dir=app_dir
            )
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\\n")
    if result.get("outcome") == unpack.OUTCOME_UNPACKED:
        return 0
    return 2


try:
    _status = _main()
except SystemExit:
    raise
except BaseException:
    try:
        sys.stderr.write(
            '{"outcome":"not_ready","code":"REMOTE_INSTALL_FAILED",'
            '"placement":"unknown"}\\n'
        )
        sys.stderr.flush()
    except OSError:
        pass
    _status = 2
sys.exit(_status)
"""


class TransportError(RuntimeError):
    """Bounded unpack transport failure. Detail is a fixed local sentence."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True)
class UnpackExchange:
    """One bounded unpack stdin exchange, or why it did not complete."""

    kind: str
    output: ExchangeOutput | None
    code: str | None
    cleanup: str = CLEANUP_SETTLED
    document: Mapping[str, object] | None = None


def _encoded_bound(raw_bytes: int) -> int:
    return ((raw_bytes + 2) // 3) * 4


def _literal_bound(raw_bytes: int) -> int:
    encoded = _encoded_bound(raw_bytes)
    lines = max(1, (encoded + CHUNK_CHARS - 1) // CHUNK_CHARS)
    return encoded + lines * 3


MAX_UNPACK_PAYLOAD = (
    BOOTSTRAP_BUDGET
    + _literal_bound(MAX_MODULE_BYTES) * 3
    + _literal_bound(MAX_ARCHIVE)
    + _literal_bound(MAX_SIDECAR)
)


def _checked_bytes(value: object, maximum: int) -> bytes:
    if type(value) is not bytes or not value or len(value) > maximum:
        raise TransportError("INVALID_PAYLOAD_INPUT", "bytes within the fixed caps are required")
    return value


def _read_module(filename: str) -> bytes:
    path = os.path.join(_SHELL_DIR, filename)
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_MODULE_BYTES + 1)
    except OSError:
        raise TransportError("PAYLOAD_SOURCE_UNAVAILABLE", "a checked-in module could not be read") from None
    if not payload or len(payload) > MAX_MODULE_BYTES:
        raise TransportError("PAYLOAD_SOURCE_UNAVAILABLE", "a checked-in module could not be read")
    return payload


def _literal(name: str, payload: bytes) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    if _BASE64_RE.fullmatch(encoded) is None:
        raise TransportError("PAYLOAD_SOURCE_UNAVAILABLE", "a checked-in module could not be read")
    chunks = [encoded[start : start + CHUNK_CHARS] for start in range(0, len(encoded), CHUNK_CHARS)]
    if not chunks:
        chunks = [""]
    body = "".join('"%s"\n' % chunk for chunk in chunks)
    return "%s = (\n%s)\n" % (name, body)


def build_unpack_payload_source(artifact: ArtifactSelection) -> bytes:
    """ASCII program for ``python -I -B -`` that dispatches place/inspect/verify."""

    if not isinstance(artifact, ArtifactSelection):
        raise TransportError("DRIVER_ARTIFACT_INVALID", "an ArtifactSelection is required")
    archive = _checked_bytes(artifact.archive_bytes, MAX_ARCHIVE)
    sidecar = _checked_bytes(artifact.sidecar_bytes, MAX_SIDECAR)
    source = "".join(
        (
            _PROLOGUE,
            _literal("_LEAF_B64", _read_module(LEAF_NAME + ".py")),
            _literal("_MAIN_B64", _read_module(ADMISSION_NAME + ".py")),
            _literal("_UNPACK_B64", _read_module(UNPACK_NAME + ".py")),
            _literal("_ARCHIVE_B64", archive),
            _literal("_SIDECAR_B64", sidecar),
            _EPILOGUE,
        )
    )
    try:
        encoded = source.encode("ascii")
    except UnicodeEncodeError:
        raise TransportError("PAYLOAD_SOURCE_UNAVAILABLE", "a checked-in module could not be read") from None
    if len(encoded) > MAX_UNPACK_PAYLOAD:
        raise TransportError("PAYLOAD_TOO_LARGE", "generated unpack payload exceeds the source bound")
    return encoded


def _require_ssh_executable(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TransportError("INVALID_PROBE", "ssh executable is required")
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        raise TransportError("INVALID_PROBE", "ssh executable is required")
    return value


def _require_host_alias(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("-"):
        raise TransportError("DRIVER_PROFILE_INVALID", "profile fields are not a valid remote target")
    try:
        token = require_safe_token(value)
    except RemoteCommandError:
        raise TransportError("DRIVER_PROFILE_INVALID", "profile fields are not a valid remote target") from None
    if not token[0].isalnum():
        raise TransportError("DRIVER_PROFILE_INVALID", "profile fields are not a valid remote target")
    return token


def unpack_remote_tokens(remote_python: object, operation: str, app_dir: str) -> list[str]:
    if operation not in UNPACK_OPERATIONS:
        raise TransportError("REMOTE_PROTOCOL_INVALID", "unpack operation is invalid")
    try:
        python = require_remote_python(remote_python)
        target = validated_posix_path(app_dir, "remote_app_dir")
    except RemoteCommandError as error:
        raise TransportError(error.code, "unpack command is invalid") from None
    tokens = [python, "-I", "-B", "-", operation, "--app-dir", target]
    scan_tokens_for_live_ssh(tokens)
    return tokens


def build_ssh_unpack_command(
    profile: object,
    ssh_executable: str,
    operation: str,
    app_dir: str,
) -> list[str]:
    """Fixed-shape OpenSSH argv. Never a login-shell script."""

    executable = _require_ssh_executable(ssh_executable)
    alias = _require_host_alias(getattr(profile, "ssh_host_alias", None))
    remote = join_remote_tokens(
        unpack_remote_tokens(getattr(profile, "remote_python", None), operation, app_dir)
    )
    command = [executable, *OPENSSH_BATCH, alias, remote]
    return command


def parse_unpack_document(payload: bytes) -> Mapping[str, object] | None:
    if not payload or len(payload) > MAX_STDOUT:
        return None
    if b"\r" in payload or payload.count(b"\n") != 1 or not payload.endswith(b"\n"):
        return None
    try:
        document = load_json_object(payload[:-1])
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        return None
    return document


def run_unpack_exchange(
    process_factory: Callable[..., object],
    command: list[str],
    payload: bytes,
    timeout: float,
) -> UnpackExchange:
    """Write one stdin program and drain bounded stdout. Never retries."""

    try:
        exchange = start_exchange(process_factory, command)
        exchange.write(payload, timeout)
        output = exchange.drain(MAX_STDOUT, MAX_STDERR, timeout)
    except ExchangeError as error:
        if error.code in PRE_EFFECT_EXCHANGE:
            return UnpackExchange("refused", None, error.code, error.cleanup, None)
        if error.code in AMBIGUOUS_EXCHANGE:
            return UnpackExchange("lost", None, error.code, error.cleanup, None)
        return UnpackExchange("refused", None, error.code, error.cleanup, None)
    document = parse_unpack_document(output.stdout)
    if document is None:
        return UnpackExchange("lost", output, "DRIVER_RECEIPT_UNRECOGNISED", CLEANUP_SETTLED, None)
    return UnpackExchange("output", output, None, CLEANUP_SETTLED, document)


def encode_line(document: Mapping[str, object]) -> bytes:
    encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return encoded.encode("utf-8") + b"\n"


__all__ = [
    "AMBIGUOUS_EXCHANGE",
    "MAX_STDERR",
    "MAX_STDOUT",
    "MAX_UNPACK_PAYLOAD",
    "OPENSSH_BATCH",
    "TransportError",
    "UNPACK_INSPECT",
    "UNPACK_OPERATIONS",
    "UNPACK_PLACE",
    "UNPACK_VERIFY",
    "UnpackExchange",
    "build_ssh_unpack_command",
    "build_unpack_payload_source",
    "encode_line",
    "parse_unpack_document",
    "run_unpack_exchange",
    "unpack_remote_tokens",
]

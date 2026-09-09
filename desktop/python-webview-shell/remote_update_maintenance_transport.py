"""Bounded SSH transport for one remote maintenance request document.

This is the desktop side of the frozen remote adapter in
``remote_update_maintenance.py``: one SSH process, one request document written
to its stdin, one result document read back from its stdout.  It builds the
argv, it runs exactly one bounded exchange, and it says which of two things
happened -- the far side answered, or it did not and the effect is unknown.
It does not decide what a backup or a restore *means*; that is the port's job.

Three properties this module exists to hold:

*   **The command is built, never assembled from strings.**  Every token goes
    through ``remote_command_contract``: an absolute traversal-free POSIX
    interpreter and helper path, a safe host alias, and the same audited
    OpenSSH option block the provision probe and install already use.  Nothing
    here reads SSH config, discovers a host, uploads a payload, installs
    anything, or touches the store.

*   **The helper location is an input, not a guess.**  The remote adapter is a
    new module.  An application directory installed before it existed does not
    contain it, and this transport will not pretend otherwise: it takes the
    absolute remote path of the helper from its caller, who is responsible for
    having placed and verified it.  A helper that is not there fails the
    exchange like any other command that will not run, and the caller decides
    what that means for the operation it was about to issue.

*   **Pre-effect and post-effect are never confused.**  ``start_exchange``
    raising ``NOT_STARTED`` and ``write`` raising ``NO_STDIN`` both happen
    before a single byte of the request has left this machine, so they settle
    as ``EFFECT_NONE``.  Everything else -- a timeout, an oversized reply, no
    output at all, a reply that is not this tool's document -- happens once the
    request may already have been read and run, so it settles as
    ``EFFECT_UNKNOWN``.  ``bounded_process_exchange`` documents exactly that
    boundary and this module reads it rather than restating it.

The reply this module hands back carries either the helper's own document or a
bounded symbolic code.  No argv, no path, no host, no interpreter, no stderr
text ever leaves here.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
if _SHELL_DIR not in sys.path:
    sys.path.insert(0, _SHELL_DIR)

from bounded_process_exchange import (  # noqa: E402
    NO_STDIN,
    NOT_STARTED,
    ExchangeError,
    ExchangeOutput,
    start_exchange,
)
from remote_command_contract import (  # noqa: E402
    RemoteCommandError,
    join_remote_tokens,
    require_safe_token,
    require_workspace_uid,
    scan_tokens_for_live_ssh,
    validated_posix_path,
)

#: The frozen remote adapter's own contract.  A document that does not carry
#: both of these is not this tool's answer and is never read as one.
HELPER_SCHEMA_VERSION = "remote-update-maintenance/1"
HELPER_TOOL = "workstack-remote-maintenance/1"

KIND_CREATE_BACKUP = "create_backup"
KIND_OBSERVE_BACKUP = "observe_backup"
KIND_RESTORE_BACKUP = "restore_backup"
KIND_OBSERVE_RESTORE = "observe_restore"
KINDS = frozenset({
    KIND_CREATE_BACKUP,
    KIND_OBSERVE_BACKUP,
    KIND_RESTORE_BACKUP,
    KIND_OBSERVE_RESTORE,
})
#: The kinds that name a source archive, mirroring the adapter's request keys.
SOURCED_KINDS = frozenset({KIND_RESTORE_BACKUP, KIND_OBSERVE_RESTORE})

#: What the exchange established about the far side's effect.
EFFECT_NONE = "none"
EFFECT_UNKNOWN = "unknown"

#: Bounded symbolic transport codes.  Lowercase, because these travel with the
#: flow's detailed codes, not with the diagnostic surface.
CODE_TARGET_INVALID = "maintenance_target_invalid"
CODE_REQUEST_UNBUILDABLE = "maintenance_request_unbuildable"
CODE_CHANNEL_NOT_STARTED = "maintenance_channel_not_started"
CODE_CHANNEL_NO_STDIN = "maintenance_channel_no_stdin"
CODE_CHANNEL_LOST = "maintenance_channel_lost"
CODE_REPLY_UNRECOGNISED = "maintenance_reply_unrecognised"

#: Exchange failure codes that are provably pre-effect, per the documented
#: contract of ``bounded_process_exchange``.
_PRE_EFFECT_EXCHANGE_CODES = {
    NOT_STARTED: CODE_CHANNEL_NOT_STARTED,
    NO_STDIN: CODE_CHANNEL_NO_STDIN,
}

MAX_STDOUT_BYTES = 8192
MAX_STDERR_BYTES = 512
MAX_REQUEST_BYTES = 16 * 1024
MAX_TIMEOUT_SECONDS = 900.0
MIN_TIMEOUT_SECONDS = 0.1
DEFAULT_TIMEOUT_SECONDS = 300.0

DIGEST_PREFIX = "sha256:"
DIGEST_BODY_CHARS = frozenset("0123456789abcdef")

_SSH_OPTIONS = (
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


class MaintenanceTargetError(ValueError):
    """One bounded refusal to accept a target, a helper path or a request."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MaintenanceTarget:
    """The one remote runtime and store this transport is bound to.

    Nothing here is discovered.  ``ssh_host_alias`` and ``remote_python`` come
    from the currently selected remote runtime; ``remote_helper_path`` is the
    absolute path of ``remote_update_maintenance.py`` on that remote, which the
    caller has already placed and checked.  The two roots are the adapter's
    own state and archive directories and must lie outside the workspace, which
    the adapter re-checks on its own side before it does anything.
    """

    ssh_host_alias: str
    remote_python: str
    remote_helper_path: str
    remote_workspace_dir: str
    workspace_id: str
    remote_state_root: str
    remote_backup_root: str


@dataclass(frozen=True)
class MaintenanceReply:
    """How one exchange settled: an answer, or an effect that is not known.

    ``document`` is the adapter's own result document, present only when the
    far side answered in its own schema.  When it is ``None``, ``effect`` says
    whether the request can have had an effect at all: ``EFFECT_NONE`` means
    the request never left this machine.
    """

    document: Mapping[str, Any] | None
    effect: str
    code: str

    @property
    def answered(self) -> bool:
        return self.document is not None


def _validated_alias(value: object) -> str:
    """A host alias that is one safe token and cannot be read as an option."""

    if type(value) is not str or not value or value.startswith("-"):
        raise MaintenanceTargetError(CODE_TARGET_INVALID)
    try:
        token = require_safe_token(value)
    except RemoteCommandError:
        raise MaintenanceTargetError(CODE_TARGET_INVALID) from None
    if not token[0].isalnum():
        raise MaintenanceTargetError(CODE_TARGET_INVALID)
    return token


def _validated_executable(value: object) -> str:
    if type(value) is not str or not value:
        raise MaintenanceTargetError(CODE_TARGET_INVALID)
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        raise MaintenanceTargetError(CODE_TARGET_INVALID)
    return value


def _validated_remote_path(value: object, field: str) -> str:
    try:
        return validated_posix_path(value, field)
    except RemoteCommandError:
        raise MaintenanceTargetError(CODE_TARGET_INVALID) from None


def validated_digest(value: object) -> str:
    """The adapter's digest spelling, checked before it is put in a request."""

    if type(value) is not str or not value.startswith(DIGEST_PREFIX):
        raise MaintenanceTargetError(CODE_REQUEST_UNBUILDABLE)
    body = value[len(DIGEST_PREFIX):]
    if len(body) != 64 or not set(body) <= DIGEST_BODY_CHARS:
        raise MaintenanceTargetError(CODE_REQUEST_UNBUILDABLE)
    return value


def _validated_operation_id(value: object) -> str:
    """The adapter's own operation-id spelling: one canonical UUID string.

    Deliberately the adapter's rule rather than the stricter workspace-uid one,
    so this transport never refuses an identity the far side would have
    accepted and left the flow holding a phantom refusal.
    """

    if type(value) is not str:
        raise MaintenanceTargetError(CODE_REQUEST_UNBUILDABLE)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise MaintenanceTargetError(CODE_REQUEST_UNBUILDABLE) from None
    if str(parsed) != value:
        raise MaintenanceTargetError(CODE_REQUEST_UNBUILDABLE)
    return value


def validated_timeout(value: object) -> float:
    if type(value) not in (int, float):
        raise MaintenanceTargetError(CODE_TARGET_INVALID)
    bound = float(value)
    if not MIN_TIMEOUT_SECONDS <= bound <= MAX_TIMEOUT_SECONDS:
        raise MaintenanceTargetError(CODE_TARGET_INVALID)
    return bound


def validated_target(target: object) -> MaintenanceTarget:
    """Re-admit every field of a target, whatever object was handed over."""

    if not isinstance(target, MaintenanceTarget):
        raise MaintenanceTargetError(CODE_TARGET_INVALID)
    workspace = _validated_remote_path(target.remote_workspace_dir, "workspace_dir")
    try:
        workspace_id = require_workspace_uid(target.workspace_id)
    except RemoteCommandError:
        raise MaintenanceTargetError(CODE_TARGET_INVALID) from None
    return MaintenanceTarget(
        ssh_host_alias=_validated_alias(target.ssh_host_alias),
        remote_python=_validated_remote_path(target.remote_python, "remote_python"),
        remote_helper_path=_validated_remote_path(
            target.remote_helper_path, "remote_helper_path"
        ),
        remote_workspace_dir=workspace,
        workspace_id=workspace_id,
        remote_state_root=_validated_remote_path(target.remote_state_root, "state_root"),
        remote_backup_root=_validated_remote_path(
            target.remote_backup_root, "backup_root"
        ),
    )


def build_request(
    target: MaintenanceTarget,
    *,
    kind: str,
    operation_id: str,
    source_operation_id: str | None = None,
    backup_digest: str | None = None,
) -> dict[str, Any]:
    """Exactly the keys the frozen adapter admits for this kind, or refuse.

    The adapter rejects an unknown key and a missing one alike, so the request
    is built to its shape here rather than discovered by a failed round trip.
    """

    if kind not in KINDS:
        raise MaintenanceTargetError(CODE_REQUEST_UNBUILDABLE)
    checked = validated_target(target)
    document: dict[str, Any] = {
        "backup_root": checked.remote_backup_root,
        "kind": kind,
        "operation_id": _validated_operation_id(operation_id),
        "state_root": checked.remote_state_root,
        "workspace_dir": checked.remote_workspace_dir,
        "workspace_id": checked.workspace_id,
    }
    if kind in SOURCED_KINDS:
        document["source_operation_id"] = _validated_operation_id(source_operation_id)
        document["backup_digest"] = validated_digest(backup_digest)
    elif source_operation_id is not None or backup_digest is not None:
        raise MaintenanceTargetError(CODE_REQUEST_UNBUILDABLE)
    return document


def encode_request(document: Mapping[str, Any]) -> bytes:
    """One compact UTF-8 document, inside the adapter's own size bound."""

    try:
        payload = json.dumps(document, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError):
        raise MaintenanceTargetError(CODE_REQUEST_UNBUILDABLE) from None
    if len(payload) > MAX_REQUEST_BYTES:
        raise MaintenanceTargetError(CODE_REQUEST_UNBUILDABLE)
    return payload


def build_command(target: MaintenanceTarget, ssh_executable: str) -> list[str]:
    """Fixed-shape OpenSSH argv running the checked helper with no site state.

    ``-I`` isolates the interpreter and ``-B`` keeps it from writing bytecode
    next to an application directory it does not own.  The helper reads its one
    request from stdin, so no operation detail is ever an argv token.
    """

    executable = _validated_executable(ssh_executable)
    checked = validated_target(target)
    tokens = [checked.remote_python, "-I", "-B", checked.remote_helper_path]
    try:
        scan_tokens_for_live_ssh(tokens)
        remote = join_remote_tokens(tokens)
    except RemoteCommandError:
        raise MaintenanceTargetError(CODE_TARGET_INVALID) from None
    return [executable, *_SSH_OPTIONS, checked.ssh_host_alias, remote]


def _decoded_document(payload: bytes) -> Mapping[str, Any] | None:
    """The adapter's own result document, or nothing at all."""

    if not payload or len(payload) > MAX_STDOUT_BYTES:
        return None
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if type(decoded) is not dict:
        return None
    if decoded.get("schema_version") != HELPER_SCHEMA_VERSION:
        return None
    if decoded.get("tool") != HELPER_TOOL:
        return None
    return decoded


def _lost(code: str) -> MaintenanceReply:
    return MaintenanceReply(None, EFFECT_UNKNOWN, code)


def _exchange_failure(error: ExchangeError) -> MaintenanceReply:
    """Read the exchange's own pre/post-effect boundary, never guess one."""

    code = _PRE_EFFECT_EXCHANGE_CODES.get(error.code)
    if code is not None:
        return MaintenanceReply(None, EFFECT_NONE, code)
    return _lost(CODE_CHANNEL_LOST)


def interpret(result: ExchangeOutput) -> MaintenanceReply:
    """One completed exchange becomes one reply.

    The exit status is not consulted: the adapter exits non-zero for every
    non-verified answer, including answers that are perfectly well-formed
    refusals.  The document is the answer; its absence is the unknown.
    """

    document = _decoded_document(result.stdout)
    if document is None:
        return _lost(CODE_REPLY_UNRECOGNISED)
    return MaintenanceReply(document, EFFECT_UNKNOWN, str(document.get("code") or ""))


def exchange(
    target: MaintenanceTarget,
    request: Mapping[str, Any],
    *,
    ssh_executable: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    process_factory: Callable[..., object] = subprocess.Popen,
) -> MaintenanceReply:
    """Run one bounded request/response over SSH and settle it exactly once.

    Every refusal raised while the argv and the payload are being built happens
    before any process exists, so it is reported as ``EFFECT_NONE``.  From
    ``start_exchange`` onward the classification is the exchange's own.
    """

    try:
        bound = validated_timeout(timeout)
        command = build_command(target, ssh_executable)
        payload = encode_request(request)
    except MaintenanceTargetError as error:
        return MaintenanceReply(None, EFFECT_NONE, error.code)
    try:
        channel = start_exchange(process_factory, command)
        channel.write(payload, bound)
        result = channel.drain(MAX_STDOUT_BYTES, MAX_STDERR_BYTES, bound)
    except ExchangeError as error:
        return _exchange_failure(error)
    return interpret(result)


__all__ = [
    "CODE_CHANNEL_LOST",
    "CODE_CHANNEL_NOT_STARTED",
    "CODE_CHANNEL_NO_STDIN",
    "CODE_REPLY_UNRECOGNISED",
    "CODE_REQUEST_UNBUILDABLE",
    "CODE_TARGET_INVALID",
    "DEFAULT_TIMEOUT_SECONDS",
    "EFFECT_NONE",
    "EFFECT_UNKNOWN",
    "HELPER_SCHEMA_VERSION",
    "HELPER_TOOL",
    "KINDS",
    "KIND_CREATE_BACKUP",
    "KIND_OBSERVE_BACKUP",
    "KIND_OBSERVE_RESTORE",
    "KIND_RESTORE_BACKUP",
    "MAX_STDERR_BYTES",
    "MAX_STDOUT_BYTES",
    "MaintenanceReply",
    "MaintenanceTarget",
    "MaintenanceTargetError",
    "SOURCED_KINDS",
    "build_command",
    "build_request",
    "encode_request",
    "exchange",
    "interpret",
    "validated_digest",
    "validated_target",
    "validated_timeout",
]

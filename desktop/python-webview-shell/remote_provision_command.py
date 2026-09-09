"""Pure argv builder for remote provision-install and skill SSH commands.

This module returns a fixed-shape OpenSSH argv. It never launches a process,
never reads the filesystem or SSH config, never discovers host aliases, and
never imports server or store modules.

Import is effect-free: no stdin, argv, environment, network, or platform
reads run at import. Profile fields are taken from the caller-supplied object.
"""

from __future__ import annotations

from remote_command_contract import (
    REMOTE_PYTHON_REQUIRED,
    RemoteCommandError,
    join_provision_install_command,
    join_skill_command,
    require_safe_token,
)

_INVALID_INSTALL = "install command is invalid"
_INVALID_SKILL = "skill command is invalid"
_INVALID_ALIAS = "ssh host alias is invalid"
_INVALID_EXECUTABLE = "ssh executable is required"


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or character == "\x7f" for character in value)


def _require_ssh_executable(value: object) -> str:
    if not isinstance(value, str) or not value or _has_control(value):
        raise RemoteCommandError("REMOTE_PROTOCOL_INVALID", _INVALID_EXECUTABLE)
    return value


def _require_host_alias(value: object) -> str:
    if not isinstance(value, str) or not value or _has_control(value):
        raise RemoteCommandError("REMOTE_PROTOCOL_INVALID", _INVALID_ALIAS)
    if value.startswith("-"):
        raise RemoteCommandError("REMOTE_PROTOCOL_INVALID", _INVALID_ALIAS)
    try:
        token = require_safe_token(value)
    except RemoteCommandError:
        raise RemoteCommandError("REMOTE_PROTOCOL_INVALID", _INVALID_ALIAS) from None
    if not token[0].isalnum():
        raise RemoteCommandError("REMOTE_PROTOCOL_INVALID", _INVALID_ALIAS)
    return token


def _closed_install_error(error: RemoteCommandError) -> RemoteCommandError:
    if error.code == REMOTE_PYTHON_REQUIRED:
        return RemoteCommandError(REMOTE_PYTHON_REQUIRED)
    return RemoteCommandError("REMOTE_PROTOCOL_INVALID", _INVALID_INSTALL)


def build_ssh_provision_install_command(
    profile: object, owner: str, ssh_executable: str
) -> list[str]:
    """Build fixed-shape argv from the shared OpenSSH probe options."""

    executable = _require_ssh_executable(ssh_executable)
    alias = _require_host_alias(getattr(profile, "ssh_host_alias", None))
    try:
        remote = join_provision_install_command(
            remote_python=getattr(profile, "remote_python", None),
            remote_app_dir=getattr(profile, "remote_app_dir", None),
            remote_data_dir=getattr(profile, "remote_data_dir", None),
            owner=owner,
            expected_workspace_uid=getattr(profile, "expected_workspace_id", None),
        )
    except RemoteCommandError as error:
        raise _closed_install_error(error) from None
    return [
        executable,
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
        alias,
        remote,
    ]


def _closed_skill_error(error: RemoteCommandError) -> RemoteCommandError:
    if error.code == REMOTE_PYTHON_REQUIRED:
        return RemoteCommandError(REMOTE_PYTHON_REQUIRED)
    return RemoteCommandError("REMOTE_PROTOCOL_INVALID", _INVALID_SKILL)


def build_ssh_skill_command(
    profile: object, ssh_executable: str, *, apply: bool = False
) -> list[str]:
    """Build fixed-shape argv that execs the packaged skill helper by path."""

    executable = _require_ssh_executable(ssh_executable)
    alias = _require_host_alias(getattr(profile, "ssh_host_alias", None))
    try:
        remote = join_skill_command(
            remote_python=getattr(profile, "remote_python", None),
            remote_app_dir=getattr(profile, "remote_app_dir", None),
            apply=apply,
        )
    except RemoteCommandError as error:
        raise _closed_skill_error(error) from None
    return [
        executable,
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
        alias,
        remote,
    ]

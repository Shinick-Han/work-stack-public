"""Bounded, read-only discovery of OpenSSH host aliases.

The desktop uses this module only to suggest aliases already present in the
user's OpenSSH configuration and to ask OpenSSH how a selected alias resolves.
It never reads identity files, connects to a host, or attempts to reproduce
OpenSSH's full configuration evaluator.
"""

from __future__ import annotations

import glob
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_CONFIG_FILES = 32
DEFAULT_MAX_INCLUDE_DEPTH = 4
MAX_CONFIG_BYTES = 1_048_576
SSH_HOST_ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,254}$")
_WILDCARD_CHARACTERS = frozenset("*?![]")


@dataclass(frozen=True)
class ResolvedSshHost:
    """Non-secret connection summary returned by ``ssh -G``."""

    alias: str
    hostname: str
    user: str
    port: int


def default_user_ssh_config(home: Path | None = None) -> Path:
    """Return the normal per-user OpenSSH config location on Windows and Unix."""

    user_home = Path.home() if home is None else Path(home)
    return user_home / ".ssh" / "config"


def validate_ssh_host_alias(value: object) -> str:
    """Accept one destination token, never an option or command fragment."""

    if not isinstance(value, str) or not SSH_HOST_ALIAS_PATTERN.fullmatch(value):
        raise ValueError(
            "SSH host alias must start with a letter or number and contain only "
            "letters, numbers, '.', '_', '@', or '-'"
        )
    return value


def discover_ssh_host_aliases(
    config_path: Path | None = None,
    *,
    max_files: int = DEFAULT_MAX_CONFIG_FILES,
    max_include_depth: int = DEFAULT_MAX_INCLUDE_DEPTH,
) -> tuple[str, ...]:
    """Discover concrete ``Host`` aliases from the user's SSH config.

    Include traversal is deliberately a suggestion-only subset of OpenSSH's
    behavior.  Includes must resolve beneath the directory containing the root
    config, recursion and file count are bounded, and recursive ``**`` globs
    are ignored.  OpenSSH remains authoritative through :func:`resolve_ssh_host`.
    """

    _validate_discovery_limits(max_files, max_include_depth)
    root_config = default_user_ssh_config() if config_path is None else Path(config_path)
    allowed_root = root_config.parent.resolve(strict=False)
    aliases: dict[str, str] = {}
    directives = _walk_config_directives(
        root_config, allowed_root, max_files, max_include_depth
    )
    for keyword, arguments in directives:
        if keyword != "host":
            continue
        for token in arguments:
            if _is_concrete_alias(token):
                aliases.setdefault(token.casefold(), token)

    return tuple(sorted(aliases.values(), key=lambda alias: (alias.casefold(), alias)))


def resolve_ssh_host(
    alias: object,
    *,
    ssh_executable: str | None = None,
    timeout_seconds: float = 10,
) -> ResolvedSshHost:
    """Resolve a strictly validated alias with OpenSSH without connecting.

    ``ssh -G`` only prints the evaluated configuration.  The subprocess shape
    is fixed and the destination is separated by ``--``.  Only HostName, User,
    and Port are retained; identity paths and all other configuration are
    intentionally discarded.
    """

    selected = validate_ssh_host_alias(alias)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError("timeout_seconds must be a positive number")
    if timeout_seconds <= 0 or timeout_seconds > 30:
        raise ValueError("timeout_seconds must be greater than zero and at most 30")

    executable = ssh_executable or shutil.which("ssh.exe") or shutil.which("ssh")
    if not executable:
        raise RuntimeError("OpenSSH client was not found")
    command = [
        executable,
        "-G",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "--",
        selected,
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            command,
            check=False,
            timeout=float(timeout_seconds),
            creationflags=creation_flags,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("OpenSSH alias resolution timed out") from error
    except OSError as error:
        raise RuntimeError("OpenSSH alias resolution could not be started") from error
    if result.returncode != 0:
        raise RuntimeError("OpenSSH could not resolve the selected host alias")

    values = _parse_safe_resolution(result.stdout)
    try:
        hostname = _validated_display_value(values["hostname"], "hostname")
        user = _validated_display_value(values["user"], "user")
        port = int(values["port"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("OpenSSH returned an incomplete host resolution") from error
    if not 1 <= port <= 65_535:
        raise RuntimeError("OpenSSH returned an invalid host port")
    return ResolvedSshHost(alias=selected, hostname=hostname, user=user, port=port)


def _is_concrete_alias(token: str) -> bool:
    return not any(character in token for character in _WILDCARD_CHARACTERS) and bool(
        SSH_HOST_ALIAS_PATTERN.fullmatch(token)
    )


def _validate_discovery_limits(max_files: int, max_include_depth: int) -> None:
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
        raise ValueError("max_files must be a positive integer")
    if isinstance(max_include_depth, bool) or not isinstance(max_include_depth, int):
        raise ValueError("max_include_depth must be a non-negative integer")
    if max_include_depth < 0:
        raise ValueError("max_include_depth must be a non-negative integer")


def _walk_config_directives(
    root_config: Path,
    allowed_root: Path,
    max_files: int,
    max_include_depth: int,
):
    pending: list[tuple[Path, int]] = [(root_config, 0)]
    visited: set[Path] = set()
    while pending and len(visited) < max_files:
        candidate, depth = pending.pop(0)
        resolved = candidate.resolve(strict=False)
        if resolved in visited or not _is_beneath(resolved, allowed_root):
            continue
        visited.add(resolved)
        lines = _read_bounded_config(resolved)
        if lines is None:
            continue
        for keyword, arguments in _iter_directives(lines):
            yield keyword, arguments
            if keyword == "include" and depth < max_include_depth:
                _queue_includes(
                    pending, visited, arguments, allowed_root, depth, max_files
                )


def _queue_includes(
    pending: list[tuple[Path, int]],
    visited: set[Path],
    patterns: list[str],
    allowed_root: Path,
    depth: int,
    max_files: int,
) -> None:
    if len(visited) + len(pending) >= max_files:
        return
    for pattern in patterns:
        for included in _resolve_include_pattern(pattern, allowed_root, allowed_root):
            if included.resolve(strict=False) not in visited:
                pending.append((included, depth + 1))
            if len(visited) + len(pending) >= max_files:
                return


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_bounded_config(path: Path) -> list[str] | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
            return None
        return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return None


def _iter_directives(lines: list[str]):
    for raw_line in lines:
        tokens = _tokenize_directive(raw_line)
        if len(tokens) >= 2:
            yield tokens[0].casefold(), tokens[1:]


def _tokenize_directive(line: str) -> list[str]:
    """Tokenize the small Host/Include subset without evaluating escapes."""

    tokens: list[str] = []
    token: list[str] = []
    quote: str | None = None
    for character in line:
        if quote is not None:
            if character == quote:
                quote = None
            else:
                token.append(character)
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == "#":
            break
        elif character.isspace():
            if token:
                tokens.append("".join(token))
                token = []
        else:
            token.append(character)
    if token:
        tokens.append("".join(token))
    if tokens and "=" in tokens[0]:
        keyword, value = tokens[0].split("=", 1)
        tokens = [keyword, value, *tokens[1:]]
    return tokens


def _resolve_include_pattern(pattern: str, base: Path, allowed_root: Path) -> tuple[Path, ...]:
    if not pattern or "\x00" in pattern or "**" in pattern:
        return ()
    if pattern.startswith("~/") or pattern.startswith("~\\"):
        candidate_pattern = allowed_root.parent / pattern[2:]
    else:
        candidate = Path(pattern)
        candidate_pattern = candidate if candidate.is_absolute() else base / candidate

    matches: list[Path] = []
    for match in glob.iglob(str(candidate_pattern), recursive=False):
        candidate = Path(match)
        resolved = candidate.resolve(strict=False)
        if _is_beneath(resolved, allowed_root) and resolved.is_file():
            matches.append(resolved)
        if len(matches) >= DEFAULT_MAX_CONFIG_FILES:
            break
    return tuple(matches)


def _parse_safe_resolution(output: str) -> dict[str, str]:
    retained: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(" ")
        normalized = key.casefold()
        if separator and normalized in {"hostname", "user", "port"}:
            retained.setdefault(normalized, value.strip())
    return retained


def _validated_display_value(value: str, field: str) -> str:
    if not value or len(value) > 1_024 or any(ord(character) < 32 for character in value):
        raise ValueError(f"invalid {field}")
    return value

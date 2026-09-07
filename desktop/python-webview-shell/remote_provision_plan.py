"""Read-only remote Linux provisioning inspect/plan.

This module never SSHes, never installs, and never treats caller-supplied
facts as a live probe.  A successful document is always ``plan_only`` with
``pending_live_probe`` verification.
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from remote_command_contract import (
    REMOTE_PYTHON_REQUIRED,
    RemoteCommandError,
    validated_posix_path,
)
from workstack import REMOTE_PROTOCOL_VERSION, __version__


SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 8192
MAX_PRODUCT_VERSION_LENGTH = 64
MAX_PROTOCOL_VERSION = 1_000_000
MAX_DETAIL_LENGTH = 256
MAX_CODE_LENGTH = 64
# Linux remote runs the product service/CLI (README: Python 3.10 or newer).
# The Windows embeddable 3.12.10 bundle is not the remote minimum.
MIN_PYTHON = (3, 10)
REMOTE_PYTHON_TOO_OLD = "REMOTE_PYTHON_TOO_OLD"
SUPPORTED_OS = "linux"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
OWNER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
PYTHON_VERSION_PATTERN = re.compile(
    r"^3\.([0-9]|[1-9][0-9])(?:\.([0-9]|[1-9][0-9]{0,2}))?$"
)

DOCUMENT_KEYS = frozenset({"schema_version", "target", "artifact", "facts"})
TARGET_KEYS = frozenset(
    {"os", "install_root", "data_root", "owner", "expected_workspace_id"}
)
ARTIFACT_KEYS = frozenset({"product_version", "protocol_version", "digest"})
FACTS_KEYS = frozenset({"os", "python", "install", "data"})
PYTHON_KEYS = frozenset({"path", "version"})
INSTALL_KEYS = frozenset(
    {"exists", "owner", "symlink", "product_version", "protocol_version", "digest"}
)
DATA_KEYS = frozenset({"exists", "owner", "symlink", "workspace_id"})
SCHEMA_LOCATIONS = frozenset(
    {"request", "target", "artifact", "facts", "python", "install", "data"}
)


class PlanError(ValueError):
    """Caller document is not a valid inspect/plan request."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = _clip_text(code, MAX_CODE_LENGTH)
        self.detail = _clip_text(detail, MAX_DETAIL_LENGTH)
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")


@dataclass(frozen=True)
class PythonFacts:
    path: str
    version: str
    series: tuple[int, int]


@dataclass(frozen=True)
class InstallFacts:
    exists: bool
    owner: str | None
    symlink: bool
    product_version: str | None
    protocol_version: int | None
    digest: str | None


@dataclass(frozen=True)
class DataFacts:
    exists: bool
    owner: str | None
    symlink: bool
    workspace_id: str | None


@dataclass(frozen=True)
class Target:
    os: str
    install_root: str
    data_root: str
    owner: str
    expected_workspace_id: str


@dataclass(frozen=True)
class Artifact:
    product_version: str
    protocol_version: int
    digest: str


@dataclass(frozen=True)
class Facts:
    os: str
    python: PythonFacts | None
    install: InstallFacts
    data: DataFacts


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    detail: str


def plan_remote_provision(raw: bytes | str) -> dict[str, object]:
    """Return a deterministic plan from bounded UTF-8 JSON. Never contacts a remote host."""

    return render_plan(interpret_provision_request(parse_provision_request(raw)))


def parse_provision_request(raw: bytes | str) -> dict[str, object]:
    encoded = _utf8_payload(raw)
    if not encoded or len(encoded) > MAX_DOCUMENT_BYTES:
        raise PlanError("INVALID_DOCUMENT", "request exceeds the inspect/plan bound")
    return _load_json_object(encoded)


def _utf8_payload(raw: object) -> bytes:
    try:
        if type(raw) is str:
            return raw.encode("utf-8")
        if type(raw) is bytes:
            return raw
    except UnicodeError:
        raise PlanError("INVALID_DOCUMENT", "request is not UTF-8") from None
    raise PlanError("INVALID_DOCUMENT", "request must be UTF-8 JSON")


def _load_json_object(encoded: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except PlanError:
        raise
    except UnicodeError:
        raise PlanError("INVALID_DOCUMENT", "request is not UTF-8") from None
    except RecursionError:
        raise PlanError("INVALID_DOCUMENT", "request is not a bounded JSON object") from None
    except ValueError:
        raise PlanError("INVALID_DOCUMENT", "request is not a bounded JSON object") from None
    if type(value) is not dict:
        raise PlanError("INVALID_DOCUMENT", "request must contain one JSON object")
    return value


def interpret_provision_request(raw: dict[str, object]) -> tuple[Target, Artifact, Facts]:
    if type(raw) is not dict:
        raise PlanError("INVALID_DOCUMENT", "request must contain one JSON object")
    _exact_keys(raw, DOCUMENT_KEYS, "request")
    if raw["schema_version"] != SCHEMA_VERSION or isinstance(raw["schema_version"], bool):
        raise PlanError("INVALID_DOCUMENT", "schema_version must be 1")
    target = _parse_target(raw["target"])
    artifact = _parse_artifact(raw["artifact"])
    facts = _parse_facts(raw["facts"])
    return target, artifact, facts


def render_plan(parsed: tuple[Target, Artifact, Facts]) -> dict[str, object]:
    target, artifact, facts = parsed
    diagnostics = _collect_diagnostics(target, artifact, facts)
    decision, action = _decision(diagnostics, facts.install, artifact)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "plan_only",
        "verification": "pending_live_probe",
        "provenance": {
            "facts": "supplied",
            "manifest": "supplied",
            "observed": "unverified",
        },
        "decision": decision,
        "diagnostics": [
            {"code": item.code, "severity": item.severity, "detail": item.detail}
            for item in diagnostics
        ],
        "plan": {
            "action": action,
            "install_root": target.install_root,
            "data_root": target.data_root,
            "remote_python": None if facts.python is None else facts.python.path,
            "product_version": artifact.product_version,
            "protocol_version": artifact.protocol_version,
            "digest": artifact.digest,
        },
    }


def encode_plan(document: dict[str, object]) -> bytes:
    encoded = json.dumps(
        document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return encoded + b"\n"


def _clip_text(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    return value[: maximum - 1] + "…"


def _unique_object(pairs: list[tuple[object, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str:
            raise PlanError("INVALID_DOCUMENT", "keys must be strings")
        if key in document:
            raise PlanError("INVALID_DOCUMENT", "duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(value: str) -> object:
    raise PlanError("INVALID_DOCUMENT", "invalid JSON constant")


def _location(name: str) -> str:
    if name in SCHEMA_LOCATIONS:
        return name
    return "request"


def _exact_keys(raw: dict[str, object], required: frozenset[str], context: str) -> None:
    place = _location(context)
    if any(type(key) is not str for key in raw):
        raise PlanError("INVALID_DOCUMENT", f"{place} keys must be strings")
    present = set(raw)
    if required - present:
        raise PlanError("INVALID_DOCUMENT", f"{place} is missing required fields")
    if present - required:
        raise PlanError("INVALID_DOCUMENT", f"{place} has unsupported fields")


def _require_object(value: object, context: str) -> dict[str, object]:
    if type(value) is not dict:
        raise PlanError("INVALID_DOCUMENT", f"{_location(context)} must contain one JSON object")
    return value


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PlanError("INVALID_DOCUMENT", f"{field} is out of bounds")
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        raise PlanError("INVALID_DOCUMENT", f"{field} contains a control character")
    return value


def _linux_os(value: object, field: str) -> str:
    text = _bounded_text(value, field, 16)
    if text != SUPPORTED_OS:
        raise PlanError("INVALID_DOCUMENT", f"{field} must be linux")
    return text


def _owner(value: object, field: str) -> str:
    text = _bounded_text(value, field, 32)
    if not OWNER_PATTERN.fullmatch(text):
        raise PlanError("INVALID_DOCUMENT", f"{field} is not a POSIX user name")
    return text


def _workspace_id(value: object, field: str) -> str:
    text = _bounded_text(value, field, 36)
    try:
        parsed = uuid.UUID(text)
    except ValueError as error:
        raise PlanError("INVALID_DOCUMENT", f"{field} must be a canonical non-nil UUID") from error
    if text != str(parsed) or parsed.int == 0:
        raise PlanError("INVALID_DOCUMENT", f"{field} must be a canonical non-nil UUID")
    return text


def _posix(value: object, field: str) -> str:
    try:
        return validated_posix_path(value, field)
    except RemoteCommandError as error:
        raise PlanError("INVALID_DOCUMENT", f"{field} is not a valid Linux POSIX path") from error


def _product_version(value: object, field: str) -> str:
    return _bounded_text(value, field, MAX_PRODUCT_VERSION_LENGTH)


def _protocol_version(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_PROTOCOL_VERSION:
        raise PlanError("INVALID_DOCUMENT", f"{field} must be an integer protocol version")
    return value


def _digest(value: object, field: str) -> str:
    text = _bounded_text(value, field, 71)
    if not DIGEST_PATTERN.fullmatch(text):
        raise PlanError("INVALID_DOCUMENT", f"{field} must be a sha256 digest")
    return text


def _flag(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise PlanError("INVALID_DOCUMENT", f"{field} must be a boolean")
    return value


def _optional_text(
    value: object, field: str, parser: Callable[[object, str], str]
) -> str | None:
    if value is None:
        return None
    return parser(value, field)


def _parse_target(raw: object) -> Target:
    value = _require_object(raw, "target")
    _exact_keys(value, TARGET_KEYS, "target")
    install_root = _posix(value["install_root"], "install_root")
    data_root = _posix(value["data_root"], "data_root")
    if _overlap(install_root, data_root):
        raise PlanError("INVALID_DOCUMENT", "install_root and data_root must be separate")
    return Target(
        os=_linux_os(value["os"], "target.os"),
        install_root=install_root,
        data_root=data_root,
        owner=_owner(value["owner"], "owner"),
        expected_workspace_id=_workspace_id(
            value["expected_workspace_id"], "expected_workspace_id"
        ),
    )


def _parse_artifact(raw: object) -> Artifact:
    value = _require_object(raw, "artifact")
    _exact_keys(value, ARTIFACT_KEYS, "artifact")
    return Artifact(
        product_version=_product_version(value["product_version"], "product_version"),
        protocol_version=_protocol_version(
            value["protocol_version"], "protocol_version"
        ),
        digest=_digest(value["digest"], "digest"),
    )


def _parse_facts(raw: object) -> Facts:
    value = _require_object(raw, "facts")
    _exact_keys(value, FACTS_KEYS, "facts")
    return Facts(
        os=_linux_os(value["os"], "facts.os"),
        python=_parse_python(value["python"]),
        install=_parse_install(value["install"]),
        data=_parse_data(value["data"]),
    )


def _parse_python(raw: object) -> PythonFacts | None:
    if raw is None:
        return None
    value = _require_object(raw, "python")
    _exact_keys(value, PYTHON_KEYS, "python")
    version = _bounded_text(value["version"], "python.version", 16)
    match = PYTHON_VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise PlanError("INVALID_DOCUMENT", "python.version is not a CPython 3.x version")
    return PythonFacts(
        path=_posix(value["path"], "python.path"),
        version=version,
        series=(3, int(match.group(1))),
    )


def _parse_install(raw: object) -> InstallFacts:
    value = _require_object(raw, "install")
    _exact_keys(value, INSTALL_KEYS, "install")
    exists = _flag(value["exists"], "install.exists")
    symlink = _flag(value["symlink"], "install.symlink")
    owner = _optional_text(value["owner"], "install.owner", _owner)
    product = _optional_text(
        value["product_version"], "install.product_version", _product_version
    )
    protocol = _optional_protocol(value["protocol_version"], "install.protocol_version")
    digest = _optional_text(value["digest"], "install.digest", _digest)
    _absent_identity(exists, (owner, product, protocol, digest), "install")
    _absent_symlink(exists, symlink, "install")
    if exists and owner is None:
        raise PlanError("INVALID_DOCUMENT", "install.owner is required when the path exists")
    return InstallFacts(exists, owner, symlink, product, protocol, digest)


def _parse_data(raw: object) -> DataFacts:
    value = _require_object(raw, "data")
    _exact_keys(value, DATA_KEYS, "data")
    exists = _flag(value["exists"], "data.exists")
    symlink = _flag(value["symlink"], "data.symlink")
    owner = _optional_text(value["owner"], "data.owner", _owner)
    workspace = _optional_text(
        value["workspace_id"], "data.workspace_id", _workspace_id
    )
    _absent_identity(exists, (owner, workspace), "data")
    _absent_symlink(exists, symlink, "data")
    if exists and owner is None:
        raise PlanError("INVALID_DOCUMENT", "data.owner is required when the path exists")
    return DataFacts(exists, owner, symlink, workspace)


def _optional_protocol(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _protocol_version(value, field)


def _absent_identity(exists: bool, fields: tuple[object, ...], context: str) -> None:
    if exists:
        return
    if any(field is not None for field in fields):
        raise PlanError(
            "INVALID_DOCUMENT",
            f"{_location(context)} identity fields must be null when the path is missing",
        )


def _absent_symlink(exists: bool, symlink: bool, context: str) -> None:
    if not exists and symlink:
        raise PlanError(
            "INVALID_DOCUMENT",
            f"{_location(context)} symlink must be false when the path is missing",
        )


def _overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def _note(code: str, severity: str, detail: str) -> Diagnostic:
    return Diagnostic(
        _clip_text(code, MAX_CODE_LENGTH),
        severity,
        _clip_text(detail, MAX_DETAIL_LENGTH),
    )


def _collect_diagnostics(
    target: Target, artifact: Artifact, facts: Facts
) -> tuple[Diagnostic, ...]:
    notes: list[Diagnostic] = [
        _note(
            "PLAN_ONLY",
            "info",
            "Results are a local plan. Live remote verification has not run.",
        )
    ]
    notes.extend(_artifact_notes(artifact))
    notes.extend(_python_notes(facts.python))
    notes.extend(_path_notes(target, facts))
    notes.extend(_install_state_notes(facts.install, artifact))
    return tuple(notes)


def _artifact_notes(artifact: Artifact) -> list[Diagnostic]:
    notes: list[Diagnostic] = []
    if artifact.product_version != __version__:
        notes.append(_note("REMOTE_APP_MISMATCH", "error", "artifact product_version is not this Work Stack"))
    if artifact.protocol_version != REMOTE_PROTOCOL_VERSION:
        notes.append(_note("REMOTE_PROTOCOL_INVALID", "error", "artifact protocol_version is not this Work Stack"))
    return notes


def _python_notes(python: PythonFacts | None) -> list[Diagnostic]:
    if python is None:
        return [_note(REMOTE_PYTHON_REQUIRED, "error", "compatible Linux Python was not supplied")]
    if python.series < MIN_PYTHON:
        return [_note(REMOTE_PYTHON_TOO_OLD, "error", "Linux Python must be 3.10 or newer")]
    return [_note("REMOTE_PYTHON_COMPATIBLE", "info", "supplied Linux Python meets the 3.10 service/CLI prerequisite")]


def _path_notes(target: Target, facts: Facts) -> list[Diagnostic]:
    notes: list[Diagnostic] = []
    if facts.install.symlink or facts.data.symlink:
        notes.append(_note("TARGET_REPARSE_AMBIGUITY", "error", "supplied install or data path is a symlink or reparse point"))
    if facts.install.exists and facts.install.owner != target.owner:
        notes.append(_note("TARGET_OWNERSHIP_MISMATCH", "error", "supplied install path owner does not match the per-user target"))
    if facts.data.exists and facts.data.owner != target.owner:
        notes.append(_note("TARGET_OWNERSHIP_MISMATCH", "error", "supplied data path owner does not match the per-user target"))
    notes.extend(_data_workspace_notes(target, facts.data))
    return notes


def _data_workspace_notes(target: Target, data: DataFacts) -> list[Diagnostic]:
    if not data.exists:
        return []
    if data.workspace_id is None:
        return [_note(
            "DATA_STATE_UNKNOWN",
            "error",
            "supplied existing data path has unknown workspace identity; repair evaluation is required",
        )]
    if data.workspace_id != target.expected_workspace_id:
        return [_note("REMOTE_WORKSPACE_MISMATCH", "error", "supplied workspace identity does not match the target")]
    return []


def _install_state_notes(install: InstallFacts, artifact: Artifact) -> list[Diagnostic]:
    if not install.exists:
        return [_note("INSTALL_STATE_MISSING", "info", "supplied facts report no Work Stack app at the install root")]
    if _unknown_install_identity(install):
        return [_note(
            "INSTALL_STATE_UNKNOWN",
            "error",
            "supplied existing install has unknown version, protocol, or digest; repair evaluation is required",
        )]
    if _is_current(install, artifact):
        return [_note("INSTALL_STATE_CURRENT", "info", "supplied app identity matches the artifact")]
    return [_note(
        "INSTALL_CONTENT_CONFLICT",
        "error",
        "supplied existing install conflicts with the artifact; repair evaluation is required",
    )]


def _unknown_install_identity(install: InstallFacts) -> bool:
    return (
        install.product_version is None
        or install.protocol_version is None
        or install.digest is None
    )


def _is_current(install: InstallFacts, artifact: Artifact) -> bool:
    if (
        not install.exists
        or install.symlink
        or install.product_version != artifact.product_version
        or install.protocol_version != artifact.protocol_version
        or install.digest is None
        or len(install.digest) != len(artifact.digest)
    ):
        return False
    return secrets.compare_digest(install.digest, artifact.digest)


def _decision(
    diagnostics: tuple[Diagnostic, ...], install: InstallFacts, artifact: Artifact
) -> tuple[str, str]:
    codes = {item.code for item in diagnostics if item.severity == "error"}
    if REMOTE_PYTHON_REQUIRED in codes or REMOTE_PYTHON_TOO_OLD in codes:
        return "prerequisite_missing", "none"
    if codes:
        return "refused", "none"
    if _is_current(install, artifact):
        return "current", "noop"
    return "install_needed", "install"

"""Offline JSON Schema validation for normalized SSOT artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


DEFAULT_SCHEMA_ROOT = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "workstack-ssot-v4"
    / "schemas"
)

SCHEMA_BY_FORMAT = {
    "workstack.ssot": "store.schema.json",
    "workstack.workspace": "workspace.schema.json",
    "workstack.task": "task.schema.json",
    "workstack.objective": "objective.schema.json",
    "workstack.capture": "capture.schema.json",
    "workstack.reply": "reply.schema.json",
    "workstack.note": "note.schema.json",
    "workstack.planning-status-event": "planning-status-event.schema.json",
    "workstack.activity-event": "activity-event.schema.json",
    "workstack.worklog-event": "worklog-event.schema.json",
    "workstack.migration-receipt": "migration-receipt.schema.json",
    "workstack.idempotency-ledger": "idempotency-ledger.schema.json",
}


class StorageContractError(ValueError):
    """Raised when bundled schemas cannot be trusted or selected."""


@dataclass(frozen=True)
class ContractViolation:
    """Content-free location and keyword for one rejected instance."""

    code: str
    instance_path: str
    schema_path: str


def _pointer(parts: object) -> str:
    encoded = []
    for part in parts:
        text = str(part).replace("~", "~0").replace("/", "~1")
        encoded.append(text)
    return "/" + "/".join(encoded) if encoded else ""


def _read_schema(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StorageContractError(f"schema artifact is unreadable: {path.name}") from error
    if not isinstance(value, dict):
        raise StorageContractError(f"schema artifact is not an object: {path.name}")
    try:
        Draft202012Validator.check_schema(value)
    except Exception as error:
        raise StorageContractError(f"schema artifact is invalid: {path.name}") from error
    return value


@lru_cache(maxsize=4)
def load_schema_catalog(schema_root: Path = DEFAULT_SCHEMA_ROOT) -> Mapping[str, dict[str, Any]]:
    """Load and meta-validate one complete offline schema catalog."""

    root = Path(schema_root).resolve()
    catalog: dict[str, dict[str, Any]] = {}
    try:
        paths = sorted(root.glob("*.schema.json"))
    except OSError as error:
        raise StorageContractError("schema directory is unreadable") from error
    if not paths:
        raise StorageContractError("schema catalog is empty")
    for path in paths:
        schema = _read_schema(path)
        identifier = schema.get("$id")
        if not isinstance(identifier, str) or not identifier:
            raise StorageContractError(f"schema artifact has no identifier: {path.name}")
        if identifier in catalog:
            raise StorageContractError("schema catalog contains a duplicate identifier")
        catalog[identifier] = schema
    return catalog


def _registry(catalog: Mapping[str, dict[str, Any]]) -> Registry[Any]:
    resources = (
        (identifier, Resource.from_contents(schema))
        for identifier, schema in catalog.items()
    )
    return Registry().with_resources(resources)


def _schema_for_name(
    schema_name: str,
    catalog: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    for schema in catalog.values():
        if str(schema.get("$id", "")).endswith("/" + schema_name):
            return schema
    raise StorageContractError(f"unknown schema artifact: {schema_name}")


def validate_instance(
    schema_name: str,
    instance: Any,
    *,
    schema_root: Path = DEFAULT_SCHEMA_ROOT,
) -> tuple[ContractViolation, ...]:
    """Return deterministic, content-free violations for one JSON instance."""

    catalog = load_schema_catalog(Path(schema_root).resolve())
    schema = _schema_for_name(schema_name, catalog)
    validator = Draft202012Validator(
        schema,
        registry=_registry(catalog),
        format_checker=FormatChecker(),
    )
    violations = [
        ContractViolation(
            code=str(error.validator or "schema"),
            instance_path=_pointer(error.absolute_path),
            schema_path=_pointer(error.absolute_schema_path),
        )
        for error in validator.iter_errors(instance)
    ]
    return tuple(sorted(violations, key=lambda item: (item.instance_path, item.schema_path, item.code)))


def validate_by_format(
    instance: Any,
    *,
    schema_root: Path = DEFAULT_SCHEMA_ROOT,
) -> tuple[ContractViolation, ...]:
    """Select a concrete schema from an instance's explicit format value."""

    if not isinstance(instance, dict):
        return (ContractViolation("type", "", ""),)
    format_name = instance.get("format")
    if not isinstance(format_name, str) or format_name not in SCHEMA_BY_FORMAT:
        return (ContractViolation("unsupported_format", "/format", ""),)
    return validate_instance(
        SCHEMA_BY_FORMAT[format_name],
        instance,
        schema_root=schema_root,
    )


def require_valid_by_format(
    instance: Any,
    *,
    schema_root: Path = DEFAULT_SCHEMA_ROOT,
) -> None:
    """Raise without retaining instance content when a contract is rejected."""

    violations = validate_by_format(instance, schema_root=schema_root)
    if violations:
        first = violations[0]
        raise StorageContractError(
            f"storage contract violation: {first.code} at {first.instance_path or '/'}"
        )

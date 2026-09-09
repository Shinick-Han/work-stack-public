"""Bounded document and identity vocabulary read by the remote collector.

Two independent identities live here. The data root's identity is the
workspace UUID admitted only behind a well formed ``store-meta.json`` whose
migration evidence is complete. The application root's identity is the
installed ``workstack/__init__.py`` literal pair plus, separately, the
canonical ``.workstack-install.json`` receipt with its frozen runtime target.
Every function is pure: it decides from bytes already read and never touches
the filesystem, so an unreadable or malformed document projects as unknown.
"""

from __future__ import annotations

import ast
import json
import uuid

from remote_provision_contract import (
    DIGEST_PATTERN,
    MAX_PRODUCT_VERSION_LENGTH,
    MAX_PROTOCOL_VERSION,
    OWNER_PATTERN,
)


RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "product_version",
        "remote_protocol_version",
        "artifact_digest",
        "artifact_manifest_sha256",
        "workspace_uid",
        "owner",
        "target",
    }
)
FROZEN_TARGET_JSON = (
    b'{"glibc_min":[2,17],"implementation":"cpython","libc":"glibc",'
    b'"machine":"x86_64","os":"linux","python_major":3,"python_minor":12,'
    b'"python_tag":"cp312","soabi":"cpython-312-x86_64-linux-gnu",'
    b'"wheel_platform":"manylinux_2_17_x86_64"}'
)
STORE_META_KEYS = frozenset({"version", "store_schema_version", "migrations"})
MIGRATION_KEYS = frozenset({"identity", "planning_status"})
EVIDENCE_KEYS = frozenset({"id", "origin", "source_sha256"})


def _unique_object(pairs: list[tuple[object, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in document:
            raise ValueError("duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(value: str) -> object:
    raise ValueError("invalid JSON constant")


def _object_from_bytes(payload: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError, MemoryError):
        return None
    if type(value) is not dict:
        return None
    return value


def _evidence_record(value: object, *, planning: bool = False, reports: bool = False, knowledge: bool = False) -> bool:
    if type(value) is not dict or set(value) != EVIDENCE_KEYS:
        return False
    if type(value["id"]) is not str or type(value["origin"]) is not str:
        return False
    digest, origin = value["source_sha256"], value["origin"]
    if knowledge:
        expected_id = "workstack.knowledge.v6" if origin == "fresh" else "workstack.knowledge.v5-to-v6"
        migrated_origins = {"migrated_v1", "migrated_v2", "migrated_v3", "migrated_v5"}
    elif reports:
        expected_id = "workstack.reports.v5" if origin == "fresh" else "workstack.reports.v3-to-v5"
        migrated_origins = {"migrated_v1", "migrated_v2", "migrated_v3"}
    elif planning:
        expected_id = "workstack.planning-status.v1"
        migrated_origins = {"migrated_v1", "migrated_v2"}
    else:
        expected_id = "workstack.store.v2" if origin == "fresh" else "workstack.store.v1-to-v2"
        migrated_origins = {"migrated_v1"}
    if value["id"] != expected_id:
        return False
    if origin == "fresh":
        return digest is None
    return origin in migrated_origins and type(digest) is str and DIGEST_PATTERN.fullmatch(digest) is not None


def _well_formed_store_meta(value: object) -> bool:
    if type(value) is not dict or set(value) != STORE_META_KEYS:
        return False
    if type(value["version"]) is not int or value["version"] != 2:
        return False
    schema, migrations = value["store_schema_version"], value["migrations"]
    keys = {3: MIGRATION_KEYS, 5: MIGRATION_KEYS | {"reports"}, 6: MIGRATION_KEYS | {"reports", "knowledge"}}
    if type(schema) is not int or schema not in keys or type(migrations) is not dict or set(migrations) != keys[schema]:
        return False
    identity = _evidence_record(migrations["identity"])
    planning = _evidence_record(migrations["planning_status"], planning=True)
    reports = schema == 3 or _evidence_record(migrations["reports"], reports=True)
    return identity and planning and reports and (
        schema != 6 or _evidence_record(migrations["knowledge"], knowledge=True)
    )


def _canonical_uid(text: object) -> str | None:
    if type(text) is not str:
        return None
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError):
        return None
    if text != str(parsed) or parsed.int == 0 or parsed.variant != uuid.RFC_4122:
        return None
    return text


def _workspace_uid(value: object) -> str | None:
    if type(value) is not dict:
        return None
    return _canonical_uid(value.get("id"))


def _assign_constants(tree: ast.AST) -> dict[str, object]:
    constants: dict[str, object] = {}
    wanted = {"__version__", "REMOTE_PROTOCOL_VERSION"}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in wanted and node.value is not None:
                    try:
                        constants[target.id] = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError, TypeError, MemoryError):
                        return {}
    return constants


def _literal_identity(payload: bytes) -> tuple[str | None, int | None]:
    try:
        tree = ast.parse(payload.decode("utf-8"))
        constants = _assign_constants(tree)
    except (UnicodeError, SyntaxError, ValueError, RecursionError, MemoryError):
        return None, None
    version = constants.get("__version__")
    protocol = constants.get("REMOTE_PROTOCOL_VERSION")
    if (
        not isinstance(version, str)
        or not version
        or len(version) > MAX_PRODUCT_VERSION_LENGTH
        or any(ord(character) < 32 for character in version)
    ):
        return None, None
    if type(protocol) is not int or not 0 <= protocol <= MAX_PROTOCOL_VERSION:
        return None, None
    return version, protocol


def _frozen_target_ok(value: object) -> bool:
    if type(value) is not dict:
        return False
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, MemoryError):
        return False
    return encoded == FROZEN_TARGET_JSON


def _parse_canonical_receipt(payload: bytes) -> dict[str, object] | None:
    if payload.startswith(b"\xef\xbb\xbf") or b"\r" in payload:
        return None
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        return None
    value = _object_from_bytes(payload)
    if value is None:
        return None
    try:
        encoded = json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, RecursionError, MemoryError):
        return None
    if encoded != payload or set(value) != RECEIPT_KEYS:
        return None
    return value if _receipt_fields_valid(value) else None


def _receipt_fields_valid(value: dict[str, object]) -> bool:
    digest = value["artifact_digest"]
    manifest = value["artifact_manifest_sha256"]
    return (
        type(value["schema_version"]) is int and value["schema_version"] == 1
        and type(value["product_version"]) is str and bool(value["product_version"])
        and type(value["remote_protocol_version"]) is int
        and type(digest) is str and DIGEST_PATTERN.fullmatch(digest) is not None
        and type(manifest) is str and DIGEST_PATTERN.fullmatch(manifest) is not None
        and _canonical_uid(value["workspace_uid"]) is not None
        and type(value["owner"]) is str and OWNER_PATTERN.fullmatch(value["owner"]) is not None
        and _frozen_target_ok(value["target"])
    )

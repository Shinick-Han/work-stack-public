"""Strict JSON contract for the desktop local-knowledge host bridge.

This module does not persist vaults or references. Persistence belongs to
``knowledge_registry``; this host decodes bounded requests, checks the active
workspace before and after each worker operation, and calls registry methods.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SHELL_DIRECTORY = Path(__file__).resolve().parent
_APPLICATION_ROOT = _SHELL_DIRECTORY.parents[1]
for _import_root in (_SHELL_DIRECTORY, _APPLICATION_ROOT):
    if str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from knowledge_host_search import (
    MAX_CORPUS_LABEL_CHARS,
    MAX_DOCUMENT_COUNT,
    MAX_QUERY_CHARS,
    MAX_SEARCH_MATCHES,
    SearchContractError,
    parse_query,
    public_search,
)
from workstack.knowledge_context import task_binding
from workstack.knowledge_reference import (
    MAX_EXCERPT_CHARS,
    KnowledgeReferenceError,
)

HOST_CONTRACT_VERSION = 1
MAX_HOST_REQUEST_BYTES = 16 * 1024
MAX_HOST_RESPONSE_BYTES = 128 * 1024
MAX_REASON_CHARS = 500
# ``knowledge_registry`` stores a reason as safe bounded text: line breaks and
# tabs are layout, every other control character stays refused.
_REASON_LAYOUT = "\n\t"
HOST_REQUEST_TYPE = "workstack-knowledge-request"
HOST_RESPONSE_TYPE = "workstack-knowledge-response"

STATUS = "status"
CHOOSE_VAULT = "choose-vault"
LIST_REFERENCES = "list-references"
READ_REFERENCE = "read-reference"
PIN_REFERENCE = "pin-reference"
UNPIN_REFERENCE = "unpin-reference"
SEARCH_REFERENCES = "search-references"
_HOST_OPERATIONS = frozenset(
    {
        STATUS,
        CHOOSE_VAULT,
        LIST_REFERENCES,
        READ_REFERENCE,
        PIN_REFERENCE,
        UNPIN_REFERENCE,
        SEARCH_REFERENCES,
    }
)
_BINDING_REQUIRED = frozenset(
    {
        LIST_REFERENCES,
        READ_REFERENCE,
        PIN_REFERENCE,
        UNPIN_REFERENCE,
        SEARCH_REFERENCES,
    }
)
_ENVELOPE_FIELDS = frozenset({"type", "schema_version", "request_id", "operation"})
_BINDING_FIELDS = frozenset(
    {"workspace_uid", "task_uid", "task_id", "task_revision"}
)
_VAULT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_SOURCE_SHA = re.compile(r"[0-9a-f]{64}")
_CLOSED_CODE = re.compile(r"[a-z][a-z0-9_]{0,62}")
_TYPE_PATTERN = re.compile(r'"type"\s*:\s*"workstack-knowledge-request"')
_REQUEST_ID_PATTERN = re.compile(
    r'"request_id"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"'
)
_OPERATION_PATTERN = re.compile(
    r'"operation"\s*:\s*"(status|choose-vault|list-references|read-reference'
    r'|pin-reference|unpin-reference|search-references)"'
)
_MISSING = object()
_SAFE_MESSAGES = {
    "busy": "Knowledge host is busy. Try again shortly.",
    "document_empty": "The referenced document is empty.",
    "document_missing": "The referenced document is not available.",
    "document_too_large": "The referenced document exceeds the allowed size.",
    "document_unavailable": "The referenced document could not be read.",
    "document_unreadable": "The referenced document could not be read.",
    "internal_error": "Knowledge operation could not be completed.",
    "invalid_document_path": "The document path is not a permitted relative Markdown path.",
    "invalid_line_range": "The requested line range is invalid.",
    "invalid_query": "The search query is invalid.",
    "invalid_request": "Knowledge request is invalid.",
    "invalid_source_revision": "The expected source revision is invalid.",
    "invalid_task_export": "The Task binding is invalid.",
    "invalid_task_identity": "The Task binding identity is invalid.",
    "invalid_task_revision": "The Task revision is invalid.",
    "invalid_text": "The referenced document is not valid text.",
    "invalid_utf8": "The referenced document is not valid UTF-8.",
    "invalid_vault_id": "The vault identity is invalid.",
    "line_range_unavailable": "The requested line range is not present in the document.",
    "linked_path_refused": "Linked or reparse paths are refused.",
    "markdown_required": "Only Markdown documents can be referenced.",
    "no_active_workspace": "No active workspace is available for knowledge operations.",
    "operation_failed": "Knowledge operation failed.",
    "outside_vault": "The document path is outside the chosen vault.",
    "registry_unavailable": "Knowledge registry is unavailable.",
    "regular_file_required": "The document must be a regular file.",
    "search_invalid_response": "The search provider returned an unusable response.",
    "search_timeout": "The related-document search took too long and was stopped.",
    "search_unavailable": "The related-document search provider is unavailable.",
    "search_unconfigured": "Related-document search is not configured on this device.",
    "source_changed_during_read": "The source document changed during read.",
    "source_revision_conflict": "The source document changed since the expected revision.",
    "vault_unavailable": "The chosen vault is not available.",
    "workspace_mismatch": "The knowledge request does not match the active workspace.",
}


class KnowledgeHostClosedError(Exception):
    """Closed refusal without source text or filesystem roots."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or _safe_message(code)
        super().__init__(self.code)


def _safe_message(code: str) -> str:
    return _SAFE_MESSAGES.get(code, "Knowledge operation was refused.")


def _try_default_registry(state_root: Path) -> object | None:
    try:
        from knowledge_registry import KnowledgeRegistry
    except ImportError:
        return None
    return KnowledgeRegistry(state_root)


def is_knowledge_host_message(message: str) -> bool:
    if not isinstance(message, str) or not message.lstrip().startswith("{"):
        return False
    try:
        message.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return bool(_TYPE_PATTERN.search(message[:4096]))


def correlate_knowledge_request(message: str) -> tuple[str, str] | None:
    if not isinstance(message, str) or len(message.encode("utf-8")) > MAX_HOST_REQUEST_BYTES:
        return None
    prefix = message[:4096]
    if len(_TYPE_PATTERN.findall(prefix)) != 1:
        return None
    request_ids = _REQUEST_ID_PATTERN.findall(prefix)
    operations = _OPERATION_PATTERN.findall(prefix)
    if len(request_ids) != 1 or len(operations) != 1:
        return None
    try:
        parsed = uuid.UUID(request_ids[0])
    except ValueError:
        return None
    request_id = str(parsed)
    if request_id != request_ids[0] or parsed.int == 0:
        return None
    return request_id, operations[0]


def encode_knowledge_error_response(
    request_id: str | None,
    operation: str | None,
    code: str,
    message: str,
) -> str:
    if (request_id is None) != (operation is None):
        request_id, operation = None, None
    document = {
        "type": HOST_RESPONSE_TYPE,
        "schema_version": HOST_CONTRACT_VERSION,
        "request_id": None if request_id is None else _canonical_uuid(request_id),
        "operation": None if operation is None else _require_operation(operation),
        "ok": False,
        "error": {
            "code": _bounded_token(code, 64),
            "message": _bounded_error_message(message),
        },
    }
    return json.dumps(document, ensure_ascii=True, separators=(",", ":"))


class KnowledgeHostService:
    """Dispatch bounded knowledge operations to an injected or default registry."""

    def __init__(
        self,
        state_root: Path,
        directory_picker: Callable[[], str | Path | None],
        current_workspace_uid: Callable[[], str | None] | str | None,
        *,
        registry: object | None = _MISSING,  # type: ignore[assignment]
    ) -> None:
        self._state_root = Path(state_root)
        self._directory_picker = directory_picker
        if callable(current_workspace_uid):
            self._current_workspace_uid = current_workspace_uid
        elif current_workspace_uid is None:
            self._current_workspace_uid = lambda: None
        else:
            uid = current_workspace_uid
            self._current_workspace_uid = lambda: uid
        if registry is _MISSING:
            self._registry = _try_default_registry(self._state_root)
        else:
            self._registry = registry

    def overflow_response(self, payload: str) -> str:
        correlation = correlate_knowledge_request(payload)
        if correlation is None:
            return encode_knowledge_error_response(
                None,
                None,
                "invalid_request",
                _safe_message("invalid_request"),
            )
        request_id, operation = correlation
        return encode_knowledge_error_response(
            request_id, operation, "busy", _safe_message("busy")
        )

    def handle_json(self, payload: str) -> str:
        try:
            request = _decode_request(payload)
        except KnowledgeHostClosedError as error:
            correlation = correlate_knowledge_request(
                payload if isinstance(payload, str) else ""
            )
            request_id, operation = correlation if correlation is not None else (None, None)
            return encode_knowledge_error_response(
                request_id, operation, error.code, error.message
            )
        try:
            data = self._execute(request)
            return _encode_success(request["request_id"], request["operation"], data)
        except KnowledgeHostClosedError as error:
            return encode_knowledge_error_response(
                request["request_id"],
                request["operation"],
                error.code,
                error.message,
            )
        except Exception as error:
            closed = _closed_from_exception(error)
            if closed is not None:
                code, message = closed
                return encode_knowledge_error_response(
                    request["request_id"], request["operation"], code, message
                )
            return encode_knowledge_error_response(
                request["request_id"],
                request["operation"],
                "operation_failed",
                _safe_message("operation_failed"),
            )

    def _execute(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request["operation"]
        binding = request.get("binding")
        if operation == STATUS:
            return self._status(binding)
        if operation == CHOOSE_VAULT:
            return self._choose_vault(binding)
        if operation == LIST_REFERENCES:
            return self._list_references(binding)
        if operation == READ_REFERENCE:
            return self._read_reference(request, binding)
        if operation == PIN_REFERENCE:
            return self._pin_reference(request, binding)
        if operation == UNPIN_REFERENCE:
            return self._unpin_reference(request, binding)
        if operation == SEARCH_REFERENCES:
            return self._search_references(request, binding)
        raise KnowledgeHostClosedError("invalid_request")

    def _status(self, binding: dict[str, str | int] | None) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            registry = self._require_registry()
            raw = registry.status()
            vaults = raw.get("vaults") if isinstance(raw, dict) else raw
            if not isinstance(vaults, list):
                raise KnowledgeHostClosedError("operation_failed")
            return {
                "vaults": [_public_vault(item) for item in vaults],
                "local_only": True,
            }

        return self._guarded(binding, operation)

    def _choose_vault(self, binding: dict[str, str | int] | None) -> dict[str, Any]:
        if binding is not None:
            self._assert_workspace(binding, required=True)
        try:
            selected = self._directory_picker()
        except Exception as error:
            raise KnowledgeHostClosedError("operation_failed") from error
        if _picker_cancelled(selected):
            # The picker can switch the active workspace before cancelling, so
            # a cancelled choice is guarded exactly like a completed one.
            if binding is not None:
                self._assert_workspace(binding, required=True)
            return {"cancelled": True}

        def operation() -> dict[str, Any]:
            registry = self._require_registry()
            vault = registry.add_vault(selected)
            return {"cancelled": False, "vault": _public_vault(vault)}

        return self._guarded(binding, operation)

    def _list_references(self, binding: dict[str, str | int]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            registry = self._require_registry()
            references = registry.list_references(binding)
            if not isinstance(references, list):
                raise KnowledgeHostClosedError("operation_failed")
            return {
                "binding": dict(binding),
                "references": [_public_metadata(item) for item in references],
                "local_only": True,
            }

        return self._guarded(binding, operation)

    def _read_reference(
        self, request: dict[str, Any], binding: dict[str, str | int]
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            registry = self._require_registry()
            reference = registry.read_reference(
                binding,
                request["vault_id"],
                request["document_path"],
                request["start_line"],
                request["end_line"],
                request.get("expected_sha256"),
            )
            return {
                "binding": dict(binding),
                "reference": _public_read_reference(reference),
            }

        return self._guarded(binding, operation)

    def _pin_reference(
        self, request: dict[str, Any], binding: dict[str, str | int]
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            registry = self._require_registry()
            metadata = registry.pin_reference(
                binding,
                request["vault_id"],
                request["document_path"],
                request["start_line"],
                request["end_line"],
                request["expected_sha256"],
                request["reason"],
            )
            return {
                "binding": dict(binding),
                "reference": _public_metadata(metadata),
                "local_only": True,
            }

        return self._guarded(binding, operation)

    def _unpin_reference(
        self, request: dict[str, Any], binding: dict[str, str | int]
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            registry = self._require_registry()
            registry.unpin_reference(binding, request["reference_id"])
            return {
                "binding": dict(binding),
                "removed": True,
                "local_only": True,
            }

        return self._guarded(binding, operation)

    def _search_references(
        self, request: dict[str, Any], binding: dict[str, str | int]
    ) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            registry = self._require_registry()
            result = registry.search_references(
                binding, request["vault_id"], request["query"]
            )
            return public_search(
                binding, request["query"], result, read_reference=_public_read_reference
            )

        return self._guarded(binding, operation)

    def _require_registry(self) -> Any:
        if self._registry is None:
            raise KnowledgeHostClosedError("registry_unavailable")
        return self._registry

    def _guarded(
        self,
        binding: dict[str, str | int] | None,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        required = binding is not None
        self._assert_workspace(binding, required=required)
        result = operation()
        self._assert_workspace(binding, required=required)
        return result

    def _assert_workspace(
        self, binding: dict[str, str | int] | None, *, required: bool
    ) -> None:
        if binding is None:
            return
        current = self._current_workspace_uid()
        requested = binding["workspace_uid"]
        if not isinstance(current, str) or not current:
            raise KnowledgeHostClosedError(
                "no_active_workspace" if required else "workspace_mismatch"
            )
        if current != requested:
            raise KnowledgeHostClosedError("workspace_mismatch")


def _decode_request(payload: object) -> dict[str, Any]:
    text = _bounded_request_text(payload)
    try:
        raw = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_members,
        )
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise KnowledgeHostClosedError("invalid_request") from error
    if not isinstance(raw, dict):
        raise KnowledgeHostClosedError("invalid_request")
    try:
        request_id = _validate_envelope(raw)
        operation = raw.get("operation")
        if operation not in _HOST_OPERATIONS:
            raise KnowledgeHostClosedError("invalid_request")
        _require_exact_fields(raw, _allowed_fields(operation, raw))
        request: dict[str, Any] = {
            "request_id": request_id,
            "operation": operation,
        }
        if "binding" in raw or operation in _BINDING_REQUIRED:
            request["binding"] = _parse_binding(raw.get("binding"))
        request.update(_operation_fields(operation, raw))
        return request
    except KnowledgeHostClosedError:
        raise
    except (KnowledgeReferenceError, SearchContractError) as error:
        raise KnowledgeHostClosedError(error.code) from error
    except (TypeError, ValueError, KeyError) as error:
        raise KnowledgeHostClosedError("invalid_request") from error


def _operation_fields(operation: str, raw: dict[object, object]) -> dict[str, Any]:
    """Decode exactly the members one operation adds to the envelope."""

    fields: dict[str, Any] = {}
    if operation in {READ_REFERENCE, PIN_REFERENCE}:
        fields.update(_span_fields(raw))
    if operation == READ_REFERENCE and "expected_sha256" in raw:
        fields["expected_sha256"] = _parse_expected_sha256(
            raw.get("expected_sha256"), required=False
        )
    if operation == PIN_REFERENCE:
        fields["expected_sha256"] = _parse_expected_sha256(
            raw.get("expected_sha256"), required=True
        )
        fields["reason"] = _parse_reason(raw.get("reason"))
    if operation == UNPIN_REFERENCE:
        fields["reference_id"] = _canonical_uuid(raw.get("reference_id"))
    if operation == SEARCH_REFERENCES:
        fields["vault_id"] = _parse_vault_id(raw.get("vault_id"))
        fields["query"] = parse_query(raw.get("query"))
    return fields


def _span_fields(raw: dict[object, object]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "vault_id": _parse_vault_id(raw.get("vault_id")),
        "document_path": _parse_document_path(raw.get("document_path")),
        "start_line": _parse_line(raw.get("start_line"), "start_line"),
        "end_line": _parse_line(raw.get("end_line"), "end_line"),
    }
    if fields["end_line"] < fields["start_line"]:
        raise KnowledgeHostClosedError("invalid_line_range")
    return fields


def _allowed_fields(operation: str, raw: dict[object, object]) -> set[str]:
    allowed = set(_ENVELOPE_FIELDS)
    if operation in _BINDING_REQUIRED or "binding" in raw:
        allowed.add("binding")
    if operation == READ_REFERENCE:
        allowed.update({"vault_id", "document_path", "start_line", "end_line"})
        if "expected_sha256" in raw:
            allowed.add("expected_sha256")
    elif operation == PIN_REFERENCE:
        allowed.update(
            {
                "vault_id",
                "document_path",
                "start_line",
                "end_line",
                "expected_sha256",
                "reason",
            }
        )
    elif operation == UNPIN_REFERENCE:
        allowed.add("reference_id")
    elif operation == SEARCH_REFERENCES:
        allowed.update({"vault_id", "query"})
    return allowed


def _validate_envelope(raw: dict[object, object]) -> str:
    if raw.get("type") != HOST_REQUEST_TYPE:
        raise KnowledgeHostClosedError("invalid_request")
    version = raw.get("schema_version")
    if isinstance(version, bool) or version != HOST_CONTRACT_VERSION:
        raise KnowledgeHostClosedError("invalid_request")
    return _canonical_uuid(raw.get("request_id"))


def _picker_cancelled(selected: object) -> bool:
    if selected is None:
        return True
    if isinstance(selected, (str, Path)):
        return not str(selected).strip()
    return False


def _parse_binding(value: object) -> dict[str, str | int]:
    if not isinstance(value, dict):
        raise KnowledgeHostClosedError("invalid_request")
    _require_exact_fields(value, _BINDING_FIELDS)
    try:
        return task_binding(
            {
                "id": value["task_id"],
                "uid": value["task_uid"],
                "revision": value["task_revision"],
            },
            value["workspace_uid"],
        )
    except KnowledgeReferenceError as error:
        raise KnowledgeHostClosedError(error.code) from error


def _parse_vault_id(value: object) -> str:
    if not isinstance(value, str) or not _VAULT_ID.fullmatch(value):
        raise KnowledgeHostClosedError("invalid_vault_id")
    return value


def _parse_document_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise KnowledgeHostClosedError("invalid_document_path")
    if any(ord(character) < 32 for character in value):
        raise KnowledgeHostClosedError("invalid_document_path")
    return value


def _parse_line(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise KnowledgeHostClosedError("invalid_line_range")
    return value


def _parse_expected_sha256(value: object, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise KnowledgeHostClosedError("invalid_source_revision")
        return None
    if not isinstance(value, str) or not _SOURCE_SHA.fullmatch(value):
        raise KnowledgeHostClosedError("invalid_source_revision")
    return value


def _parse_reason(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_REASON_CHARS:
        raise KnowledgeHostClosedError("invalid_request")
    if any(
        ord(character) < 32 and character not in _REASON_LAYOUT
        for character in value
    ):
        raise KnowledgeHostClosedError("invalid_request")
    return value


def _public_vault(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise KnowledgeHostClosedError("operation_failed")
    vault_id = value.get("vault_id")
    label = value.get("label")
    if not isinstance(vault_id, str) or not _VAULT_ID.fullmatch(vault_id):
        raise KnowledgeHostClosedError("invalid_vault_id")
    if not isinstance(label, str) or not label or len(label) > 255:
        raise KnowledgeHostClosedError("operation_failed")
    if any(separator in label for separator in "/\\") or any(
        ord(character) < 32 for character in label
    ):
        raise KnowledgeHostClosedError("operation_failed")
    return {"vault_id": vault_id, "label": label}


def _public_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise KnowledgeHostClosedError("operation_failed")
    document: dict[str, object] = {
        "reference_id": _canonical_uuid(value.get("reference_id")),
        "vault_id": _parse_vault_id(value.get("vault_id")),
        "document_path": _parse_document_path(value.get("document_path")),
        "start_line": _parse_line(value.get("start_line"), "start_line"),
        "end_line": _parse_line(value.get("end_line"), "end_line"),
        "source_sha256": _parse_expected_sha256(
            value.get("source_sha256"), required=True
        ),
        "reason": _parse_reason(value.get("reason")),
    }
    if document["end_line"] < document["start_line"]:
        raise KnowledgeHostClosedError("operation_failed")
    return document


def _public_read_reference(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise KnowledgeHostClosedError("operation_failed")
    excerpt = value.get("excerpt")
    if not isinstance(excerpt, str) or len(excerpt) > MAX_EXCERPT_CHARS:
        raise KnowledgeHostClosedError("operation_failed")
    title = value.get("title")
    if not isinstance(title, str) or not title or len(title) > 240:
        raise KnowledgeHostClosedError("operation_failed")
    freshness = value.get("freshness")
    if freshness not in {"uncompared", "unchanged", "changed"}:
        raise KnowledgeHostClosedError("operation_failed")
    schema_version = value.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise KnowledgeHostClosedError("operation_failed")
    document = {
        "schema_version": 1,
        "provider": value.get("provider"),
        "vault_id": _parse_vault_id(value.get("vault_id")),
        "document_path": _parse_document_path(value.get("document_path")),
        "title": title,
        "source_sha256": _parse_expected_sha256(
            value.get("source_sha256"), required=True
        ),
        "expected_sha256": _parse_expected_sha256(
            value.get("expected_sha256"), required=False
        ),
        "freshness": freshness,
        "start_line": _parse_line(value.get("start_line"), "start_line"),
        "end_line": _parse_line(value.get("end_line"), "end_line"),
        "excerpt": excerpt,
        "excerpt_truncated": value.get("excerpt_truncated"),
        "trust": value.get("trust"),
        "read_only": value.get("read_only"),
    }
    if document["provider"] != "markdown-vault":
        raise KnowledgeHostClosedError("operation_failed")
    if document["trust"] != "external_reference":
        raise KnowledgeHostClosedError("operation_failed")
    if document["read_only"] is not True:
        raise KnowledgeHostClosedError("operation_failed")
    if document["excerpt_truncated"] not in {True, False}:
        raise KnowledgeHostClosedError("operation_failed")
    if document["end_line"] < document["start_line"]:
        raise KnowledgeHostClosedError("operation_failed")
    return document


def _closed_from_exception(error: BaseException) -> tuple[str, str] | None:
    code = getattr(error, "code", None)
    if isinstance(code, str) and _CLOSED_CODE.fullmatch(code):
        return code, _safe_message(code)
    return None


def _encode_success(request_id: str, operation: str, data: dict[str, Any]) -> str:
    document = {
        "type": HOST_RESPONSE_TYPE,
        "schema_version": HOST_CONTRACT_VERSION,
        "request_id": _canonical_uuid(request_id),
        "operation": _require_operation(operation),
        "ok": True,
        "data": data,
    }
    encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_HOST_RESPONSE_BYTES:
        return encode_knowledge_error_response(
            request_id,
            operation,
            "operation_failed",
            _safe_message("operation_failed"),
        )
    return encoded


def _bounded_request_text(payload: object) -> str:
    if isinstance(payload, bytes):
        if len(payload) > MAX_HOST_REQUEST_BYTES:
            raise KnowledgeHostClosedError("invalid_request")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise KnowledgeHostClosedError("invalid_request") from error
    if not isinstance(payload, str):
        raise KnowledgeHostClosedError("invalid_request")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as error:
        raise KnowledgeHostClosedError("invalid_request") from error
    if len(encoded) > MAX_HOST_REQUEST_BYTES:
        raise KnowledgeHostClosedError("invalid_request")
    return payload


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise KnowledgeHostClosedError("invalid_request")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise KnowledgeHostClosedError("invalid_request") from error
    if value != str(parsed) or parsed.int == 0:
        raise KnowledgeHostClosedError("invalid_request")
    return value


def _require_exact_fields(raw: dict[object, object], required: set[str]) -> None:
    if set(raw) != required:
        raise KnowledgeHostClosedError("invalid_request")


def _require_operation(value: object) -> str:
    if value not in _HOST_OPERATIONS:
        raise KnowledgeHostClosedError("invalid_request")
    return value  # type: ignore[return-value]


def _bounded_token(value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise KnowledgeHostClosedError("operation_failed")
    if any(ord(character) < 32 for character in value):
        raise KnowledgeHostClosedError("operation_failed")
    return value


def _bounded_error_message(value: object) -> str:
    message = _bounded_token(value, 256)
    if any(marker in message for marker in ("\\", "/", ":\\")):
        return _safe_message("operation_failed")
    return message


def _reject_json_constant(value: str) -> None:
    raise ValueError("JSON constants are not allowed")


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse ambiguous objects at every depth; the last member never wins."""
    members: dict[str, Any] = {}
    for key, value in pairs:
        if key in members:
            raise ValueError("duplicate JSON object member")
        members[key] = value
    return members

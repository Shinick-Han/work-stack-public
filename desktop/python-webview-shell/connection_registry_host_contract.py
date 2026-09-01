"""Strict JSON contract for the desktop connection-registry host bridge.

This module is intentionally independent from the WebView lifecycle.  It
accepts only versioned registry operations, delegates persistence to
``connection_registry``, and delegates read-only alias discovery to
``ssh_config_discovery``.  Bounded profile paths are accepted because they are
the configuration being edited; credentials, SSH config paths, OpenSSH
arguments, and Work Stack planning content are never accepted from the web
payload.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal, TypeAlias

from connection_registry import (
    ConnectionRegistry,
    load_connection_registry,
    registry_from_document,
    registry_to_document,
    save_connection_registry,
)
from connection_registry_mutations import (
    ActivationProofError,
    ConnectionRegistryMutationService,
    RegistryConflictError,
    current_registry_snapshot,
    registry_digest,
)
from profile_inspection import (
    ProfileInspectionError,
    ProfileTestCandidate,
    ProfileTestResult,
    SshProfileMetadata,
    SshProfileTester,
    inspect_profile,
    profile_test_candidate_from_document,
    profile_test_result_to_document,
    validate_local_directory_path,
)
from ssh_config_discovery import discover_ssh_host_aliases, validate_ssh_host_alias


HOST_CONTRACT_VERSION = 1
MAX_HOST_REQUEST_BYTES = 1_048_576
MAX_HOST_RESPONSE_BYTES = 2_097_152
MAX_DISCOVERED_SSH_ALIASES = 512
HOST_REQUEST_TYPE = "workstack-connection-registry-request"
HOST_RESPONSE_TYPE = "workstack-connection-registry-response"

GET_REGISTRY = "get-registry"
SAVE_REGISTRY = "save-registry"
DISCOVER_SSH_ALIASES = "discover-ssh-aliases"
CHOOSE_LOCAL_DIRECTORY = "choose-local-directory"
TEST_PROFILE = "test-profile"
ACTIVATE_PROFILE = "activate-profile"
_HOST_OPERATIONS = frozenset(
    {
        GET_REGISTRY,
        SAVE_REGISTRY,
        DISCOVER_SSH_ALIASES,
        CHOOSE_LOCAL_DIRECTORY,
        TEST_PROFILE,
        ACTIVATE_PROFILE,
    }
)

RegistryHostOperation: TypeAlias = Literal[
    "get-registry",
    "save-registry",
    "discover-ssh-aliases",
    "choose-local-directory",
    "test-profile",
    "activate-profile",
]


@dataclass(frozen=True)
class GetRegistryRequest:
    request_id: str
    operation: Literal["get-registry"] = GET_REGISTRY


@dataclass(frozen=True)
class SaveRegistryRequest:
    request_id: str
    registry: ConnectionRegistry
    expected_registry_digest: str
    operation: Literal["save-registry"] = SAVE_REGISTRY


@dataclass(frozen=True)
class DiscoverSshAliasesRequest:
    request_id: str
    operation: Literal["discover-ssh-aliases"] = DISCOVER_SSH_ALIASES


@dataclass(frozen=True)
class ChooseLocalDirectoryRequest:
    request_id: str
    operation: Literal["choose-local-directory"] = CHOOSE_LOCAL_DIRECTORY


@dataclass(frozen=True)
class TestProfileRequest:
    request_id: str
    profile: ProfileTestCandidate
    base_registry_digest: str
    operation: Literal["test-profile"] = TEST_PROFILE


@dataclass(frozen=True)
class ActivateProfileRequest:
    request_id: str
    registry: ConnectionRegistry
    profile_id: str
    proof_id: str
    expected_registry_digest: str
    operation: Literal["activate-profile"] = ACTIVATE_PROFILE


RegistryHostRequest: TypeAlias = (
    GetRegistryRequest
    | SaveRegistryRequest
    | DiscoverSshAliasesRequest
    | ChooseLocalDirectoryRequest
    | TestProfileRequest
    | ActivateProfileRequest
)


@dataclass(frozen=True)
class GetRegistryResponse:
    request_id: str
    registry: ConnectionRegistry | None
    registry_digest: str
    operation: Literal["get-registry"] = GET_REGISTRY


@dataclass(frozen=True)
class SaveRegistryResponse:
    request_id: str
    registry: ConnectionRegistry
    registry_digest: str
    operation: Literal["save-registry"] = SAVE_REGISTRY


@dataclass(frozen=True)
class DiscoverSshAliasesResponse:
    request_id: str
    aliases: tuple[str, ...]
    operation: Literal["discover-ssh-aliases"] = DISCOVER_SSH_ALIASES


@dataclass(frozen=True)
class ChooseLocalDirectoryResponse:
    request_id: str
    selection: str | None
    operation: Literal["choose-local-directory"] = CHOOSE_LOCAL_DIRECTORY


@dataclass(frozen=True)
class TestProfileResponse:
    request_id: str
    result: ProfileTestResult
    proof_id: str | None = None
    operation: Literal["test-profile"] = TEST_PROFILE


@dataclass(frozen=True)
class ActivateProfileResponse:
    request_id: str
    registry: ConnectionRegistry
    registry_digest: str
    restart_required: bool = True
    operation: Literal["activate-profile"] = ACTIVATE_PROFILE


@dataclass(frozen=True)
class RegistryHostErrorResponse:
    request_id: str | None
    operation: RegistryHostOperation | None
    code: str
    message: str


RegistryHostResponse: TypeAlias = (
    GetRegistryResponse
    | SaveRegistryResponse
    | DiscoverSshAliasesResponse
    | ChooseLocalDirectoryResponse
    | TestProfileResponse
    | ActivateProfileResponse
    | RegistryHostErrorResponse
)


def decode_registry_host_request(payload: str | bytes) -> RegistryHostRequest:
    """Decode one bounded, exact, versioned request or fail closed."""

    text = _bounded_request_text(payload)
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as error:
        raise RuntimeError("Registry host request must be valid JSON") from error
    if not isinstance(raw, dict):
        raise RuntimeError("Registry host request must contain one JSON object")

    operation = raw.get("operation")
    if operation == GET_REGISTRY:
        _require_exact_fields(
            raw,
            {"type", "schema_version", "request_id", "operation"},
            "get-registry request",
        )
        return GetRegistryRequest(_validate_envelope(raw))
    if operation == SAVE_REGISTRY:
        _require_exact_fields(
            raw,
            {
                "type",
                "schema_version",
                "request_id",
                "operation",
                "registry",
                "expected_registry_digest",
            },
            "save-registry request",
        )
        request_id = _validate_envelope(raw)
        return SaveRegistryRequest(
            request_id=request_id,
            registry=registry_from_document(raw["registry"]),
            expected_registry_digest=_validate_digest(
                raw["expected_registry_digest"], "expected_registry_digest"
            ),
        )
    if operation == DISCOVER_SSH_ALIASES:
        _require_exact_fields(
            raw,
            {"type", "schema_version", "request_id", "operation"},
            "discover-ssh-aliases request",
        )
        return DiscoverSshAliasesRequest(_validate_envelope(raw))
    if operation == CHOOSE_LOCAL_DIRECTORY:
        _require_exact_fields(
            raw,
            {"type", "schema_version", "request_id", "operation"},
            "choose-local-directory request",
        )
        return ChooseLocalDirectoryRequest(_validate_envelope(raw))
    if operation == TEST_PROFILE:
        _require_exact_fields(
            raw,
            {
                "type",
                "schema_version",
                "request_id",
                "operation",
                "profile",
                "base_registry_digest",
            },
            "test-profile request",
        )
        request_id = _validate_envelope(raw)
        return TestProfileRequest(
            request_id=request_id,
            profile=profile_test_candidate_from_document(raw["profile"]),
            base_registry_digest=_validate_digest(
                raw["base_registry_digest"], "base_registry_digest"
            ),
        )
    if operation == ACTIVATE_PROFILE:
        _require_exact_fields(
            raw,
            {
                "type",
                "schema_version",
                "request_id",
                "operation",
                "registry",
                "profile_id",
                "proof_id",
                "expected_registry_digest",
            },
            "activate-profile request",
        )
        request_id = _validate_envelope(raw)
        return ActivateProfileRequest(
            request_id=request_id,
            registry=registry_from_document(raw["registry"]),
            profile_id=_canonical_uuid(raw["profile_id"], "profile_id"),
            proof_id=_canonical_uuid(raw["proof_id"], "proof_id"),
            expected_registry_digest=_validate_digest(
                raw["expected_registry_digest"], "expected_registry_digest"
            ),
        )
    raise RuntimeError("Registry host request operation is unsupported")


def encode_registry_host_response(response: RegistryHostResponse) -> str:
    """Encode a response with a deterministic envelope and exact result shape."""

    if isinstance(response, RegistryHostErrorResponse):
        if (response.request_id is None) != (response.operation is None):
            raise RuntimeError(
                "Error response correlation fields must both be present or absent"
            )
        document: dict[str, object] = {
            "type": HOST_RESPONSE_TYPE,
            "schema_version": HOST_CONTRACT_VERSION,
            "request_id": (
                None
                if response.request_id is None
                else _canonical_uuid(response.request_id, "request_id")
            ),
            "operation": (
                None
                if response.operation is None
                else _validate_operation(response.operation)
            ),
            "ok": False,
            "error": {
                "code": _bounded_response_text(response.code, "error code", 64),
                "message": _bounded_response_text(
                    response.message, "error message", 256
                ),
            },
        }
    elif isinstance(response, GetRegistryResponse):
        if response.operation != GET_REGISTRY:
            raise RuntimeError("Get registry response operation is invalid")
        document = _success_document(
            response.request_id,
            response.operation,
            {
                "registry": None
                if response.registry is None
                else registry_to_document(response.registry),
                "registry_digest": _validate_digest(
                    response.registry_digest, "registry_digest"
                ),
            },
        )
    elif isinstance(response, SaveRegistryResponse):
        if response.operation != SAVE_REGISTRY:
            raise RuntimeError("Save registry response operation is invalid")
        document = _success_document(
            response.request_id,
            response.operation,
            {
                "registry": registry_to_document(response.registry),
                "registry_digest": _validate_digest(
                    response.registry_digest, "registry_digest"
                ),
            },
        )
    elif isinstance(response, DiscoverSshAliasesResponse):
        if response.operation != DISCOVER_SSH_ALIASES:
            raise RuntimeError("SSH alias response operation is invalid")
        aliases = tuple(_validate_alias_result(alias) for alias in response.aliases)
        if len(aliases) > MAX_DISCOVERED_SSH_ALIASES:
            raise RuntimeError("SSH alias response contains too many entries")
        if len({alias.casefold() for alias in aliases}) != len(aliases):
            raise RuntimeError("SSH alias response contains duplicate entries")
        document = _success_document(
            response.request_id,
            response.operation,
            {"aliases": list(aliases)},
        )
    elif isinstance(response, ChooseLocalDirectoryResponse):
        if response.operation != CHOOSE_LOCAL_DIRECTORY:
            raise RuntimeError("Local directory response operation is invalid")
        selection = (
            None
            if response.selection is None
            else validate_local_directory_path(response.selection)
        )
        document = _success_document(
            response.request_id,
            response.operation,
            {"selection": selection},
        )
    elif isinstance(response, TestProfileResponse):
        if response.operation != TEST_PROFILE:
            raise RuntimeError("Profile test response operation is invalid")
        document = _success_document(
            response.request_id,
            response.operation,
            {
                **profile_test_result_to_document(response.result),
                "proof_id": (
                    None
                    if response.proof_id is None
                    else _canonical_uuid(response.proof_id, "proof_id")
                ),
            },
        )
    elif isinstance(response, ActivateProfileResponse):
        if response.operation != ACTIVATE_PROFILE or response.restart_required is not True:
            raise RuntimeError("Activate profile response is invalid")
        document = _success_document(
            response.request_id,
            response.operation,
            {
                "registry": registry_to_document(response.registry),
                "registry_digest": _validate_digest(
                    response.registry_digest, "registry_digest"
                ),
                "restart_required": True,
            },
        )
    else:
        raise TypeError("Unsupported registry host response")
    encoded = json.dumps(document, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_HOST_RESPONSE_BYTES:
        raise RuntimeError("Registry host response is too large")
    return encoded


class ConnectionRegistryHostService:
    """Purely dispatch bounded registry bridge operations to injected services."""

    def __init__(
        self,
        state_root: Path,
        *,
        ssh_config_path: Path | None = None,
        local_directory_picker: Callable[[], str | Path | None] = lambda: None,
        ssh_profile_tester: SshProfileTester | None = None,
        registry_loader: Callable[[Path], ConnectionRegistry | None] = load_connection_registry,
        registry_saver: Callable[
            [Path, ConnectionRegistry | object], ConnectionRegistry
        ] = save_connection_registry,
        alias_discoverer: Callable[[Path | None], tuple[str, ...]] = discover_ssh_host_aliases,
        mutation_service: ConnectionRegistryMutationService | None = None,
        activation_observer: Callable[[ConnectionRegistry, str], None] = (
            lambda _registry, _digest: None
        ),
    ) -> None:
        self._state_root = Path(state_root)
        self._ssh_config_path = (
            None if ssh_config_path is None else Path(ssh_config_path)
        )
        self._registry_loader = registry_loader
        self._registry_saver = registry_saver
        self._alias_discoverer = alias_discoverer
        self._local_directory_picker = local_directory_picker
        self._ssh_profile_tester = ssh_profile_tester
        self._mutation_service = mutation_service
        self._activation_observer = activation_observer

    def execute(self, request: RegistryHostRequest) -> RegistryHostResponse:
        if isinstance(request, GetRegistryRequest):
            registry = self._registry_loader(self._state_root)
            return GetRegistryResponse(
                request_id=request.request_id,
                registry=registry,
                registry_digest=registry_digest(registry),
            )
        if isinstance(request, SaveRegistryRequest):
            if self._mutation_service is None:
                raise RuntimeError("Connection registry mutations are disabled")
            saved, digest = self._mutation_service.save_metadata(
                request.registry,
                expected_registry_digest=request.expected_registry_digest,
            )
            return SaveRegistryResponse(request.request_id, saved, digest)
        if isinstance(request, DiscoverSshAliasesRequest):
            aliases = self._alias_discoverer(self._ssh_config_path)
            return DiscoverSshAliasesResponse(request.request_id, tuple(aliases))
        if isinstance(request, ChooseLocalDirectoryRequest):
            selected = self._local_directory_picker()
            selection = (
                None if selected is None else validate_local_directory_path(selected)
            )
            return ChooseLocalDirectoryResponse(request.request_id, selection)
        if isinstance(request, TestProfileRequest):
            result = inspect_profile(
                request.profile,
                ssh_profile_tester=self._ssh_profile_tester,
            )
            proof_id = None
            if result.status == "ready":
                if self._mutation_service is None:
                    raise RuntimeError("Connection registry mutations are disabled")
                if result.actual_workspace_id is None:
                    raise RuntimeError("Ready profile result is missing an identity")
                tested_profile = replace(
                    request.profile.profile,
                    expected_workspace_id=result.actual_workspace_id,
                )
                proof = self._mutation_service.issue_successful_test_proof(
                    tested_profile,
                    result,
                    base_registry_digest=request.base_registry_digest,
                )
                proof_id = proof.proof_id
            return TestProfileResponse(request.request_id, result, proof_id)
        if isinstance(request, ActivateProfileRequest):
            if self._mutation_service is None:
                raise RuntimeError("Connection registry mutations are disabled")
            receipt = self._mutation_service.activate(
                request.registry,
                request.profile_id,
                request.proof_id,
                expected_registry_digest=request.expected_registry_digest,
            )
            saved, digest = current_registry_snapshot(self._state_root)
            if digest != receipt.activated_registry_digest:
                raise RegistryConflictError("Activated registry changed before response")
            self._activation_observer(saved, digest)
            return ActivateProfileResponse(request.request_id, saved, digest)
        raise TypeError("Unsupported registry host request")

    def handle_json(self, payload: str | bytes) -> str:
        """Return a sanitized JSON response for one untrusted bridge payload."""

        try:
            request = decode_registry_host_request(payload)
        except (RuntimeError, UnicodeError):
            return encode_registry_host_response(
                RegistryHostErrorResponse(
                    request_id=None,
                    operation=None,
                    code="invalid_request",
                    message="Connection registry request is invalid.",
                )
            )
        try:
            response = self.execute(request)
            return encode_registry_host_response(response)
        except (RegistryConflictError, ActivationProofError) as error:
            return encode_registry_host_response(
                RegistryHostErrorResponse(
                    request_id=request.request_id,
                    operation=request.operation,
                    code=error.code,
                    message=error.safe_message,
                )
            )
        except ProfileInspectionError as error:
            return encode_registry_host_response(
                RegistryHostErrorResponse(
                    request_id=request.request_id,
                    operation=request.operation,
                    code=error.code,
                    message=error.safe_message,
                )
            )
        except (RuntimeError, OSError, TypeError, ValueError):
            return encode_registry_host_response(
                RegistryHostErrorResponse(
                    request_id=request.request_id,
                    operation=request.operation,
                    code="operation_failed",
                    message="Connection registry operation failed.",
                )
            )


def _bounded_request_text(payload: str | bytes) -> str:
    if isinstance(payload, bytes):
        if len(payload) > MAX_HOST_REQUEST_BYTES:
            raise RuntimeError("Registry host request is too large")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("Registry host request must be UTF-8") from error
    if not isinstance(payload, str):
        raise RuntimeError("Registry host request must be text or UTF-8 bytes")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RuntimeError("Registry host request must be valid UTF-8") from error
    if len(encoded) > MAX_HOST_REQUEST_BYTES:
        raise RuntimeError("Registry host request is too large")
    return payload


def _validate_envelope(raw: dict[object, object]) -> str:
    if raw["type"] != HOST_REQUEST_TYPE:
        raise RuntimeError(f"type must be exactly '{HOST_REQUEST_TYPE}'")
    version = raw["schema_version"]
    if isinstance(version, bool) or version != HOST_CONTRACT_VERSION:
        raise RuntimeError(
            f"schema_version must be exactly {HOST_CONTRACT_VERSION}"
        )
    return _canonical_uuid(raw["request_id"], "request_id")


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{field} must be a canonical non-nil UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise RuntimeError(f"{field} must be a canonical non-nil UUID") from error
    if value != str(parsed) or parsed.int == 0:
        raise RuntimeError(f"{field} must be a canonical non-nil UUID")
    return value


def _require_exact_fields(
    raw: dict[object, object], required: set[str], context: str
) -> None:
    missing = required - set(raw)
    unexpected = set(raw) - required
    if missing:
        raise RuntimeError(f"{context} is missing required fields")
    if unexpected:
        raise RuntimeError(f"{context} has unsupported fields")


def _success_document(
    request_id: str, operation: RegistryHostOperation, result: dict[str, object]
) -> dict[str, object]:
    return {
        "type": HOST_RESPONSE_TYPE,
        "schema_version": HOST_CONTRACT_VERSION,
        "request_id": _canonical_uuid(request_id, "request_id"),
        "operation": _validate_operation(operation),
        "ok": True,
        "result": result,
    }


def _bounded_response_text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise RuntimeError(f"{field} must be bounded text")
    if any(ord(character) < 32 for character in value):
        raise RuntimeError(f"{field} contains a control character")
    return value


def _validate_alias_result(value: object) -> str:
    # Reuse the registry's identical public alias contract without exposing any
    # OpenSSH-expanded values or subprocess arguments.
    try:
        return validate_ssh_host_alias(value)
    except ValueError as error:
        raise RuntimeError("SSH alias response contains an invalid entry") from error


def _validate_operation(value: object) -> RegistryHostOperation:
    if value not in _HOST_OPERATIONS:
        raise RuntimeError("Registry host response operation is unsupported")
    return value  # type: ignore[return-value]


def _validate_digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise RuntimeError(f"{field} must be a canonical sha256 digest")
    return value

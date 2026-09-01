"""Deterministic JSON bytes for normalized SSOT records and manifests."""

from __future__ import annotations

import hashlib
import json
from typing import Any


CANONICAL_JSON_FORMAT = "workstack.canonical-json.v1"
MAX_CANONICAL_INTEGER = 9_007_199_254_740_991


class CanonicalJsonError(ValueError):
    """A content-free refusal to canonicalize an unsupported value."""

    def __init__(self, code: str, location: str) -> None:
        super().__init__(f"{code} at {location}")
        self.code = code
        self.location = location


def _validate_text(value: str, location: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CanonicalJsonError("INVALID_UNICODE", location) from error


def _validate_integer(value: int, location: str) -> None:
    if not -MAX_CANONICAL_INTEGER <= value <= MAX_CANONICAL_INTEGER:
        raise CanonicalJsonError("UNSAFE_INTEGER", location)


def _enter_container(value: object, active: set[int], location: str) -> int:
    identity = id(value)
    if identity in active:
        raise CanonicalJsonError("CYCLIC_VALUE", location)
    active.add(identity)
    return identity


def _validate_list(value: list[Any], active: set[int], location: str) -> None:
    identity = _enter_container(value, active, location)
    try:
        for index, child in enumerate(value):
            _validate_value(child, active, f"{location}/items/{index}")
    finally:
        active.remove(identity)


def _validate_mapping(value: dict[Any, Any], active: set[int], location: str) -> None:
    keys = list(value)
    if any(type(key) is not str for key in keys):
        raise CanonicalJsonError("NON_STRING_KEY", location)
    identity = _enter_container(value, active, location)
    try:
        for index, key in enumerate(sorted(keys)):
            _validate_text(key, f"{location}/keys/{index}")
            _validate_value(value[key], active, f"{location}/members/{index}")
    finally:
        active.remove(identity)


def _validate_value(value: Any, active: set[int], location: str) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        _validate_text(value, location)
        return
    if type(value) is int:
        _validate_integer(value, location)
        return
    if type(value) is float:
        raise CanonicalJsonError("UNSUPPORTED_FLOAT", location)
    if type(value) is list:
        _validate_list(value, active, location)
        return
    if type(value) is dict:
        _validate_mapping(value, active, location)
        return
    raise CanonicalJsonError("UNSUPPORTED_TYPE", location)


def canonical_json_bytes(value: Any) -> bytes:
    """Return the exact UTF-8 representation defined by Work Stack JSON v1."""

    _validate_value(value, set(), "$")
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            check_circular=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return text.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise CanonicalJsonError("SERIALIZATION_FAILED", "$") from error


def canonical_sha256(value: Any) -> str:
    """Return a prefixed SHA-256 digest over canonical Work Stack JSON bytes."""

    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()

"""Read one operator-written knowledge-driver registry file, once, at startup.

The operator writes a `workstack.knowledge-drivers.v1` document and names it on
`graph serve --knowledge-drivers-config <absolute path>`. This module turns that
one file into the mapping :func:`workstack.knowledge_execution_runtime.admit_drivers`
already admits, and refuses everything it cannot admit *before* the caller
creates a Store, takes its lease, opens a socket or seeds a demo.

**It reads exactly the named file and nothing else.** No default path, no
environment variable, no ambient discovery, no merge with `os.environ`, no read
of an adapter's own configuration or key material, no subprocess, no watch and
no reload: a running server's registry is the one this call returned.

**Every refusal is a closed code.** The path, the document, an argv part, an
environment name or value and a caught OS message never travel in a refusal,
because the operator's file may name a credential file the process must not
echo. Shape rules are the transport's own, applied through `admit_drivers`
rather than restated here; the decoder is the request decoder, so a duplicate
key, a non-finite number, an over-deep document and a bad encoding are refused
by the same grammar the rest of the process already uses.

An entry may optionally add one `verification: {command, environment}` stanza,
which pins that alias' separately opted-in read-time source verifier. It is
optional in exactly one direction: omitting it is the unchanged legacy entry
and means the alias has no verifier, while writing it half-formed -- as null,
as an empty object, with an unknown key or with a missing half -- refuses the
start rather than producing a server that silently never verifies. The stanza
never rewrites, inherits or reuses the search `command`, and the alias and its
upstream identity above cover both operations.

The file, its permissions and the adapter it points at are the operator's own
trusted material on one OS user account. This is not a sandbox and this module
does not claim to detect a secret in an arbitrary string.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .knowledge_execution_runtime import (
    MAX_DRIVERS,
    KnowledgeDriverBinding,
    KnowledgeDriverConfigurationError,
    KnowledgeVerificationBinding,
    admit_drivers,
)
from .knowledge_request import KnowledgeRequestError, decode_strict_json
# The request decoder's own closed-schema object predicate, reused rather than
# restated. It is module-private because it is not a public boundary, and this
# caller is not making it one: a second spelling of "exactly these fields, no
# more and no fewer" could drift from the one the rest of the process applies.
from .knowledge_request import _object as _closed_object  # noqa: PLC2701

__all__ = (
    "MAX_CONFIG_BYTES",
    "MIN_DRIVERS",
    "REGISTRY_SCHEMA",
    "KnowledgeDriverBinding",
    "KnowledgeDriverConfigurationError",
    "KnowledgeVerificationBinding",
    "load_driver_registry",
)

#: The only document this loader accepts.
REGISTRY_SCHEMA = "workstack.knowledge-drivers.v1"

#: One operator file, bounded before it is decoded.
MAX_CONFIG_BYTES = 64 * 1024

#: A registry that pins nothing is a mistake, not the default. The default is
#: the absent flag, which stays the explicit "no driver is configured" state.
MIN_DRIVERS = 1

_REGISTRY_FIELDS = frozenset({"schema", "drivers"})
_DRIVER_FIELDS = frozenset({
    "alias",
    "upstream_workspace_uid",
    "command",
    "environment",
})
#: The one optional key an entry may add. An entry either has exactly the
#: legacy four fields or exactly those four plus this one: `verification` is
#: opt-in, so omitting it is the unchanged default and *writing* it means the
#: operator deliberately turned one alias' read-time check on.
_VERIFICATION_FIELD = "verification"
_VERIFIED_DRIVER_FIELDS = _DRIVER_FIELDS | {_VERIFICATION_FIELD}
_VERIFICATION_FIELDS = frozenset({"command", "environment"})


def _config_bytes(path: str) -> bytes:
    """The named file's exact octets, bounded, read once.

    The path must be absolute so the operator's pin is not resolved against a
    working directory. Reading at most one octet past the bound keeps an
    oversized file from being loaded to be measured.
    """

    if not isinstance(path, str) or not path:
        raise KnowledgeDriverConfigurationError("driver_config_path_required")
    if not os.path.isabs(path):
        raise KnowledgeDriverConfigurationError("driver_config_path_not_absolute")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        # The path, the errno and the OS message all stay out of the refusal:
        # the operator's own file name can itself be sensitive.
        raise OSError("driver_config_unreadable") from error
    if len(raw) > MAX_CONFIG_BYTES:
        raise KnowledgeDriverConfigurationError("driver_config_too_large")
    return raw


def _registry_entries(raw: bytes) -> list[Any]:
    """The `drivers` array of one v1 document, or a closed refusal.

    The decoder's own codes travel unchanged: `duplicate_json_key`,
    `non_finite_number`, `request_too_deep`, `invalid_encoding` and
    `invalid_json` mean here exactly what they mean everywhere else.
    """

    try:
        decoded = decode_strict_json(raw, maximum_bytes=MAX_CONFIG_BYTES)
        if not isinstance(decoded, dict):
            raise KnowledgeDriverConfigurationError("invalid_driver_registry")
        document = _closed_object(decoded, "registry", _REGISTRY_FIELDS)
    except KnowledgeRequestError as error:
        raise KnowledgeDriverConfigurationError(error.code) from error
    if document["schema"] != REGISTRY_SCHEMA:
        raise KnowledgeDriverConfigurationError("unknown_driver_registry_schema")
    entries = document["drivers"]
    if not isinstance(entries, list):
        raise KnowledgeDriverConfigurationError("invalid_driver_registry")
    if len(entries) < MIN_DRIVERS:
        raise KnowledgeDriverConfigurationError("driver_registry_empty")
    if len(entries) > MAX_DRIVERS:
        raise KnowledgeDriverConfigurationError("driver_registry_full")
    return entries


def _environment(value: Any) -> dict[str, str]:
    """Explicit names, none of them a case variant of another.

    Windows resolves an environment name case-insensitively, so two names that
    differ only in case are one name to the child and an ambiguity here. The
    types of the names and values are `admit_drivers`' decision, not this one.
    """

    if not isinstance(value, dict):
        raise KnowledgeDriverConfigurationError("invalid_driver_environment")
    folded: set[str] = set()
    for name in value:
        if not isinstance(name, str):
            raise KnowledgeDriverConfigurationError("invalid_driver_environment")
        if name.casefold() in folded:
            raise KnowledgeDriverConfigurationError(
                "duplicate_driver_environment_name"
            )
        folded.add(name.casefold())
    return value


def _verification(entry: dict[str, Any]) -> KnowledgeVerificationBinding | None:
    """The entry's optional verifier, or the absence that means "none".

    Absence is *omission*. An explicit ``null``, an empty object, a missing
    half and an unknown key are all refused rather than read as "off": an
    operator who half-wrote a verifier stanza gets a refused start, not a
    server that silently never verifies. The environment goes through the same
    case-insensitive duplicate-name check the search driver's does, because the
    child resolves both the same way on Windows.
    """

    if _VERIFICATION_FIELD not in entry:
        return None
    stanza = entry[_VERIFICATION_FIELD]
    if not isinstance(stanza, dict):
        # An explicit ``null`` is the case this arm exists for: "written, and
        # written as nothing" is a mistake, not a way to spell the default.
        raise KnowledgeDriverConfigurationError("invalid_driver_verification")
    try:
        pinned = _closed_object(stanza, "verification", _VERIFICATION_FIELDS)
    except KnowledgeRequestError as error:
        raise KnowledgeDriverConfigurationError(error.code) from error
    return KnowledgeVerificationBinding(
        command=pinned["command"],
        environment=_environment(pinned["environment"]),
    )


def _driver_entry(entry: Any) -> dict[str, Any]:
    """One entry's fields: the legacy four, optionally plus ``verification``.

    The choice between the two closed shapes is made on the *presence* of the
    key alone, so an entry written before this field existed is admitted by the
    exact rules it always was, and an entry that names it is held to the whole
    closed verifier shape.
    """

    if not isinstance(entry, dict):
        raise KnowledgeDriverConfigurationError("invalid_driver_binding")
    allowed = (
        _VERIFIED_DRIVER_FIELDS if _VERIFICATION_FIELD in entry else _DRIVER_FIELDS
    )
    try:
        return _closed_object(entry, "drivers", allowed)
    except KnowledgeRequestError as error:
        raise KnowledgeDriverConfigurationError(error.code) from error


def _pinned_drivers(entries: list[Any]) -> dict[str, KnowledgeDriverBinding]:
    """One binding per entry, keyed by the alias the operator wrote.

    A repeated alias is refused rather than resolved last-wins: the operator
    would be shown one pin and the owner would execute another.
    """

    pinned: dict[str, KnowledgeDriverBinding] = {}
    for entry in entries:
        driver = _driver_entry(entry)
        alias = driver["alias"]
        if not isinstance(alias, str):
            raise KnowledgeDriverConfigurationError("invalid_driver_alias")
        if alias in pinned:
            raise KnowledgeDriverConfigurationError("duplicate_driver_alias")
        pinned[alias] = KnowledgeDriverBinding(
            upstream_workspace_uid=driver["upstream_workspace_uid"],
            command=driver["command"],
            environment=_environment(driver["environment"]),
            verification=_verification(driver),
        )
    return pinned


def load_driver_registry(
    path: str | None,
) -> Mapping[str, KnowledgeDriverBinding] | None:
    """Admit the registry at `path`; answer `None` when no flag was given.

    `None` is the unchanged default: the caller then makes the same server call
    it made before this flag existed. Anything else is read, decoded and
    admitted here, so an unusable registry refuses a start instead of surfacing
    as an execution-time refusal on a server that already holds the store.
    """

    if path is None:
        return None
    return admit_drivers(_pinned_drivers(_registry_entries(_config_bytes(path))))

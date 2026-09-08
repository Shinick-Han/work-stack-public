"""Owner binding, advertisement reads, and session/storage/sync preflight.

The CLI writer never takes a local Store lease while owner metadata is present.
This module classifies that metadata, reads it under a hard byte bound, and
runs the one preflight sequence every owner write reuses. It does not post
mutations, import the CLI, or grow an HTTP stack: ``request_json`` stays
injected by the caller.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import stat
import uuid
from typing import Callable, Mapping

# (status, payload) transport, injected by the CLI so this module never imports
# it back, never grows its own HTTP stack, and never changes the shared helper.
RequestJson = Callable[..., tuple[int, dict[str, object]]]
# store -> (host, port) | None. Still accepted so the CLI call site and the
# frozen contract keep their exact shape, but the note route no longer reads
# owner coordinates through it: see _resolve_coordinates for why an unbounded
# helper cannot sit behind a byte bound.
CoordinatesReader = Callable[..., "tuple[str, int] | None"]

# Mirrors the states the store reports; anything else is an invalid response.
SYNC_STATES = frozenset({"external-change-detected", "in-sync", "invalid"})

# Owner metadata classification. Only ABSENT keeps the exclusive-local path.
OWNER_ABSENT = "absent"
OWNER_PRESENT = "present"
OWNER_INVALID = "invalid"

# Bounded reads. Owner metadata is a tiny fixed record; the store manifest lists
# files and tasks and is larger, but neither is unbounded input.
SERVER_INFO_READ_LIMIT = 64 * 1024
STORE_MANIFEST_READ_LIMIT = 4 * 1024 * 1024

# A response that never arrived, arrived truncated, or arrived unparseable after
# the request went out leaves the outcome unknown. http.client raises
# HTTPException subclasses such as IncompleteRead and BadStatusLine that are not
# OSError, so both hierarchies count.
AMBIGUOUS_TRANSPORT = (OSError, http.client.HTTPException)

# Tests patch ``workstack.cli_writer.open``. The facade binds this to a lookup
# in that module so the existing monkeypatch seam keeps working after extraction.
_open_impl = open


def bind_open(opener):
    global _open_impl
    _open_impl = opener


def _open(*args, **kwargs):
    return _open_impl(*args, **kwargs)


class WriterTransportError(OSError):
    """Owner-route failure. Never fall back to a local write after this."""


class CommitUnknownError(WriterTransportError):
    """The note POST outcome stayed unknown after the one permitted replay."""


def owner_metadata_state(store: object) -> str:
    """Classify the owner metadata entry by its actual filesystem kind.

    ``is_file()`` collapses "missing" and "exists but is a directory, symlink or
    otherwise not a regular file" into one false answer. Only the first is
    absence; the rest are an invalid owner state that must refuse rather than
    silently take the local write path. ``lstat`` is used rather than ``stat``
    so a symlink is judged as a symlink instead of its target.

    A ``server_info_path`` that is not a filesystem path at all yields
    ``OWNER_ABSENT``: there is no entry to observe. That is the single
    accommodation here, and it is not used to swallow errors about a real entry.
    """

    path = getattr(store, "server_info_path", None)
    if path is None:
        return OWNER_ABSENT
    try:
        target = os.fspath(path)
    except TypeError:
        return OWNER_ABSENT
    try:
        info = os.lstat(target)
    except (FileNotFoundError, NotADirectoryError):
        return OWNER_ABSENT
    except (OSError, ValueError):
        # Permission denied, a bad name, a broken mount: the entry may exist and
        # cannot be read. Refusing is the only safe reading.
        return OWNER_INVALID
    return OWNER_PRESENT if stat.S_ISREG(info.st_mode) else OWNER_INVALID


def _read_bounded(path: object, limit: int, label: str) -> bytes:
    """Read at most ``limit`` bytes, and never quote the path in the error."""

    try:
        with _open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as error:
        raise WriterTransportError("Work Stack {} is unreadable".format(label)) from error
    if len(raw) > limit:
        raise WriterTransportError("Work Stack {} exceeds the supported size".format(label))
    return raw


def _canonical_workspace_uid(value: object) -> str:
    if type(value) is not str:
        raise WriterTransportError("workspace identity is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise WriterTransportError("workspace identity is invalid") from error
    if parsed.int == 0 or parsed.variant != uuid.RFC_4122 or str(parsed) != value:
        raise WriterTransportError("workspace identity is invalid")
    return value


def expected_workspace_uid(store: object) -> str:
    """Read the selected workspace identity without taking the Store lease."""

    path = getattr(store, "store_manifest_path", None)
    if path is None:
        raise WriterTransportError("Work Stack store manifest is unavailable")
    raw = _read_bounded(path, STORE_MANIFEST_READ_LIMIT, "store manifest")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WriterTransportError("Work Stack store manifest is invalid") from error
    if not isinstance(manifest, dict):
        raise WriterTransportError("Work Stack store manifest is invalid")
    return _canonical_workspace_uid(manifest.get("workspace_id"))


def _origin(host: str, port: int) -> str:
    origin_host = "[{}]".format(host) if ":" in host else host
    return "http://{}:{}".format(origin_host, port)


def _preflight_get(
    request_json: RequestJson, host: str, port: int, path: str, label: str
) -> dict[str, object]:
    """One preflight read. Never retried, and never leaks the server's own text."""

    try:
        status, payload = request_json(host, port, "GET", path)
    except AMBIGUOUS_TRANSPORT as error:
        raise WriterTransportError(
            "the running Work Stack server {} could not be read".format(label)
        ) from error
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if status != 200 or not isinstance(data, dict):
        raise WriterTransportError(
            "Work Stack server {} response is invalid".format(label)
        )
    return data


def _preflight(request_json: RequestJson, host: str, port: int, expected_uid: str) -> str:
    """Session, identity and readiness. A preflight failure is final, never retried."""

    session = _preflight_get(request_json, host, port, "/api/v1/session", "session")
    csrf = session.get("csrf_token")
    if type(csrf) is not str or not csrf:
        raise WriterTransportError("Work Stack server session could not be established")

    storage = _preflight_get(request_json, host, port, "/api/v1/storage", "storage")
    if _canonical_workspace_uid(storage.get("workspace_id")) != expected_uid:
        raise WriterTransportError(
            "the running Work Stack server owns a different workspace identity"
        )

    sync = _preflight_get(request_json, host, port, "/api/v1/sync/status", "sync status")
    state = sync.get("state")
    if type(state) is not str or state not in SYNC_STATES:
        raise WriterTransportError("Work Stack server sync status response is invalid")
    if state != "in-sync":
        raise WriterTransportError(
            "the running Work Stack store is not in-sync; resolve synchronization first"
        )
    return csrf

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def read_owner_binding(store: object) -> tuple[str, int, str]:
    """Read the owner advertisement under a hard byte bound and validate it.

    The shared ``_server_coordinates`` helper is the Agent transport's contract
    and reads the whole entry; the note route must not inherit that. An owner
    advertisement is a tiny fixed record, so anything above
    ``SERVER_INFO_READ_LIMIT`` is refused as oversized *before* the note path
    makes any HTTP contact, rather than being parsed because it happens to be
    valid JSON.

    Returns ``(host, port, binding)``. ``binding`` is a digest of the exact
    bytes observed, so the same advertisement can be proven unchanged later
    without keeping its contents around.
    """

    path = getattr(store, "server_info_path", None)
    if path is None:
        raise WriterTransportError(
            "Work Stack server runtime metadata became unavailable; refusing to write locally"
        )
    state = owner_metadata_state(store)
    if state == OWNER_ABSENT:
        # Presence was already observed once, so an absent entry now means it
        # vanished mid-flight. That is never permission to write locally.
        raise WriterTransportError(
            "Work Stack server runtime metadata became unavailable; refusing to write locally"
        )
    if state != OWNER_PRESENT:
        raise WriterTransportError(
            "Work Stack server runtime metadata is not a readable regular file"
        )

    raw = _read_bounded(path, SERVER_INFO_READ_LIMIT, "server runtime metadata")
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WriterTransportError(
            "Work Stack server runtime metadata is invalid"
        ) from error
    if not isinstance(info, dict):
        raise WriterTransportError("Work Stack server runtime metadata is invalid")
    version = info.get("version")
    host = info.get("host")
    port = info.get("port")
    # Types are checked before the operations that depend on them. Equality
    # alone would accept True and 1.0 for the version, because both compare
    # equal to 1, and a set membership test on an unhashable host would raise
    # TypeError out of this module instead of refusing. `type(...) is int`
    # rather than isinstance also excludes bool, which is an int subclass.
    if (
        type(version) is not int
        or version != 1
        or type(host) is not str
        or host not in LOOPBACK_HOSTS
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise WriterTransportError("Work Stack server runtime metadata is invalid")
    return str(info["host"]), port, hashlib.sha256(raw).hexdigest()


def _resolve_coordinates(
    store: object, coordinates_reader: CoordinatesReader
) -> tuple[str, int, str]:
    """Resolve the owner coordinates from the one bounded, validated snapshot.

    ``read_owner_binding`` is the only authority for the note route's
    coordinates. The injected ``coordinates_reader`` is deliberately NOT
    consulted: it is the Agent transport's helper and reads the entry whole, so
    calling it would put an unbounded read behind the bound. A file that grows
    between the two observations defeats a size check placed in front of an
    unbounded read, which is exactly the gap this avoids. The Agent helper and
    its own callers are untouched; only the note route stops relying on it.

    Presence has been established before this point, so a vanished or
    unreadable entry here is an invalid owner state, not permission to write
    locally, and it is never cleaned up.
    """

    return read_owner_binding(store)

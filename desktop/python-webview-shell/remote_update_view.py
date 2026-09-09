"""Mountable native view session for remote update and owner recovery.

The page HTML comes from ``remote_update_presentation``. This module admits
one bounded ``chrome.webview`` request against the page the host just loaded.
It does not start updates, stop processes, or talk to the network.
"""

from __future__ import annotations

import base64
import json
import secrets
import threading
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Callable, Literal

from native_theme import theme_color
from remote_update_presentation import (
    ACTIONS,
    READ_ONLY_ACTIONS,
    REMOTE_UPDATE_CAPABILITY_BYTES,
    REMOTE_UPDATE_REQUEST_TYPE,
    SCHEMA_VERSION,
    THIS_PC_ACTIONS,
    RemoteUpdatePresentation,
    RemoteUpdateSnapshot,
    build_remote_update_html,
    normalize_remote_update_snapshot,
    present_remote_update,
    unknown_snapshot,
)


REMOTE_UPDATE_DOCUMENT_SOURCES = frozenset({"about:blank"})
NAVIGATE_TO_STRING_PREFIX = "data:text/html;charset=utf-8;base64,"
MAX_REMOTE_UPDATE_REQUEST_BYTES = 2048


@dataclass(frozen=True)
class RemoteUpdateRequest:
    request_id: str
    capability: str
    operation: str


@dataclass(frozen=True)
class RemoteUpdateAdmission:
    outcome: Literal["unbound", "close", "action", "ignored"]
    request: RemoteUpdateRequest | None = None
    snapshot: RemoteUpdateSnapshot | None = None


def mint_remote_update_capability() -> str:
    return secrets.token_hex(REMOTE_UPDATE_CAPABILITY_BYTES)


def remote_update_navigation_targets(page: str) -> frozenset[str]:
    inlined = base64.b64encode(page.encode("utf-8")).decode("ascii")
    return frozenset({NAVIGATE_TO_STRING_PREFIX + inlined} | REMOTE_UPDATE_DOCUMENT_SOURCES)


def parse_remote_update_request(message: str) -> RemoteUpdateRequest | None:
    if not isinstance(message, str):
        return None
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_REMOTE_UPDATE_REQUEST_BYTES:
        return None
    try:
        document = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    expected = {"type", "schema_version", "request_id", "capability", "operation"}
    if set(document) != expected:
        return None
    operation = document.get("operation")
    if (
        document.get("type") != REMOTE_UPDATE_REQUEST_TYPE
        or document.get("schema_version") != 1
        or operation not in ACTIONS
        or not _is_canonical_uuid(document.get("request_id"))
        or not _is_capability(document.get("capability"))
    ):
        return None
    return RemoteUpdateRequest(
        request_id=document["request_id"],
        capability=document["capability"],
        operation=operation,
    )


def remote_update_action_is_offered(snapshot: RemoteUpdateSnapshot, operation: str) -> bool:
    if operation == "close":
        return True
    return operation == present_remote_update(snapshot).next_action


class RemoteUpdateViewSession:
    """One on-screen page: the host renders HTML, this admits the click."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.generation = 0
        self.capability: str | None = None
        self.snapshot: RemoteUpdateSnapshot | None = None
        self.closed = False
        self.in_progress = False
        self._expected_documents: list[frozenset[str]] = []

    def show(
        self,
        snapshot: object,
        render: Callable[[str], None],
        *,
        theme: str = "dark",
        capability: str | None = None,
    ) -> bool:
        snap = snapshot if isinstance(snapshot, RemoteUpdateSnapshot) else normalize_remote_update_snapshot(snapshot)
        token = capability if capability is not None else mint_remote_update_capability()
        page = build_remote_update_html(snap, capability=token, theme=theme)
        with self._lock:
            if self.closed:
                return False
            self.generation += 1
            self.capability = token
            self.snapshot = snap
            self.in_progress = False
            self._expected_documents = [remote_update_navigation_targets(page)]
        try:
            render(page)
        except Exception:
            self.retire()
            return False
        return True

    def admit(self, message: str) -> RemoteUpdateAdmission:
        with self._lock:
            snapshot = self.snapshot
            if snapshot is None or self.closed:
                return RemoteUpdateAdmission("unbound")
            request = parse_remote_update_request(message)
            if request is None or not _is_capability(self.capability):
                return RemoteUpdateAdmission("unbound")
            if not secrets.compare_digest(self.capability, request.capability):
                return RemoteUpdateAdmission("unbound")
            if request.operation == "close":
                self._retire_locked(closed=True)
                return RemoteUpdateAdmission("close", request, snapshot)
            if self.in_progress or not remote_update_action_is_offered(
                snapshot, request.operation
            ):
                return RemoteUpdateAdmission("ignored", request, snapshot)
            self.in_progress = True
            return RemoteUpdateAdmission("action", request, snapshot)

    def admits_navigation(self, target: str) -> bool:
        with self._lock:
            if self.closed or self.snapshot is None:
                return False
            candidates = {target}
            if "%" in target:
                candidates.add(urllib.parse.unquote(target))
            for index, armed in enumerate(self._expected_documents):
                if candidates & armed:
                    del self._expected_documents[index]
                    return True
            return False

    def retire(self, *, closed: bool = False) -> None:
        with self._lock:
            self._retire_locked(closed=closed)

    def _retire_locked(self, *, closed: bool) -> None:
        self.snapshot = None
        self.capability = None
        self.in_progress = False
        self.generation += 1
        self._expected_documents.clear()
        self.closed = self.closed or closed


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.int != 0 and str(parsed) == value


def _is_capability(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == REMOTE_UPDATE_CAPABILITY_BYTES * 2
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "ACTIONS",
    "READ_ONLY_ACTIONS",
    "THIS_PC_ACTIONS",
    "MAX_REMOTE_UPDATE_REQUEST_BYTES",
    "NAVIGATE_TO_STRING_PREFIX",
    "REMOTE_UPDATE_CAPABILITY_BYTES",
    "REMOTE_UPDATE_DOCUMENT_SOURCES",
    "REMOTE_UPDATE_REQUEST_TYPE",
    "SCHEMA_VERSION",
    "RemoteUpdateAdmission",
    "RemoteUpdatePresentation",
    "RemoteUpdateRequest",
    "RemoteUpdateSnapshot",
    "RemoteUpdateViewSession",
    "build_remote_update_html",
    "mint_remote_update_capability",
    "normalize_remote_update_snapshot",
    "parse_remote_update_request",
    "present_remote_update",
    "remote_update_action_is_offered",
    "remote_update_navigation_targets",
    "theme_color",
    "unknown_snapshot",
]

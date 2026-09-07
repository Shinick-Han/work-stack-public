"""Fail-closed exclusive-local authority: one real writer lease, never metadata."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

from workstack.agent_authority import admit_authority
from workstack.store import DEFAULTS, JOURNAL_NAME, Store, _FileLease


EXCLUSIVE_LOCAL_HELD = "exclusive_local_held"
OWNER_ROUTE_REQUIRED = "owner_route_required"
WORKSPACE_MISMATCH = "workspace_mismatch"
RECOVERY_OR_SYNC_BLOCKED = "recovery_or_sync_blocked"
OWNER_UNAVAILABLE = "owner_unavailable"
COMMIT_UNKNOWN = "commit_unknown"

AUTHORITY_STATES = frozenset(
    {
        EXCLUSIVE_LOCAL_HELD,
        OWNER_ROUTE_REQUIRED,
        WORKSPACE_MISMATCH,
        RECOVERY_OR_SYNC_BLOCKED,
        OWNER_UNAVAILABLE,
        COMMIT_UNKNOWN,
    }
)
_COMMIT_UNKNOWN_MUTATIONS = frozenset({"possible_send", "unknown"})
_IDLE_MUTATIONS = frozenset({None, "none"})

T = TypeVar("T")


__all__ = (
    "AUTHORITY_STATES",
    "COMMIT_UNKNOWN",
    "EXCLUSIVE_LOCAL_HELD",
    "OWNER_ROUTE_REQUIRED",
    "OWNER_UNAVAILABLE",
    "OwnerAuthority",
    "RECOVERY_OR_SYNC_BLOCKED",
    "WORKSPACE_MISMATCH",
    "acquire_owner_authority",
)


def _metadata_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _admission_state(
    data_dir: Path, expected_workspace_uid: str
) -> tuple[Any | None, str | None]:
    try:
        return (
            admit_authority(
                data_dir=data_dir, expected_workspace_uid=expected_workspace_uid
            ),
            None,
        )
    except ValueError as error:
        code = error.args[0] if error.args else ""
        if code == "workspace_mismatch":
            return None, WORKSPACE_MISMATCH
        return None, RECOVERY_OR_SYNC_BLOCKED


def _authoritative_out_of_sync(store: Store) -> bool:
    try:
        payload = json.loads(store.store_manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return True
    files = payload.get("files") if isinstance(payload, dict) else None
    if type(files) is not dict:
        return True
    for name in DEFAULTS:
        try:
            body = store.path(name).read_bytes()
        except OSError:
            return True
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if files.get(name) != digest:
            return True
    return False


@dataclass
class OwnerAuthority:
    """Caller-owned authority decision. Release an acquired lease exactly once."""

    state: str
    store: Store | None = None
    lease: _FileLease | None = None
    workspace_uid: str | None = None
    data_dir: Path | None = None
    _released: bool = field(default=False, repr=False, compare=False)

    def release(self) -> None:
        if self._released:
            return
        store = self.store
        lease = self.lease
        self.lease = None
        self._released = True
        if store is not None and lease is not None:
            store.release_writer_lease(lease)

    def run(self, operation: Callable[[Store], T]) -> T | None:
        if self.state != EXCLUSIVE_LOCAL_HELD or self.store is None:
            return None
        try:
            result = operation(self.store)
        except BaseException:
            try:
                self.release()
            except BaseException:
                pass
            raise
        self.release()
        return result

    def __enter__(self) -> "OwnerAuthority":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc is None:
            self.release()
            return False
        try:
            self.release()
        except BaseException:
            pass
        return False


def acquire_owner_authority(
    *,
    data_dir: Path | str,
    expected_workspace_uid: str,
    owner_mutation: str | None = None,
    store_factory: Callable[..., Store] = Store,
) -> OwnerAuthority:
    """Attempt the real writer lease once. Metadata never grants local writes."""

    root = Path(data_dir)
    if owner_mutation in _COMMIT_UNKNOWN_MUTATIONS:
        return OwnerAuthority(state=COMMIT_UNKNOWN, data_dir=root)
    if owner_mutation not in _IDLE_MUTATIONS:
        return OwnerAuthority(state=RECOVERY_OR_SYNC_BLOCKED, data_dir=root)
    admission, error_state = _admission_state(root, expected_workspace_uid)
    if error_state is not None:
        return OwnerAuthority(state=error_state, data_dir=root)
    workspace_uid = admission.workspace_uid
    admitted_root = admission.data_dir
    if (admitted_root / JOURNAL_NAME).exists():
        return OwnerAuthority(
            state=RECOVERY_OR_SYNC_BLOCKED,
            workspace_uid=workspace_uid,
            data_dir=admitted_root,
        )
    store = store_factory(root=admitted_root)
    if _authoritative_out_of_sync(store):
        return OwnerAuthority(
            state=RECOVERY_OR_SYNC_BLOCKED,
            store=store,
            workspace_uid=workspace_uid,
            data_dir=admitted_root,
        )
    lease = store.try_acquire_writer_lease()
    if lease is None:
        if _metadata_present(store.server_info_path):
            return OwnerAuthority(
                state=OWNER_ROUTE_REQUIRED,
                store=store,
                workspace_uid=workspace_uid,
                data_dir=admitted_root,
            )
        return OwnerAuthority(
            state=OWNER_UNAVAILABLE,
            store=store,
            workspace_uid=workspace_uid,
            data_dir=admitted_root,
        )
    return OwnerAuthority(
        state=EXCLUSIVE_LOCAL_HELD,
        store=store,
        lease=lease,
        workspace_uid=workspace_uid,
        data_dir=admitted_root,
    )

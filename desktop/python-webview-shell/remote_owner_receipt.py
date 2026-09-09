"""The owner receipt itself: its bytes, its bounded fields, its publication.

One receipt records which process, on which host and which boot, is serving one
data directory.  This module is the whole byte-level contract for that record
and nothing else: the exact key set a receipt may carry, the bounds every field
is admitted under, the atomic no-overwrite publication, and the removal that
only fires while the receipt is still the exact one that was judged.

It decides no authority.  Whether a receipt is this host's, whether its process
is alive, and whether a caller may stop it are questions about identity and
liveness, and they are answered above in ``remote_owner``.  What lives here
cannot read ``/proc``, cannot open a process and cannot signal anything.

Every operation refuses through :class:`EntryError`, the bounded remote error
code the entry point turns into an exit status, so a malformed receipt, an
oversized payload, a contended guard and a filesystem that cannot publish are
four distinct refusals rather than one traceback.  Mutations run inside the
caller's hold of ``remote_receipt_guard``; this module takes no guard of its
own and never touches the real writer lease.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from remote_receipt_guard import (
    GuardContended,
    GuardUnavailable,
    GuardWait,
    owner_receipt_guard,
)
from remote_receipt_io import (
    ReceiptAlreadyExists,
    ReceiptPublicationUnavailable,
    publish_bytes_exclusive,
)


OWNER_KEYS = (
    "workspace_id",
    "data_dir_digest",
    "app_dir_digest",
    "pid",
    "start_identity",
    "release_id",
    "token_hash",
)
# A receipt carries exactly OWNER_KEYS or exactly FENCED_OWNER_KEYS.
OWNER_FENCING_KEYS = ("host_identity", "boot_identity")
FENCED_OWNER_KEYS = OWNER_KEYS + OWNER_FENCING_KEYS
MAX_OWNER_BYTES = 4096
MAX_IDENTITY_VALUE_LENGTH = 64
OWNER_FILENAME = ".workstack-remote-owner.json"
SHA256_HEX_LENGTH = 64

UNFENCED_RECOVERY_DETAIL = (
    "legacy owner receipt has no host or boot identity; confirm no remote owner "
    f"is serving this data directory from any host, then remove {OWNER_FILENAME}"
)


class EntryError(Exception):
    def __init__(self, code: str, detail: str = "", reason: str = "") -> None:
        self.code = code
        # Bounded symbolic discriminator carried up from the process handle,
        # so a caller can tell "this kernel has no pidfd" from "this pid may
        # not be signalled" without parsing the message text.
        self.reason = reason
        super().__init__(code if not detail else f"{code}: {detail}")


@dataclass(frozen=True)
class OwnerReceipt:
    workspace_id: str
    data_dir_digest: str
    app_dir_digest: str
    pid: int
    start_identity: str
    release_id: str
    token_hash: str
    # Absent in a pre-fencing receipt, and absent means unknown host.
    host_identity: str | None = None
    boot_identity: str | None = None

    @property
    def is_fenced(self) -> bool:
        return self.host_identity is not None and self.boot_identity is not None


def owner_receipt_path(data_dir: Path) -> Path:
    return data_dir / OWNER_FILENAME


def _sha256_hex(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != SHA256_HEX_LENGTH:
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"{field} is invalid")
    if any(character not in "0123456789abcdef" for character in value):
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"{field} is invalid")
    return value


def _bounded_label(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTITY_VALUE_LENGTH:
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"owner {field} is invalid")
    if any(ord(character) < 32 for character in value):
        raise EntryError("REMOTE_PROTOCOL_INVALID", f"owner {field} is invalid")
    return value


def _bounded_pid(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner pid is invalid")
    return value


def _owner_body(payload: bytes) -> dict[str, object]:
    if len(payload) > MAX_OWNER_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt too large")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt is not JSON") from error
    if not isinstance(value, dict):
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt shape")
    if set(value) not in ({*OWNER_KEYS}, {*FENCED_OWNER_KEYS}):
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt shape")
    return value


def parse_owner_receipt(payload: bytes) -> OwnerReceipt:
    value = _owner_body(payload)
    fenced = "host_identity" in value
    return OwnerReceipt(
        workspace_id=_bounded_label(value["workspace_id"], "workspace identity"),
        data_dir_digest=_sha256_hex(value["data_dir_digest"], "data_dir_digest"),
        app_dir_digest=_sha256_hex(value["app_dir_digest"], "app_dir_digest"),
        pid=_bounded_pid(value["pid"]),
        start_identity=_bounded_label(value["start_identity"], "start identity"),
        release_id=_bounded_label(value["release_id"], "release identity"),
        token_hash=_sha256_hex(value["token_hash"], "token_hash"),
        host_identity=_sha256_hex(value["host_identity"], "host_identity") if fenced else None,
        boot_identity=_sha256_hex(value["boot_identity"], "boot_identity") if fenced else None,
    )


def _read_owner_bytes(data_dir: Path) -> bytes | None:
    path = owner_receipt_path(data_dir)
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_OWNER_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt is unreadable") from error
    if len(payload) > MAX_OWNER_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt too large")
    return payload


def read_owner_receipt_bytes(data_dir: Path) -> tuple[OwnerReceipt, bytes] | None:
    """Read the receipt with the exact bytes every later removal is judged on."""

    payload = _read_owner_bytes(data_dir)
    if payload is None:
        return None
    return parse_owner_receipt(payload), payload


def read_owner_receipt(data_dir: Path) -> OwnerReceipt | None:
    found = read_owner_receipt_bytes(data_dir)
    return None if found is None else found[0]


def encode_owner_receipt(receipt: OwnerReceipt) -> bytes:
    if not receipt.is_fenced:
        raise EntryError(
            "REMOTE_PROTOCOL_INVALID", "owner receipt is missing host or boot identity"
        )
    encoded = json.dumps(
        {
            "workspace_id": receipt.workspace_id,
            "data_dir_digest": receipt.data_dir_digest,
            "app_dir_digest": receipt.app_dir_digest,
            "pid": receipt.pid,
            "start_identity": receipt.start_identity,
            "release_id": receipt.release_id,
            "token_hash": receipt.token_hash,
            "host_identity": receipt.host_identity,
            "boot_identity": receipt.boot_identity,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(encoded) > MAX_OWNER_BYTES:
        raise EntryError("REMOTE_PROTOCOL_INVALID", "owner receipt too large")
    return encoded


@contextmanager
def _receipt_guard(data_dir: Path, guard: GuardWait) -> Iterator[None]:
    """Hold the receipt guard, reporting a refusal in the remote error codes."""

    try:
        with owner_receipt_guard(data_dir, guard):
            yield
    except GuardContended as error:
        raise EntryError("REMOTE_LOCK_OWNED", str(error)) from error
    except GuardUnavailable as error:
        raise EntryError("REMOTE_PROTOCOL_INVALID", str(error)) from error


def write_owner_receipt_exclusive(data_dir: Path, receipt: OwnerReceipt) -> None:
    """Create the receipt, refusing an existing one. Callers hold the guard."""

    encoded = encode_owner_receipt(receipt)
    try:
        publish_bytes_exclusive(owner_receipt_path(data_dir), encoded)
    except ReceiptAlreadyExists as error:
        raise EntryError("REMOTE_LOCK_OWNED", "pid=unknown") from error
    except ReceiptPublicationUnavailable as error:
        raise EntryError(
            "REMOTE_PROTOCOL_INVALID",
            str(error) or "owner receipt could not be published",
        ) from error

"""Read-only verification of an already-confirmed local Store workspace rebind.

The host must not adopt a replaced workspace on the strength of a frontend
completion string. Before the connection registry's authority metadata is
touched, this module independently re-reads what the Store itself persisted when
the user confirmed the rebind, and then checks the **actual current** contents of
the selected directory against that confirmed baseline.

Both halves are needed. The runtime receipt and manifest are historical records:
matching them proves a confirmation happened, not that the directory still holds
what was confirmed. If a third valid workspace replaces every authoritative file
after the confirmation was written, those records are untouched and still
describe the confirmed candidate. So the identity is re-read from the live
``workspace.json`` and every authoritative file is re-hashed against the
baseline the receipt recorded.

Nothing here constructs or initializes a ``Store``. ``Store.__init__`` mkdir's
both its data root and its runtime root, which makes it unusable as a read-only
probe, so the runtime-root derivation is mirrored from ``workstack/store.py``
instead. Pure validators are imported from that module and reused rather than
re-implemented. Every function is pure with respect to the filesystem: it reads,
it never writes, creates, repairs or removes anything, and it never synthesizes,
adopts or repairs a baseline.
"""

from __future__ import annotations

import hashlib
import json
import os
import types
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# Mirrors workstack/store.py. Kept as literals so this read-only helper never
# imports the Store module into the desktop host process.
STORE_MANIFEST_NAME = ".workstack-store-manifest.json"
SYNC_REBIND_RECEIPT_NAME = ".workstack-sync-rebind-receipt.json"
RUNTIME_KEY_LENGTH = 20

# The Store rebind receipt is a small fixed record; the manifest lists files and
# task semantics. Neither is unbounded input to this helper.
RECEIPT_READ_LIMIT = 1 * 1024 * 1024
MANIFEST_READ_LIMIT = 4 * 1024 * 1024

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

# Every field this module reads from the receipt. A key that is indexed but not
# listed here would surface as KeyError, which is a LookupError and so escapes
# the host's (RuntimeError, OSError, ValueError) handling entirely, leaving no
# status posted. Keep this tuple and the reads below in step.
REQUIRED_RECEIPT_FIELDS = (
    "schema_version",
    "operation",
    "idempotency_key",
    "previous_workspace_id",
    "candidate_workspace_id",
    "manifest_digest",
    "candidate_digest",
    "result_manifest_digest",
    "authoritative_files",
    "planning_mutated",
)


class LocalRebindEvidenceError(RuntimeError):
    """The persisted evidence does not support a confirmed local rebind."""


WORKSPACE_IDENTITY_FILE = "workspace.json"

# One authoritative planning file. The Store rejects anything larger long before
# this, so the bound only stops an unbounded read of a corrupted directory.
AUTHORITATIVE_FILE_READ_LIMIT = 64 * 1024 * 1024


@dataclass(frozen=True)
class ConfirmedLocalRebind:
    """What the Store persisted AND what the directory actually holds now."""

    previous_workspace_id: str
    candidate_workspace_id: str
    result_manifest_digest: str
    idempotency_key: str
    receipt_path: Path
    manifest_path: Path
    verified_data_dir: Path
    verified_file_count: int


def _pure_store_validators():
    """Import the Store module's pure helpers without constructing a Store.

    Importing the module is explicitly permitted; only constructing or
    initializing a ``Store`` is not. Reusing these keeps the receipt shape, the
    manifest shape and the authoritative-file set defined in exactly one place.
    """

    from workstack.store import (
        StoreCorruptError,
        _validate_store_manifest_files,
        _validate_store_manifest_header,
        _validate_store_manifest_tasks,
        _validated_rebind_file_records,
    )

    return types.SimpleNamespace(
        StoreCorruptError=StoreCorruptError,
        validate_manifest_header=_validate_store_manifest_header,
        validate_manifest_files=_validate_store_manifest_files,
        validate_manifest_tasks=_validate_store_manifest_tasks,
        validate_file_records=_validated_rebind_file_records,
    )


def _bound_baseline(
    receipt_records: Mapping[str, Mapping[str, object]],
    manifest_files: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """Bind the receipt's file roster to the manifest the digest actually covers.

    ``result_manifest_digest`` binds the manifest, not the receipt. Taking the
    file baseline from the receipt alone therefore trusts a record nothing
    covers: editing a file and its own receipt entry leaves the manifest intact
    and the digest check satisfied. Requiring the two rosters to agree exactly
    makes that combination self-contradictory, and it refuses.
    """

    if set(receipt_records) != set(manifest_files):
        raise LocalRebindEvidenceError(
            "the rebind receipt file roster does not match the confirmed manifest"
        )
    for name in sorted(receipt_records):
        if receipt_records[name]["sha256"] != manifest_files[name]:
            raise LocalRebindEvidenceError(
                f"the rebind receipt and the confirmed manifest disagree about {name}"
            )
    return {name: dict(record) for name, record in receipt_records.items()}


def _canonical_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise LocalRebindEvidenceError(f"{field} must be a canonical non-nil UUID")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as error:
        raise LocalRebindEvidenceError(
            f"{field} must be a canonical non-nil UUID"
        ) from error
    if str(parsed) != value or parsed.int == 0:
        raise LocalRebindEvidenceError(f"{field} must be a canonical non-nil UUID")
    return value


def derive_store_runtime_root(
    data_dir: Path | str, environment: Mapping[str, str] | None = None
) -> Path:
    """Reproduce ``Store`` runtime-root derivation without constructing one.

    Mirrors ``workstack/store.py``: the runtime base is ``WORK_STACK_RUNTIME``
    when set, else ``LOCALAPPDATA/WorkStack/runtime``, else the POSIX state
    directory; the per-store subdirectory is the first 20 hex characters of the
    SHA-256 of the case-normalized resolved data root.
    """

    environment = os.environ if environment is None else environment
    root = Path(data_dir).expanduser().resolve()
    override = environment.get("WORK_STACK_RUNTIME")
    if override:
        base = Path(override).expanduser().resolve()
    else:
        local_app_data = environment.get("LOCALAPPDATA")
        if local_app_data:
            base = (Path(local_app_data) / "WorkStack" / "runtime").resolve()
        else:
            base = (Path.home() / ".local" / "state" / "workstack").resolve()
    key = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
    return base / key[:RUNTIME_KEY_LENGTH]


def _read_evidence(path: Path, limit: int, label: str) -> tuple[dict[str, object], bytes]:
    """Read an evidence record ONCE and return both the document and its bytes.

    The exact bytes that were parsed are returned so the caller can compare the
    final stability read against them. Taking a separate "before" snapshot after
    parsing would leave a window in which a change is invisible: the later reads
    would agree with each other while disagreeing with the evidence that was
    actually validated.
    """

    raw = _read_bytes_bounded(path, limit, label)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalRebindEvidenceError(f"{label} is not valid JSON") from error
    if not isinstance(document, dict):
        raise LocalRebindEvidenceError(f"{label} is not a JSON object")
    return document, raw


def _read_json_bounded(path: Path, limit: int, label: str) -> dict[str, object]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except FileNotFoundError as error:
        raise LocalRebindEvidenceError(f"{label} is missing") from error
    except OSError as error:
        raise LocalRebindEvidenceError(f"{label} is unreadable") from error
    if len(raw) > limit:
        raise LocalRebindEvidenceError(f"{label} exceeds the supported size")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalRebindEvidenceError(f"{label} is not valid JSON") from error
    if not isinstance(document, dict):
        raise LocalRebindEvidenceError(f"{label} is not a JSON object")
    return document


def manifest_digest(manifest: Mapping[str, object]) -> str:
    """Digest a store manifest exactly as ``Store._manifest_digest`` does.

    Mirrors ``workstack/store.py``: compact JSON with ``ensure_ascii=False``,
    ``(",", ":")`` separators and sorted keys. Any divergence here would silently
    reject a genuine confirmation, so the encoding is kept byte-identical.
    """

    payload = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise LocalRebindEvidenceError(f"{field} must be a sha256 digest")
    return value


def _read_bytes_bounded(path: Path, limit: int, label: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except FileNotFoundError as error:
        raise LocalRebindEvidenceError(f"{label} is missing") from error
    except OSError as error:
        raise LocalRebindEvidenceError(f"{label} is unreadable") from error
    if len(raw) > limit:
        raise LocalRebindEvidenceError(f"{label} exceeds the supported size")
    return raw


def _verify_actual_authority(
    data_dir: Path, candidate: str, baseline: Mapping[str, Mapping[str, object]]
) -> int:
    """Check what the selected directory holds NOW against the confirmed baseline.

    The receipt records the exact authoritative files the confirmed rebind
    produced. Re-hashing the live directory against that baseline rejects both
    a wholesale replacement by another valid workspace and a same-identity
    content divergence, neither of which the runtime records can reveal.

    Read-only: no file is written, created, repaired or adopted, and a mismatch
    refuses rather than resynchronizing anything.
    """

    identity_path = data_dir / WORKSPACE_IDENTITY_FILE
    identity = _read_json_bounded(
        identity_path, RECEIPT_READ_LIMIT, "workspace identity file"
    )
    actual = _canonical_uuid(identity.get("id"), "workspace.json id")
    if actual != candidate:
        raise LocalRebindEvidenceError(
            "the selected directory now holds a different workspace identity "
            "than the confirmed rebind; refusing to update the registry"
        )

    for name in sorted(baseline):
        record = baseline[name]
        body = _read_bytes_bounded(
            data_dir / name, AUTHORITATIVE_FILE_READ_LIMIT, f"authoritative file {name}"
        )
        if len(body) != record["size"]:
            raise LocalRebindEvidenceError(
                f"authoritative file {name} diverged from the confirmed baseline"
            )
        if "sha256:" + hashlib.sha256(body).hexdigest() != record["sha256"]:
            raise LocalRebindEvidenceError(
                f"authoritative file {name} diverged from the confirmed baseline"
            )
    return len(baseline)


def read_confirmed_local_rebind(
    data_dir: Path | str,
    *,
    expected_previous_workspace_id: str,
    expected_candidate_workspace_id: str,
    environment: Mapping[str, str] | None = None,
) -> ConfirmedLocalRebind:
    """Re-read the Store's own rebind evidence and check it supports adoption.

    Raises ``LocalRebindEvidenceError`` unless all of the following hold:

    * a well-formed ``workspace-rebind`` receipt exists for this data directory
      and reports that planning bytes were not mutated;
    * the receipt's previous and candidate identities are exactly the ones the
      caller expects, so a receipt from some other rebind cannot be reused;
    * the Store manifest now carries the candidate identity, so a completion
      message alone cannot adopt a workspace the Store never rebound;
    * the manifest digest matches the receipt's recorded result;
    * the selected directory's live ``workspace.json`` still names the candidate,
      and every authoritative file still matches the confirmed baseline, so a
      replacement by another valid workspace, or a same-identity content
      divergence, refuses instead of being adopted; and
    * the receipt and manifest bytes did not change while that snapshot was
      taken, so the evidence checked is the evidence relied on.

    The directory this succeeded against is returned so the caller can bind the
    same directory into the registry compare-and-swap. This is a bounded
    read-only check between two writers on one machine, not a cross-filesystem
    atomic guarantee against an arbitrary external writer.
    """

    previous = _canonical_uuid(
        expected_previous_workspace_id, "expected_previous_workspace_id"
    )
    candidate = _canonical_uuid(
        expected_candidate_workspace_id, "expected_candidate_workspace_id"
    )
    if previous == candidate:
        raise LocalRebindEvidenceError(
            "a confirmed rebind requires a changed workspace identity"
        )

    runtime_root = derive_store_runtime_root(data_dir, environment)
    receipt_path = runtime_root / SYNC_REBIND_RECEIPT_NAME
    manifest_path = runtime_root / STORE_MANIFEST_NAME

    # Each evidence record is read exactly once. These are the bytes that get
    # parsed, validated and later re-compared; no separate snapshot is taken.
    receipt, receipt_raw = _read_evidence(
        receipt_path, RECEIPT_READ_LIMIT, "rebind receipt"
    )
    missing = [field for field in REQUIRED_RECEIPT_FIELDS if field not in receipt]
    if missing:
        raise LocalRebindEvidenceError("rebind receipt is incomplete")
    if receipt.get("schema_version") != 1 or receipt.get("operation") != "workspace-rebind":
        raise LocalRebindEvidenceError("rebind receipt is not a workspace rebind record")
    if receipt.get("planning_mutated") is not False:
        raise LocalRebindEvidenceError("rebind receipt reports mutated planning bytes")
    key = receipt.get("idempotency_key")
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key) is None:
        raise LocalRebindEvidenceError("rebind receipt idempotency key is invalid")

    if _canonical_uuid(receipt.get("previous_workspace_id"), "receipt previous") != previous:
        raise LocalRebindEvidenceError(
            "the confirmed rebind was recorded against a different previous workspace"
        )
    if _canonical_uuid(receipt.get("candidate_workspace_id"), "receipt candidate") != candidate:
        raise LocalRebindEvidenceError(
            "the confirmed rebind was recorded for a different candidate workspace"
        )
    result_digest = _require_digest(
        receipt.get("result_manifest_digest"), "result_manifest_digest"
    )

    manifest, manifest_raw = _read_evidence(
        manifest_path, MANIFEST_READ_LIMIT, "store manifest"
    )
    if _canonical_uuid(manifest.get("workspace_id"), "manifest workspace_id") != candidate:
        raise LocalRebindEvidenceError(
            "the Store does not currently carry the confirmed candidate identity"
        )
    actual_digest = manifest_digest(manifest)
    if actual_digest != result_digest:
        raise LocalRebindEvidenceError(
            "the Store manifest changed after the confirmed rebind was recorded"
        )

    # Validate both records with the Store's own pure validators, then bind the
    # receipt roster to the manifest that result_manifest_digest actually covers.
    store = _pure_store_validators()
    try:
        store.validate_manifest_header(manifest)
        store.validate_manifest_files(manifest.get("files"))
        store.validate_manifest_tasks(manifest.get("tasks"))
        records = store.validate_file_records(receipt["authoritative_files"])
    except store.StoreCorruptError as error:
        raise LocalRebindEvidenceError(
            "the confirmed rebind evidence is malformed"
        ) from error
    baseline = _bound_baseline(records, manifest["files"])

    # The records above are historical. This checks what the selected directory
    # actually holds right now, against that bound baseline.
    resolved_data_dir = Path(data_dir).expanduser().resolve()
    verified_files = _verify_actual_authority(resolved_data_dir, candidate, baseline)

    # Evidence stability, compared against the EXACT bytes that were parsed and
    # validated above. A change landing during verification refuses; there is no
    # retry or polling, and this is detection during verification rather than a
    # guarantee about what happens after it.
    if _read_bytes_bounded(receipt_path, RECEIPT_READ_LIMIT, "rebind receipt") != receipt_raw:
        raise LocalRebindEvidenceError(
            "the rebind receipt changed while the directory was being verified"
        )
    if _read_bytes_bounded(manifest_path, MANIFEST_READ_LIMIT, "store manifest") != manifest_raw:
        raise LocalRebindEvidenceError(
            "the store manifest changed while the directory was being verified"
        )

    return ConfirmedLocalRebind(
        previous_workspace_id=previous,
        candidate_workspace_id=candidate,
        result_manifest_digest=result_digest,
        idempotency_key=key,
        receipt_path=receipt_path,
        manifest_path=manifest_path,
        verified_data_dir=resolved_data_dir,
        verified_file_count=verified_files,
    )

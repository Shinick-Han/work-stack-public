"""The two facts about an update attempt a desktop restart cannot re-derive.

The update journal already carries the mutation identities in flight, and it is
the authority on them.  Two things sit outside it, and both are the host's to
keep:

``update_attempt_id``
    A journal instance is built *from* its binding, so a restart has to know
    which attempt it is resuming before it can open the journal at all.  Minting
    a fresh id instead would make the journal refuse its own record as another
    attempt's, and a pending activation would never be resumable.

``restore_source``
    ``RestoreSource`` names the verified archive a restore would put back.  Its
    own contract says so: the archive lives under the *backup's* operation
    directory, the backup identity is settled and gone by activation time, and
    the port takes the value back through its constructor.

Both live in one small file beside the desktop state, written the way the
registry and the journal write: an exclusively created temporary file in the
destination directory, flushed, fsynced, then moved onto the destination.

``current_app_dir``
    Which application directory this attempt started from.  Once the activation
    has moved the registry, the selected profile *is* the candidate, and a
    restart that re-derived a candidate from it would name a directory nobody
    ever staged.  The pre-activation application is therefore recorded while it
    is still the selected one.

``prepared``
    Which operation staged the candidate, and the artifact identity it was
    staged from.  It is not proof: it says which receipt the install port must
    go back and re-verify before a restarted process may read the candidate.

``endpoint_fingerprint``
    Which remote endpoint produced all of the above.  A profile id is a name an
    operator may re-point: the same id can be edited from one host, data root,
    forwarded port or interpreter to another and still be selected.  Every
    field above was produced *against* one endpoint, so the record carries one
    canonical digest of that endpoint and refuses to speak for another.  The
    two application directories are deliberately outside it -- moving from the
    current application to the target is the attempt's own intended effect --
    and so are the session token and its generation, which rotate on a
    reconnect without the endpoint having changed at all.

Nothing here is authority.  The recovered attempt id only *addresses* a journal;
the journal's own binding digest still decides whether that record belongs to
this attempt, and refuses when it does not.

A record that cannot be understood is a *third* answer, and the distinction is
the point: an absent record means no attempt was ever begun here, while an
unreadable, oversized, replaced or unstattable one means an attempt may have
been begun and its evidence lost.  The first may start a fresh attempt; the
second must not, because an external effect could have survived the evidence.
Neither ever repairs, rewrites or deletes the bytes it could not read.

"Replaced" is checked, not assumed.  The read opens one handle, without
following a link where the platform offers that, and settles the whole answer
on *that* handle: the path is stat'ed before the open, the open handle is
stat'ed again, and the handle is stat'ed once more after the bytes are read.
A file identity that is not the same one across all three is a replacement, and
a replacement is ambiguous -- never a ``present`` record read out of whichever
file the second lookup happened to land on.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
_ROOT = str(Path(_SHELL_DIR).parents[1])
for _path in (_SHELL_DIR, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from remote_update_backup_port import RestoreSource  # noqa: E402
from remote_update_journal import (  # noqa: E402
    JournalUnavailable,
    remote_update_journal_lock,
)


SESSION_FILE = "remote-update-session.json"
#: Version 3 binds the record to the endpoint that produced it.  A version 2
#: document carries no such binding and therefore cannot be shown to belong to
#: the endpoint now selected, so it is not admitted -- and, being present but
#: unadmitted, it is ambiguous rather than absent, exactly like any other
#: record this process cannot understand.
SESSION_SCHEMA_VERSION = "remote-update-session/3"
MAX_SESSION_BYTES = 4096

#: The three answers a read can give.  ``ambiguous`` is not ``absent``.
STATE_ABSENT = "absent"
STATE_PRESENT = "present"
STATE_AMBIGUOUS = "ambiguous"

_OPAQUE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
#: The adapter's own digest spelling, prefix included.
_DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_KEYS = frozenset(
    {
        "schema_version",
        "workspace_id",
        "profile_id",
        "endpoint_fingerprint",
        "update_attempt_id",
        "target_app_dir",
        "current_app_dir",
        "prepared",
        "restore_source",
    }
)
_PREPARED_KEYS = {"operation_id", "artifact_digest", "artifact_manifest_sha256"}

#: The fingerprint joins its parts on a character no connection field holds.
ENDPOINT_SEPARATOR = "\n"
#: Named, in this order, so the digest is one canonical spelling everywhere.
ENDPOINT_FIELDS = (
    "profile_id",
    "workspace_id",
    "ssh_host_alias",
    "remote_data_dir",
    "remote_port",
    "remote_python",
)


@dataclass(frozen=True)
class RemoteEndpoint:
    """The authority-relevant identity of one remote the ports address.

    Exactly the fields a port would have to be re-pointed at for its answers to
    be about a different machine or a different store: the selected profile and
    the workspace it claims, the SSH alias the transports open, the remote data
    root the maintenance helper works under, the forwarded port the owner
    observation speaks over, and the interpreter both helpers execute with.

    Neither application directory is here.  The whole point of an attempt is to
    move from the current application to the target, and folding either in
    would make the record stop speaking for itself the moment it succeeded.
    Nor is the session token or its generation: a reconnect rotates both while
    addressing the very same endpoint, and the durable evidence a previous
    process produced is still that endpoint's evidence.  Retiring the in-memory
    composition on such a rotation is a separate rule, and the host keeps it.
    """

    profile_id: str
    workspace_id: str
    ssh_host_alias: str
    remote_data_dir: str
    #: The port on the *remote*, which is part of what the alias addresses.
    #: The desktop's local forward port is not: it is reallocated on a restart
    #: and would make the same endpoint stop recognising its own evidence.
    remote_port: str
    remote_python: str

    def fingerprint(self) -> str:
        """One canonical digest, in the adapter's own ``sha256:`` spelling."""

        parts = tuple(str(getattr(self, name) or "") for name in ENDPOINT_FIELDS)
        joined = ENDPOINT_SEPARATOR.join(parts).encode("utf-8")
        return "sha256:" + hashlib.sha256(joined).hexdigest()



def endpoint_of(profile: object, remote: object) -> RemoteEndpoint | None:
    """The endpoint a selected registry profile and its runtime name together.

    The registry profile owns the identity and the addresses the transports
    open; the settled runtime profile owns the port the remote actually serves
    on and the interpreter the remote helpers execute with.  Either half
    missing is no endpoint at all, and therefore no durable binding.
    """

    if profile is None or remote is None:
        return None
    return RemoteEndpoint(
        profile_id=str(getattr(profile, "profile_id", "")),
        workspace_id=str(getattr(profile, "expected_workspace_id", "")),
        ssh_host_alias=str(getattr(profile, "ssh_host_alias", "")),
        remote_data_dir=str(getattr(profile, "remote_data_dir", "")),
        remote_port=str(getattr(remote, "remote_port", "")),
        remote_python=str(getattr(remote, "remote_python", "") or ""),
    )


@dataclass(frozen=True)
class PreparedCandidate:
    """Which operation staged the candidate, and from which admitted artifact."""

    operation_id: str
    artifact_digest: str
    artifact_manifest_sha256: str


@dataclass(frozen=True)
class RemoteUpdateSession:
    """Which attempt this desktop is in the middle of, and what it may restore."""

    workspace_id: str
    profile_id: str
    endpoint_fingerprint: str
    update_attempt_id: str
    target_app_dir: str
    current_app_dir: str
    prepared: PreparedCandidate | None = None
    restore_source: RestoreSource | None = None

    def speaks_for(
        self,
        *,
        workspace_id: str,
        profile_id: str,
        endpoint_fingerprint: str,
        live_app_dir: str,
    ) -> bool:
        """Whether this whole record belongs to the selection now in front of us.

        The workspace and the profile have to be this selection's, the endpoint
        has to be the one that produced the record, and the application the
        registry currently selects has to be one of the two this record itself
        names: the one the attempt started from, before the activation, or the
        candidate it moved to, after.  Anything else is another attempt's
        record, and adopting any part of it -- the attempt id, the staging, or
        the archive a restore would put back -- would bind this flow to
        evidence that is not its own.

        The endpoint check is what a profile id alone cannot do.  The same
        profile may be edited from one host, data root, forwarded port or
        interpreter to another and stay selected; the staging, the prepared
        receipt and the verified archive it names all live on the *previous*
        machine, and a flow speaking to the new one must never adopt them.
        """

        if self.workspace_id != workspace_id or self.profile_id != profile_id:
            return False
        if not endpoint_fingerprint or self.endpoint_fingerprint != endpoint_fingerprint:
            return False
        return live_app_dir in (self.current_app_dir, self.target_app_dir)

    def document(self) -> dict[str, object]:
        source = self.restore_source
        prepared = self.prepared
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "profile_id": self.profile_id,
            "endpoint_fingerprint": self.endpoint_fingerprint,
            "update_attempt_id": self.update_attempt_id,
            "target_app_dir": self.target_app_dir,
            "current_app_dir": self.current_app_dir,
            "prepared": (
                None
                if prepared is None
                else {
                    "operation_id": prepared.operation_id,
                    "artifact_digest": prepared.artifact_digest,
                    "artifact_manifest_sha256": prepared.artifact_manifest_sha256,
                }
            ),
            "restore_source": (
                None
                if source is None
                else {
                    "operation_id": source.operation_id,
                    "backup_digest": source.backup_digest,
                }
            ),
        }


@dataclass(frozen=True)
class SessionRead:
    """One read, and which of the three answers it is.

    ``identity`` is the file the answer was actually read out of, so a caller
    about to act on the record -- retiring it, say -- can prove the path still
    names that same file rather than one written since.
    """

    state: str
    session: RemoteUpdateSession | None = None
    identity: tuple[object, object, int] | None = None

    @property
    def ambiguous(self) -> bool:
        return self.state == STATE_AMBIGUOUS


def session_path(state_root: Path) -> Path:
    return Path(state_root) / SESSION_FILE


def new_attempt_id() -> str:
    return str(uuid.uuid4())


def _opaque(value: object) -> str | None:
    return value if isinstance(value, str) and _OPAQUE_ID.match(value) else None


def _restore_source(value: object) -> RestoreSource | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"operation_id", "backup_digest"}:
        return None
    operation_id = _opaque(value["operation_id"])
    digest = value["backup_digest"]
    if operation_id is None or not isinstance(digest, str) or not _DIGEST.match(digest):
        return None
    return RestoreSource(operation_id=operation_id, backup_digest=digest)


def _prepared(value: object) -> PreparedCandidate | None:
    if not isinstance(value, dict) or set(value) != _PREPARED_KEYS:
        return None
    operation_id = _opaque(value["operation_id"])
    digest = value["artifact_digest"]
    manifest = value["artifact_manifest_sha256"]
    if operation_id is None or not isinstance(digest, str) or not _DIGEST.match(digest):
        return None
    if not isinstance(manifest, str) or not _DIGEST.match(manifest):
        return None
    return PreparedCandidate(
        operation_id=operation_id,
        artifact_digest=digest,
        artifact_manifest_sha256=manifest,
    )


def _posix_directory(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 1024:
        return None
    return value


def admit_session(raw: object) -> RemoteUpdateSession | None:
    """Return the recorded attempt, or ``None`` for anything not understood."""

    if not isinstance(raw, dict) or set(raw) != _KEYS:
        return None
    if raw["schema_version"] != SESSION_SCHEMA_VERSION:
        return None
    workspace_id = _opaque(raw["workspace_id"])
    profile_id = _opaque(raw["profile_id"])
    attempt = _opaque(raw["update_attempt_id"])
    target = _posix_directory(raw["target_app_dir"])
    current = _posix_directory(raw["current_app_dir"])
    endpoint = raw["endpoint_fingerprint"]
    if workspace_id is None or profile_id is None or attempt is None:
        return None
    if not isinstance(endpoint, str) or not _DIGEST.match(endpoint):
        return None
    if target is None or current is None or target == current:
        return None
    for key, admit in (("restore_source", _restore_source), ("prepared", _prepared)):
        if raw[key] is not None and admit(raw[key]) is None:
            return None
    return RemoteUpdateSession(
        workspace_id=workspace_id,
        profile_id=profile_id,
        endpoint_fingerprint=endpoint,
        update_attempt_id=attempt,
        target_app_dir=target,
        current_app_dir=current,
        prepared=_prepared(raw["prepared"]),
        restore_source=_restore_source(raw["restore_source"]),
    )


#: ``O_NOFOLLOW`` where the platform has it, ``O_BINARY`` where it needs it.
#: Both are absent on the other platform, and both default to no bit at all --
#: the identity comparison below is what actually decides, on every platform.
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | _NO_FOLLOW


def _identity(info: os.stat_result) -> tuple[object, object, int]:
    """Which file this is: the device, the inode, and what kind of file it is.

    ``os.stat`` fills the first two on every platform this desktop runs on, and
    a link's own ``lstat`` identity is never its target's, so comparing this
    across an open catches a replacement even where ``O_NOFOLLOW`` is absent.
    """

    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _read_handle(handle: int, limit: int) -> bytes | None:
    """Read at most ``limit`` + 1 bytes from the open handle, or ``None``."""

    chunks: list[bytes] = []
    read = 0
    while read <= limit:
        try:
            chunk = os.read(handle, limit + 1 - read)
        except OSError:
            return None
        if not chunk:
            break
        chunks.append(chunk)
        read += len(chunk)
    return b"".join(chunks)


def _same_file_bytes(path: Path, before: os.stat_result) -> bytes | None:
    """The bytes of exactly the file ``before`` named, or ``None`` if replaced.

    One handle carries the read, and three lookups have to agree about which
    file it is: the ``lstat`` taken before the open, the handle's own ``fstat``
    before and after the bytes, and a fresh ``lstat`` of the path afterwards.
    The handle alone cannot see a *path* replacement -- an ``os.replace`` onto
    the name leaves the open handle pointing at the old, still perfectly
    readable inode -- so the final path lookup is what catches it.  A path that
    has stopped existing, become a link, or become another file is a
    replacement, and a replacement is not this record.
    """

    try:
        handle = os.open(path, _OPEN_FLAGS)
    except OSError:
        return None
    try:
        opened = os.fstat(handle)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            return None
        if opened.st_size > MAX_SESSION_BYTES:
            return None
        payload = _read_handle(handle, MAX_SESSION_BYTES)
        after = os.fstat(handle)
    except OSError:
        return None
    finally:
        try:
            os.close(handle)
        except OSError:
            pass
    if payload is None or len(payload) != opened.st_size:
        return None
    if _identity(after) != _identity(opened) or after.st_size != opened.st_size:
        return None
    try:
        settled = os.lstat(path)
    except OSError:
        return None
    return payload if _identity(settled) == _identity(opened) else None


def load_session(state_root: Path) -> SessionRead:
    """Read the record as one of three answers.  Never writes or repairs.

    Absent is only ever an absent path.  A path that is there but is not a
    plain readable file this process understands -- a symlink, a directory, an
    oversized or malformed document, an unreadable one -- is ambiguous, and the
    caller must treat it as evidence that may have been lost rather than as an
    attempt that never happened.

    The whole answer is settled on one handle.  The path is stat'ed without
    following a link, opened without following one where the platform offers
    that, and the open handle is stat'ed before and after its bytes are read.
    All three have to be the same file: a path that turned into a link, into
    another file, or into a differently sized one between the lookup and the
    read is a *replacement*, and a replacement is ambiguous.  Reading the path
    a second time, as a plain ``read_bytes`` does, would instead admit whatever
    the second lookup landed on as this attempt's own record.
    """

    path = session_path(state_root)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return SessionRead(STATE_ABSENT)
    except OSError:
        return SessionRead(STATE_AMBIGUOUS)
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_SESSION_BYTES:
        return SessionRead(STATE_AMBIGUOUS)
    payload = _same_file_bytes(path, before)
    if payload is None:
        return SessionRead(STATE_AMBIGUOUS)
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        return SessionRead(STATE_AMBIGUOUS)
    admitted = admit_session(raw)
    if admitted is None:
        return SessionRead(STATE_AMBIGUOUS)
    return SessionRead(STATE_PRESENT, admitted, _identity(before))


def read_session(state_root: Path) -> RemoteUpdateSession | None:
    """The admitted record, or ``None``.  Callers that must distinguish an
    ambiguous record from an absent one use :func:`load_session` instead."""

    return load_session(state_root).session


def changed_under_the_gate(state_root: Path, change: Callable[[], bool]) -> bool:
    """Run one *change* to the record under the gate all its writers share.

    Replacing the record and retiring it are both single writes on their own,
    but the retire is a read *and* a write: it admits the record, proves the
    path still names that same file, and only then unlinks.  Between the proof
    and the unlink an ordinary successor write could land -- a plain
    ``os.replace`` onto the name -- and the retire would delete a record the
    next process needs.  Re-stating the identity narrows that window; it does
    not close it, because nothing stops the successor from writing inside it.

    So both operations take one gate, and it is the journal's own
    ``remote_update_journal_lock`` on this same state root: the journal and
    this record describe the same attempt, taken with the same bounded,
    portable, non-blocking-with-a-deadline convention, over a persistent lock
    file nobody unlinks.  This is not the SSOT lock and not the registry's.

    It is never held across a journal write.  Every caller here is either the
    host's own record write, made from a port callback after the flow has
    already released the journal, or the completion hook, which runs after the
    anchor has been cleared.  Contention is a refusal, not a wait: a change
    that could not take the gate answers ``False`` and touches nothing.

    Reads do not take it.  :func:`load_session` settles its whole answer on one
    checked handle and calls a replacement ambiguous, which is the truthful
    answer for a reader whether or not a writer was holding anything.
    """

    try:
        with remote_update_journal_lock(state_root):
            return change()
    except (JournalUnavailable, OSError):
        return False


def write_session(state_root: Path, session: RemoteUpdateSession) -> bool:
    """Replace the record atomically.  Returns whether it is now on disk."""

    payload = json.dumps(
        session.document(), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    if len(payload) > MAX_SESSION_BYTES:
        return False
    return changed_under_the_gate(
        state_root, lambda: _replace_record(session_path(state_root), payload)
    )


def _replace_record(destination: Path, payload: bytes) -> bool:
    directory = destination.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=".remote-update-session-", suffix=".tmp", dir=str(directory)
        )
    except OSError:
        return False
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False
    return True


def clear_session(state_root: Path) -> None:
    """Forget a finished attempt.  An absent record is already the answer."""

    try:
        session_path(state_root).unlink()
    except OSError:
        pass


def retire_session(
    state_root: Path, *, endpoint_fingerprint: str, update_attempt_id: str
) -> bool:
    """Remove the record only while it is still exactly this attempt's.

    A terminal success retires the attempt it finished, and nothing else.  The
    record on disk is read first and has to still name this same attempt id --
    minted fresh per attempt -- against this same endpoint; a newer attempt's
    record, a foreign one, or one that could not be read is left exactly as it
    is.  That is what keeps a callback held by an already-superseded flow from
    deleting the record its successor is depending on.

    Returns whether the finished attempt is no longer recorded here.
    """

    if not update_attempt_id or not endpoint_fingerprint:
        return False
    return changed_under_the_gate(
        state_root,
        lambda: _retire_admitted(state_root, endpoint_fingerprint, update_attempt_id),
    )


def _retire_admitted(
    state_root: Path, endpoint_fingerprint: str, update_attempt_id: str
) -> bool:
    """Admit the record, prove it is still the same file, and unlink it."""

    read = load_session(state_root)
    if read.state == STATE_ABSENT:
        return True
    recorded = read.session
    if recorded is None:
        return False
    if recorded.update_attempt_id != update_attempt_id:
        return False
    if recorded.endpoint_fingerprint != endpoint_fingerprint:
        return False
    # The check above was about the file that read came out of.  No cooperating
    # writer can be inside the gate, but a foreign one is not obliged to take
    # it, so prove the path still names the file just admitted rather than
    # unlinking whatever is there now.
    try:
        settled = os.lstat(session_path(state_root))
    except OSError:
        return False
    if read.identity is None or _identity(settled) != read.identity:
        return False
    clear_session(state_root)
    return load_session(state_root).state == STATE_ABSENT


__all__ = [
    "ENDPOINT_FIELDS",
    "ENDPOINT_SEPARATOR",
    "MAX_SESSION_BYTES",
    "PreparedCandidate",
    "RemoteEndpoint",
    "RemoteUpdateSession",
    "SESSION_FILE",
    "SESSION_SCHEMA_VERSION",
    "STATE_ABSENT",
    "STATE_AMBIGUOUS",
    "STATE_PRESENT",
    "SessionRead",
    "admit_session",
    "changed_under_the_gate",
    "clear_session",
    "endpoint_of",
    "load_session",
    "new_attempt_id",
    "read_session",
    "retire_session",
    "session_path",
    "write_session",
]

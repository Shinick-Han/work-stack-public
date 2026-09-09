"""The durable ``JournalPort`` behind remote update restart recovery.

The flow in ``remote_update_flow`` records at most two identities: one semantic
mutation in flight whose disposition is unknown, and one passive activation
anchor -- a receipt the registry has already written, kept because a restart
has to find the activation this desktop owns the recovery of.  This module is
where those two records survive the process.

Failing closed is the whole point.  ``RemoteUpdateFlow.__init__`` calls
``load()`` and adopts whatever comes back; an empty tuple means "nothing was in
flight", so a flow built on an empty tuple starts idle and is free to issue a
fresh mutation.  Returning empty because the file could not be parsed, could
not be read, or belongs to a different workspace would therefore turn a lost
restore into a second restore.  Every such condition raises instead, and the
host decides whether to reload or to refuse the update.

``load()`` therefore never substitutes an answer: it returns the records the
file itself carries, or it raises.  Exactly two files carry no records.  One is
the absent file -- nothing was ever written beside this state root.  The other
is a *settled* record of another attempt: a document admitted whole, agreeing
with its own binding digest, whose ``records`` are empty.  Such a record names
no mutation in flight and no activation anchor, so there is nothing for this
attempt to adopt and nothing the previous attempt can lose -- it is superseded,
never adopted, and it is replaced only by this instance's next ``record()``,
under the same lock and the same sequence-and-writer gate every write passes.
A record of another attempt that still carries either identity is refused
exactly as before.  That pair is what keeps one completed update from ending
remote update for this desktop forever, without letting a fresh attempt walk
over an unreconciled one.

Reading never writes.  ``load()`` does not repair a malformed document, does
not rewrite an old one into the current shape, does not truncate and does not
delete.  A file that cannot be understood is left exactly as it is, because it
is the only remaining evidence of what the previous process was doing.

Writing follows the local desktop convention already used for the connection
registry: an exclusively created temporary file in the destination directory,
written, flushed, fsynced, and only then moved onto the destination by an
atomic replace, with the temp file this call created -- and no other -- removed
afterwards.  The destination is never opened for writing, so a failure at any
point leaves the previous record intact rather than half of a new one.

Two desktops must not take turns clobbering one record, and remembering a
sequence is not enough on its own: two instances that observed the same
sequence can both pass such a check and both replace the file, each believing
it committed.  So the whole decision -- read the file, admit this instance's
observation as still current, stage the temp file, replace -- happens inside
one exclusive lock on a persistent lock file beside the journal, taken with the
same ``msvcrt.LK_NBLCK`` / ``fcntl.flock`` convention
``connection_registry_mutation_lock`` already uses for registry writers.  It is
this journal's own lock file: not the registry's, not the SSOT's and not an
activation receipt's.

Inside that lock an instance is admitted only when the file still carries
exactly the sequence *and* the writer identity it last established, so the
instance that loses a race is refused and cannot write again until an admitted
``load()`` gives it a current observation.  Contention is bounded: the
non-blocking attempt is retried under an explicit deadline and then becomes a
plain refusal, before anything is staged or replaced, and the lock is released
on every outcome.  A write whose outcome is not established leaves the instance
refusing further writes rather than claiming a success it cannot demonstrate.

This file lives beside the desktop state root.  It is not the connection
registry, not the SSOT and not an activation receipt, and it never reads or
writes any of those.  Its body carries opaque identifiers and one binding
digest: no secrets, no tokens and no filesystem paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from remote_update_flow_contract import (
    ACTIVATION_ANCHOR_KINDS,
    MUTATIONS,
    OBSERVATIONS,
    PendingOperation,
)

JOURNAL_FILE = "remote-update-journal.json"

#: This journal's own lock file, created once and then kept forever.  Its
#: identity is what makes the lock shared, so it is never removed: unlinking it
#: would let the next writer lock a different file and pass a held gate.
JOURNAL_LOCK_FILE = "remote-update-journal.lock"
JOURNAL_SCHEMA_VERSION = 1
MAX_JOURNAL_BYTES = 8192
MAX_RECORDS = 2

#: How long a writer waits for another writer before refusing.  Bounded on
#: purpose: a busy peer is worth a short wait, a stuck or crashed one must
#: never hold this call forever.
LOCK_CONTENTION_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.01

#: Opaque identifiers only.  The charset admits no path separator, no
#: whitespace and no wildcard, so a binding value can never name a location.
_OPAQUE_ID = re.compile(r"\A[A-Za-z0-9._:@+-]{1,128}\Z")

#: Values that would bind this journal to "whatever is selected".  A recovery
#: record answers "which activation is this", so a binding that matches
#: anything answers nothing and is refused at construction.
_WILDCARD_VALUES = frozenset({
    "*", "?", "-", "any", "all", "none", "null", "default", "unknown", "wildcard",
})

_DOCUMENT_KEYS = frozenset({
    "schema_version", "binding", "binding_digest", "sequence", "writer", "records",
})
_BINDING_KEYS = ("workspace_id", "profile_id", "update_attempt_id")
_RECORD_REQUIRED = frozenset({"stage", "operation_id", "kind"})
_RECORD_KEYS = _RECORD_REQUIRED | {"previous_app_retained"}


class JournalUnavailable(Exception):
    """The durable record could not be established, so recovery may not run.

    Every failure in this module is one of these.  None of them means "there
    was nothing in flight": a caller that treats one as an empty journal has
    turned an unknown mutation into a fresh one.
    """


class JournalBindingRefused(JournalUnavailable):
    """The host did not supply an exact workspace/profile/attempt binding."""


class JournalUnreadable(JournalUnavailable):
    """The file exists but is not an admissible record of this journal."""


class JournalBindingMismatch(JournalUnavailable):
    """The record on disk belongs to a different binding than this one."""


class JournalWriteRefused(JournalUnavailable):
    """Another writer advanced the record; nothing here was written."""


class JournalWriteUncertain(JournalUnavailable):
    """A write's outcome is not established, so no success is claimed."""


@dataclass(frozen=True)
class JournalBinding:
    """The one update attempt a journal file is allowed to speak for.

    All three values are admitted by the host explicitly.  The profile matters
    on its own: after the same activation the host may select a different
    profile, and that selection must carry its own admitted binding rather than
    inherit the previous one, so a record written under one profile is never
    handed to a flow running under another.
    """

    workspace_id: str
    profile_id: str
    update_attempt_id: str

    def __post_init__(self) -> None:
        for name in _BINDING_KEYS:
            _admit_binding_value(name, getattr(self, name))

    @property
    def digest(self) -> str:
        """A stable digest over the exact triple, in a fixed order.

        The three identifiers are already opaque and are stored as they are;
        the digest binds them together, so editing one value in the file
        without the others is a mismatch rather than a silent rebinding.
        """

        joined = "\x1f".join(getattr(self, name) for name in _BINDING_KEYS)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def document(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _BINDING_KEYS}


def _admit_binding_value(name: str, value: object) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.match(value):
        raise JournalBindingRefused(f"{name} must be one opaque identifier")
    if value.strip().lower() in _WILDCARD_VALUES:
        raise JournalBindingRefused(f"{name} must name one attempt, not any")
    return value


def journal_path(state_root: Path) -> Path:
    """Where the record lives: beside the desktop state, in its own file."""

    return Path(state_root) / JOURNAL_FILE


class RemoteUpdateJournal:
    """A ``JournalPort`` whose two records outlive the process.

    One instance speaks for one binding.  ``load()`` is read-only, and
    ``record()`` performs its read, its admission and its replace inside the
    shared journal lock, so a process that has fallen behind -- or that lost a
    simultaneous race -- refuses rather than overwrites.

    Lock order is fixed and the same everywhere: this object's own lock first,
    the shared file lock second.  Nothing in this module ever takes them the
    other way round.
    """

    def __init__(self, state_root: Path, binding: JournalBinding) -> None:
        if not isinstance(binding, JournalBinding):
            raise JournalBindingRefused("an explicit admitted binding is required")
        self._state_root = Path(state_root)
        self._path = journal_path(state_root)
        self._binding = binding
        self._lock = threading.Lock()
        self._writer = secrets.token_hex(8)
        #: The exact ``(sequence, writer)`` this instance established, or
        #: ``None`` for "there was no file".  The writer is part of it because
        #: a sequence number alone can be produced by two instances at once.
        self._observed: tuple[int, str] | None = None
        #: The settled binding of another attempt that the last read
        #: admitted, or ``None``.  It records what was found, never a
        #: permission: no branch in this module consults it.
        self._superseded: JournalBinding | None = None
        self._uncertain = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def binding(self) -> JournalBinding:
        return self._binding

    @property
    def superseded(self) -> JournalBinding | None:
        """The settled binding of another attempt that the last read admitted.

        ``None`` in the ordinary cases: no file at all, or a record this
        instance's own binding already speaks for.  It is evidence a host may
        present, never permission: nothing in this module is admitted because
        of it.
        """

        return self._superseded

    # -- reading ---------------------------------------------------------

    def load(self) -> tuple[PendingOperation, ...]:
        """The recorded identities, or ``()`` only when there is no file.

        Read-only in every branch: nothing here creates, repairs, rewrites or
        removes anything on disk.
        """

        with self._lock:
            document = self._read_document()
            if document is None:
                self._observed = None
                return ()
            records = _admit_records(document["records"])
            self._observed = (int(document["sequence"]), str(document["writer"]))
            return records

    def _read_document(self) -> dict[str, object] | None:
        """The admitted document, or ``None`` for a genuinely absent file."""

        self._superseded = None
        try:
            raw_bytes = self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            # Present but unreadable is not an absent journal: a permission or
            # device error says nothing about what was in flight.
            raise JournalUnreadable(f"the update journal could not be read: {error}") from error
        if len(raw_bytes) > MAX_JOURNAL_BYTES:
            raise JournalUnreadable("the update journal is larger than its bound")
        try:
            raw = json.loads(raw_bytes.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as error:
            # A truncated or corrupt file is the strongest possible signal that
            # a mutation was in flight.  It is never a fresh start.
            raise JournalUnreadable("the update journal is not valid JSON") from error
        return self._admit_document(raw)

    def _admit_document(self, raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise JournalUnreadable("the update journal is not an object")
        keys = set(raw)
        if keys != _DOCUMENT_KEYS:
            raise JournalUnreadable("the update journal has unknown or missing fields")
        if raw["schema_version"] != JOURNAL_SCHEMA_VERSION or isinstance(
            raw["schema_version"], bool
        ):
            raise JournalUnreadable(
                f"the update journal schema must be exactly {JOURNAL_SCHEMA_VERSION}"
            )
        sequence = raw["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise JournalUnreadable("the update journal sequence is not a counter")
        if not isinstance(raw["writer"], str) or not _OPAQUE_ID.match(raw["writer"]):
            raise JournalUnreadable("the update journal writer is not an identifier")
        if not isinstance(raw["records"], list):
            raise JournalUnreadable("the update journal records are not a list")
        self._admit_binding(raw)
        return raw

    def _admit_binding(self, raw: dict[str, object]) -> None:
        stored = raw["binding"]
        if not isinstance(stored, dict) or set(stored) != set(_BINDING_KEYS):
            raise JournalUnreadable("the update journal binding is not the closed shape")
        recorded = JournalBinding(
            workspace_id=_admit_binding_value("workspace_id", stored["workspace_id"]),
            profile_id=_admit_binding_value("profile_id", stored["profile_id"]),
            update_attempt_id=_admit_binding_value(
                "update_attempt_id", stored["update_attempt_id"]
            ),
        )
        if raw["binding_digest"] != recorded.digest:
            # The document does not agree with itself, so it names no attempt
            # at all -- neither this one, nor one whose records this journal
            # could prove settled.  Ambiguous evidence is refused, not retired.
            raise JournalBindingMismatch(
                "the update journal on record belongs to another update attempt"
            )
        if recorded == self._binding:
            return
        self._admit_superseded(raw, recorded)

    def _admit_superseded(self, raw: dict[str, object], recorded: JournalBinding) -> None:
        """Another attempt's record: settled and superseded, or refused.

        Refusing is still the whole guarantee for anything in flight.  A record
        for another workspace, profile or attempt that names a mutation or an
        activation anchor must never be adopted as this flow's own, and must
        never be reported as "nothing in flight" either.

        Empty ``records`` is the one record that says something different, and
        it is not an exception to that guarantee so much as its floor: the
        previous attempt has no mutation to reconcile and no activation to
        recover, which is exactly the state its own next process would find.
        Refusing teaches this attempt nothing and proceeding takes nothing
        away, so the settled record is superseded rather than adopted -- the
        answer returned is the file's own ``()``, not one substituted for it.

        Nothing is written here.  The read stays a read, the settled document
        is left byte for byte where it is, and it is replaced only if this
        instance later writes, inside the shared lock and only while the file
        still carries exactly the sequence and writer this read observed.
        """

        if raw["records"]:
            raise JournalBindingMismatch(
                "the update journal on record belongs to another update attempt"
            )
        self._superseded = recorded

    # -- writing ---------------------------------------------------------

    def record(self, entries: tuple[PendingOperation, ...]) -> None:
        """Replace the recorded identities, or leave the record untouched."""

        admitted = _admit_records(entries)
        with self._lock:
            if self._uncertain:
                raise JournalWriteUncertain(
                    "the previous update journal write is unresolved; reload before writing"
                )
            # Read, admission and replace are one critical section shared by
            # every instance and every process: two writers can no longer both
            # pass the gate and then both replace the file.
            with remote_update_journal_lock(self._state_root):
                self._record_locked(admitted)

    def _record_locked(self, admitted: tuple[PendingOperation, ...]) -> None:
        current = self._read_document()
        self._guard_observation(current)
        sequence = 1 if current is None else int(current["sequence"]) + 1
        self._replace(_document(self._binding, sequence, self._writer, admitted))
        self._observed = (sequence, self._writer)

    def _guard_observation(self, current: dict[str, object] | None) -> None:
        """Refuse unless the file is still exactly where this writer left it.

        The observation is revalidated here, inside the shared lock and
        immediately before the replace, rather than trusted from construction
        time -- the same discipline the attempt-resource gate uses so a stale
        holder can never overwrite a newer generation.  A writer that never
        observed an existing record is stale by definition: it would be
        clobbering a record it has not read.

        Writer identity is compared alongside the sequence.  Serialisation
        already stops two instances from producing the same sequence, and this
        closes the remaining ABA: a record that happens to carry the number
        this instance remembers, but was written by somebody else, is a
        different record and does not admit this writer.
        """

        on_disk = (
            None
            if current is None
            else (int(current["sequence"]), str(current["writer"]))
        )
        if on_disk == self._observed:
            return
        raise JournalWriteRefused(
            "another update journal writer advanced the record; nothing was written"
        )

    def _replace(self, document: dict[str, object]) -> None:
        payload = json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n"
        if len(payload.encode("utf-8")) > MAX_JOURNAL_BYTES:
            raise JournalWriteRefused("the update journal record is larger than its bound")
        self._state_root.mkdir(parents=True, exist_ok=True)
        owned: Path | None = None
        try:
            owned = self._write_temp(payload)
            self._commit(owned)
        finally:
            # Only the temporary file this call created, never a sweep and
            # never a path it merely tried: another instance's temp file is
            # that instance's to remove.
            if owned is not None:
                _unlink_own_temp(owned)

    def _write_temp(self, payload: str) -> Path:
        temporary = self._path.with_name(
            f".{self._path.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
        )
        try:
            stream = temporary.open("x", encoding="utf-8", newline="\n")
        except OSError as error:
            # Nothing was created, so there is nothing of this call's to clean
            # up and the destination stands exactly as it was.
            raise JournalWriteRefused(
                f"the update journal could not be staged: {error}"
            ) from error
        try:
            with stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            # The destination was never opened, so the previous record stands
            # exactly as it was and the failure is a plain refusal.
            _unlink_own_temp(temporary)
            raise JournalWriteRefused(
                f"the update journal could not be staged: {error}"
            ) from error
        return temporary

    def _commit(self, temporary: Path) -> None:
        try:
            os.replace(temporary, self._path)
        except OSError as error:
            # A failed replace leaves either the old record or the new one and
            # this process cannot tell which.  No success is claimed, the old
            # record is not erased, and this instance writes nothing further
            # until a host reloads it or refuses the update.
            self._uncertain = True
            raise JournalWriteUncertain(
                f"the update journal write outcome is unknown: {error}"
            ) from error
        _fsync_directory_best_effort(self._state_root)


def journal_lock_path(state_root: Path) -> Path:
    """The persistent lock file every journal writer takes, and nobody removes."""

    return Path(state_root) / JOURNAL_LOCK_FILE


@contextmanager
def remote_update_journal_lock(state_root: Path):
    """Hold the one cross-instance, cross-process gate on this journal.

    Modelled on ``connection_registry_mutation_lock``: an exclusive lock on one
    byte of a persistent lock file, taken with ``msvcrt.LK_NBLCK`` on Windows
    and ``fcntl.flock`` elsewhere.  ``flock`` and not ``lockf``, deliberately --
    ``lockf`` locks are owned by the process, so two journal instances inside
    one desktop would both be admitted through it.

    The lock file is this journal's own and is never unlinked: a lock proven by
    a file that a writer may delete is not a lock at all.
    """

    state_root = Path(state_root)
    try:
        state_root.mkdir(parents=True, exist_ok=True)
        stream = journal_lock_path(state_root).open("a+b")
    except OSError as error:
        raise JournalWriteRefused(
            f"the update journal lock could not be opened: {error}"
        ) from error
    acquired = False
    try:
        _prime_lock_file(stream)
        acquired = _acquire_within_bound(stream)
        yield
    finally:
        _release_lock(stream, acquired)


def _prime_lock_file(stream) -> None:
    """Give a newly created lock file the one zero byte the lock is taken on.

    Two processes may both find it empty and both append; the lock is still
    taken on byte zero, so the outcome of that race changes nothing.
    """

    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(bytes(1))
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
    except OSError as error:
        raise JournalWriteRefused(
            f"the update journal lock could not be prepared: {error}"
        ) from error


def _acquire_within_bound(stream) -> bool:
    """Take the lock, or turn contention into a refusal at the deadline.

    Every attempt is non-blocking, so no call can be parked indefinitely behind
    a peer that crashed holding the lock.  Exhausting the bound raises before
    the caller has staged or replaced anything, which makes contention a
    refusal with no journal effect rather than a hang.
    """

    deadline = time.monotonic() + LOCK_CONTENTION_SECONDS
    while not _try_lock(stream):
        if time.monotonic() >= deadline:
            raise JournalWriteRefused(
                "another update journal writer holds the record; nothing was written"
            )
        time.sleep(_LOCK_POLL_SECONDS)
    return True


def _try_lock(stream) -> bool:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release_lock(stream, acquired: bool) -> None:
    """Release on every outcome, including the ones that raised."""

    try:
        if acquired:
            stream.seek(0)
            _unlock(stream)
    except OSError:
        # Closing the handle drops the lock anyway, and a failure to release
        # must not replace the outcome the caller has to act on.
        pass
    finally:
        stream.close()


def _unlock(stream) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _document(
    binding: JournalBinding,
    sequence: int,
    writer: str,
    records: tuple[PendingOperation, ...],
) -> dict[str, object]:
    return {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "binding": binding.document(),
        "binding_digest": binding.digest,
        "sequence": sequence,
        "writer": writer,
        "records": [
            {
                "stage": record.stage,
                "operation_id": record.operation_id,
                "kind": record.kind,
                "previous_app_retained": record.previous_app_retained,
            }
            for record in records
        ],
    }


def _admit_records(entries: object) -> tuple[PendingOperation, ...]:
    """The two-slot shape, or a refusal.  Used on both sides of the file.

    A document and an in-memory tuple are held to the same rules, so a record
    this journal would refuse to load is one it also refuses to write.
    """

    if not isinstance(entries, (tuple, list)):
        raise JournalUnreadable("the update journal records are not a sequence")
    if len(entries) > MAX_RECORDS:
        raise JournalUnreadable("the update journal holds at most two identities")
    records = tuple(_admit_record(entry) for entry in entries)
    _admit_slots(records)
    return records


def _admit_record(entry: object) -> PendingOperation:
    if isinstance(entry, PendingOperation):
        fields = {
            "stage": entry.stage,
            "operation_id": entry.operation_id,
            "kind": entry.kind,
            "previous_app_retained": entry.previous_app_retained,
        }
    elif isinstance(entry, dict):
        fields = _admit_record_fields(entry)
    else:
        raise JournalUnreadable("an update journal record is not a recorded identity")
    return _admit_identity(fields)


def _admit_record_fields(entry: dict[object, object]) -> dict[str, object]:
    keys = set(entry)
    if not _RECORD_REQUIRED <= keys or not keys <= _RECORD_KEYS:
        raise JournalUnreadable("an update journal record has unknown or missing fields")
    # A record written before ``previous_app_retained`` existed carries no
    # answer, and the flow's contract is that no answer reads as ``unknown``:
    # the conservative end, offering pairing resolution rather than a rollback.
    return {
        "stage": entry["stage"],
        "operation_id": entry["operation_id"],
        "kind": entry["kind"],
        "previous_app_retained": entry.get("previous_app_retained", "unknown"),
    }


def _admit_identity(fields: dict[str, object]) -> PendingOperation:
    kind = fields["kind"]
    if not isinstance(kind, str) or kind not in MUTATIONS:
        raise JournalUnreadable("an update journal record names an unknown operation")
    if fields["stage"] != MUTATIONS[kind].stage.value:
        raise JournalUnreadable(f"an update journal record pairs {kind} with a foreign stage")
    operation_id = fields["operation_id"]
    if not isinstance(operation_id, str) or not _OPAQUE_ID.match(operation_id):
        raise JournalUnreadable("an update journal record has no opaque operation identity")
    retained = fields["previous_app_retained"]
    if retained not in OBSERVATIONS:
        raise JournalUnreadable("an update journal record has a foreign retention answer")
    return PendingOperation(str(fields["stage"]), operation_id, kind, str(retained))


def _admit_slots(records: tuple[PendingOperation, ...]) -> None:
    """One in-flight mutation and one activation anchor, at most, once each.

    A rollback legitimately carries the retained receipt's own operation id, so
    the two slots may share an identifier; what may not repeat is the same
    identity under the same kind, or either slot itself.
    """

    anchors = [record for record in records if record.kind in ACTIVATION_ANCHOR_KINDS]
    inflight = [record for record in records if record.kind not in ACTIVATION_ANCHOR_KINDS]
    if len(anchors) > 1:
        raise JournalUnreadable("the update journal holds more than one activation anchor")
    if len(inflight) > 1:
        raise JournalUnreadable("the update journal holds more than one mutation in flight")
    identities = {(record.kind, record.operation_id) for record in records}
    if len(identities) != len(records):
        raise JournalUnreadable("the update journal repeats one recorded identity")


def _unlink_own_temp(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        # The replace already decided the record; a leftover inert temp file
        # must not replace the outcome the caller has to act on.
        return


def _fsync_directory_best_effort(directory: Path) -> None:
    """Persist the replace metadata where directory fsync is supported.

    Windows does not expose portable directory handles through ``os.open``;
    successful file fsync plus atomic replace remains the supported guarantee.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)

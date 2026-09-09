"""The gate that makes the host's recovery evidence a precondition, not a note.

Two facts an activated update cannot be recovered without -- which operation
staged the candidate, and which verified archive a restore would put back --
are produced by the remote and then kept by the *desktop*, on its own disk.
Those are two different observations, and conflating them in either direction
is a defect:

* Reporting a verified remote staging as failed because a local write did not
  land would state something untrue about the remote.
* Letting the flow stop the owner, take the backup, probe and move the registry
  while that write is still outstanding leaves a product that has already
  changed authority and can no longer recover the proof it depends on.

So nothing here ever restates a remote outcome.  What it does instead is stand
in front of the *next* mutating boundary until the write lands, and retry that
write under the identity it already has -- never a second operation for the
reconciler to disagree about.

Nothing in this module reads the desktop's state root, opens a transport or
knows what a session record looks like; the writing itself belongs to the host,
and the record it writes belongs to ``remote_update_host_surface_record``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
_ROOT = str(Path(_SHELL_DIR).parents[1])
for _path in (_SHELL_DIR, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from remote_update_backup_port import (  # noqa: E402
    RemoteMaintenanceBackupPort,
    RestoreSource,
)
from remote_update_flow_contract import PortRefusal  # noqa: E402


#: Recovery evidence this attempt already produced is not on disk yet, so the
#: next mutation may not be issued.  Not a statement about any remote outcome.
REFUSED_EVIDENCE_NOT_DURABLE = "host_recovery_evidence_not_durable"

#: The two kinds of recovery evidence the host has to make durable.
EVIDENCE_PREPARED = "prepared"
EVIDENCE_RESTORE_SOURCE = "restore_source"


class DurableEvidenceGate:
    """Recovery evidence that has to be on disk before the next mutation.

    The two facts above are produced by the remote and then kept *here*, and
    those are two different observations.  This keeps them from being conflated
    in either direction.  It never restates a remote outcome: a staging that
    verified is reported verified, a backup that verified is reported verified,
    because that is what happened out there.  What it does instead is stand in
    front of the next mutating boundary -- while a required write is
    outstanding, the stop, the backup, the probe and the activation refuse
    before issuing anything, so the product never moves the registry and
    restarts into a state whose recovery evidence it has already failed to keep.

    A failed write is retried under the *same* identity: the pending entry holds
    the very value that failed, so a retry that lands records the operation that
    actually ran instead of minting a second one to disagree about.
    """

    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, Callable[[], bool]]] = {}
        self._failures = 0

    @property
    def pending(self) -> tuple[str, ...]:
        """Which kinds of evidence are still not durable, in a stable order."""

        return tuple(sorted(self._pending))

    @property
    def satisfied(self) -> bool:
        return not self._pending

    @property
    def failures(self) -> int:
        """How many write attempts this gate has seen fail, for the trace."""

        return self._failures

    def record(self, kind: str, identity: str, write: Callable[[], bool]) -> bool:
        """Try the write now; keep it, under its own identity, if it does not land."""

        if self._attempt(write):
            self._pending.pop(kind, None)
            return True
        self._pending[kind] = (identity, write)
        return False

    def settle(self) -> bool:
        """Retry every outstanding write, unchanged, and say whether all landed."""

        for kind, (_identity, write) in tuple(self._pending.items()):
            if self._attempt(write):
                self._pending.pop(kind, None)
        return self.satisfied

    def require(self) -> None:
        """Refuse the caller's mutation while any required write is outstanding."""

        if not self.settle():
            raise PortRefusal(REFUSED_EVIDENCE_NOT_DURABLE)

    def _attempt(self, write: Callable[[], bool]) -> bool:
        try:
            landed = bool(write())
        except Exception:  # noqa: BLE001 - the host's own write, whatever it raises
            landed = False
        if not landed:
            self._failures += 1
        return landed


class GatedPort:
    """A real port with the recovery-evidence gate in front of named calls.

    Every other call -- the observations, the reconciles, and the two calls a
    restore is made of -- passes straight through to the real object.  Gating
    those would be the opposite of the point: an attempt whose evidence could
    not be written is exactly the one that may still need observing, reconciling
    and rolling back.
    """

    def __init__(
        self, port: object, gate: DurableEvidenceGate, gated: frozenset[str]
    ) -> None:
        self._port = port
        self._gate = gate
        self._gated = gated

    @property
    def port(self) -> object:
        """The production adapter itself, unchanged and unwrapped."""

        return self._port

    def __getattr__(self, name: str):
        attribute = getattr(self._port, name)
        if name not in self._gated or not callable(attribute):
            return attribute

        def gated(*args: object, **kwargs: object):
            self._gate.require()
            return attribute(*args, **kwargs)

        return gated

#: The owner stop is a mutation; observing what it did is not.
GATED_OWNER = frozenset({"stop"})
#: Issuing the registry move and confirming it are mutations; observing them,
#: and rolling them back, are how a stranded attempt is recovered.
GATED_ACTIVATION = frozenset({"activate", "confirm"})


def guarded_port(
    port: object, gate: DurableEvidenceGate, gated: frozenset[str], guard: bool
) -> object:
    """The real port, wrapped only when there is durable evidence to wait on."""

    return GatedPort(port, gate, gated) if guard else port


class RestoreSourceRecorder:
    """The real backup port, plus the durable write its own contract needs.

    ``RestoreSource`` names the verified archive a restore would put back.  The
    port learns it during the verified backup, the backup identity is settled
    and gone by activation time, and a restart has to be handed it back through
    the constructor -- so it has to reach the host's durable record at the
    moment it becomes known, not at whatever later paint happens to ask.

    Every call here is the real port's call and its real answer.  Nothing is
    doubled, nothing is decided, and a recording callback that fails cannot
    turn a verified backup into a failed stage; the host reports its own write.
    """

    def __init__(
        self,
        port: RemoteMaintenanceBackupPort,
        record: Callable[[RestoreSource], bool],
        gate: DurableEvidenceGate | None = None,
    ) -> None:
        self._port = port
        self._record = record
        self._gate = gate
        self._seen = port.restore_source

    @property
    def restore_source(self) -> RestoreSource | None:
        return self._port.restore_source

    def create_verified(self, operation_id: str):
        # Not over evidence this attempt has already failed to keep.
        if self._gate is not None:
            self._gate.require()
        return self._recorded(self._port.create_verified, operation_id)

    def observe(self, operation_id: str):
        return self._recorded(self._port.observe, operation_id)

    def restore(self, operation_id: str):
        return self._recorded(self._port.restore, operation_id)

    def observe_restore(self, operation_id: str):
        return self._recorded(self._port.observe_restore, operation_id)

    def _recorded(self, call: Callable[[str], object], operation_id: str):
        try:
            return call(operation_id)
        finally:
            self._settle()

    def _settle(self) -> None:
        """Record a newly learned archive, and only then call it seen.

        Advancing ``_seen`` first would make this the only moment the archive
        could ever be recorded: every later observation of the same backup
        compares equal and skips the write, and the attempt activates with
        nothing to restore from.  So the identity advances behind the write, and
        until it lands the gate holds that same value, under that same backup
        operation id, in front of the next mutation.
        """

        current = self._port.restore_source
        if current is None or current == self._seen:
            return

        def write() -> bool:
            return bool(self._record(current))

        if self._gate is None:
            self._seen = current if write() else self._seen
        elif self._gate.record(EVIDENCE_RESTORE_SOURCE, current.operation_id, write):
            self._seen = current


__all__ = [
    "DurableEvidenceGate",
    "EVIDENCE_PREPARED",
    "EVIDENCE_RESTORE_SOURCE",
    "GATED_ACTIVATION",
    "GATED_OWNER",
    "GatedPort",
    "REFUSED_EVIDENCE_NOT_DURABLE",
    "RestoreSourceRecorder",
    "guarded_port",
]

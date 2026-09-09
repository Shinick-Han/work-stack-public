"""Production Preview/Prepare/Probe/Verify ports for remote application install.

Composes existing ``inspect_remote_target`` / ``apply_remote_install``,
verified-unpack place/inspect/verify over bounded SSH stdin, and
``observe_served_ui``. Root supplies the selected profile, absent target,
admitted artifact, workspace, explicit install mode, and a loopback/session
callback. This module never downloads artifacts, writes SSOT, activates a
connection, or silently downgrades transactional install to verified unpack.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
_ROOT = str(Path(_SHELL_DIR).parents[1])
for _path in (_SHELL_DIR, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from remote_command_contract import (  # noqa: E402
    RemoteCommandError,
    overlapping_posix_roots,
    require_posix_owner,
    require_workspace_uid,
    validated_posix_path,
)
from remote_provision_artifact import ArtifactSelection  # noqa: E402
from remote_provision_driver import (  # noqa: E402
    INSTALL_TIMEOUT_SECONDS,
    PROBE_TIMEOUT_SECONDS,
    DriverError,
    DriverInspection,
    DriverProfile,
    apply_remote_install,
    inspect_remote_target,
)
from remote_provision_probe import ProbeError, run_remote_provision_probe  # noqa: E402
from remote_ui_observation import UNKNOWN as UI_UNKNOWN  # noqa: E402
from remote_verified_unpack import PLACEMENT_IDENTITY  # noqa: E402
from remote_ui_observation import observe_served_ui  # noqa: E402
from remote_update_flow_contract import (  # noqa: E402
    LostResponse,
    PortRefusal,
    PrepareOutcome,
    PreviewFacts,
    ProbeOutcome,
    VerifyOutcome,
)
from remote_update_install_ports_inspection import (  # noqa: E402
    INSTALL_MODES,
    LoopbackSession,
    MODE_TRANSACTIONAL,
    MODE_VERIFIED_UNPACK,
    OperationRecord,
    RetainedPreparation,
    data_workspace,
    install_digest,
    install_exists,
    install_product,
    install_protocol,
    mapping_of,
    observe_prepare_from_facts,
    observe_prepare_from_unpack,
    preparation_verified,
    preview_facts,
    probe_from_facts,
    probe_from_unpack,
    project_offer,
    request_fingerprint,
    retained_matches_inputs,
    reverified_preparation,
    served_digest,
    target_profile,
    unpack_code,
    unpack_product,
    unpack_protocol,
    unpack_runtime_digest,
    verify_outcome,
    prepare_from_install,
    prepare_from_unpack,
)
from remote_update_install_ports_transport import (  # noqa: E402
    TransportError,
    UNPACK_PLACE,
    UNPACK_VERIFY,
    build_ssh_unpack_command,
    build_unpack_payload_source,
    run_unpack_exchange,
)
from workstack import __version__ as DESKTOP_VERSION  # noqa: E402


MAX_OPERATION_ID = 128
ObserveCurrent = Callable[[], LoopbackSession]


class InstallPortsConfigError(ValueError):
    """Constructor inputs are not a usable selected target and artifact."""


@dataclass(frozen=True)
class InstallPortInputs:
    """Typed immutable constructor inputs. Root owns selection and download UI."""

    current_profile: DriverProfile
    target_app_dir: str
    artifact: ArtifactSelection
    expected_workspace_id: str
    owner: str
    ssh_executable: str
    install_mode: str
    observe_current: ObserveCurrent
    capability_scratch_parent: str | None = None
    process_factory: Callable[..., object] = subprocess.Popen
    probe_timeout: float = PROBE_TIMEOUT_SECONDS
    install_timeout: float = INSTALL_TIMEOUT_SECONDS
    expected_served_ui_sha256: str | None = None


def admit_inputs(inputs: InstallPortInputs) -> InstallPortInputs:
    if not isinstance(inputs, InstallPortInputs):
        raise InstallPortsConfigError("InstallPortInputs is required")
    if not isinstance(inputs.current_profile, DriverProfile):
        raise InstallPortsConfigError("a DriverProfile is required")
    if not isinstance(inputs.artifact, ArtifactSelection):
        raise InstallPortsConfigError("an ArtifactSelection is required")
    if inputs.install_mode not in INSTALL_MODES:
        raise InstallPortsConfigError("install_mode must be transactional or verified_unpack")
    if not callable(inputs.observe_current):
        raise InstallPortsConfigError("observe_current must be callable")
    try:
        workspace = require_workspace_uid(inputs.expected_workspace_id)
        owner = require_posix_owner(inputs.owner)
        target = validated_posix_path(inputs.target_app_dir, "remote_app_dir")
        current_app = validated_posix_path(
            inputs.current_profile.remote_app_dir, "remote_app_dir"
        )
        data = validated_posix_path(inputs.current_profile.remote_data_dir, "remote_data_dir")
        profile_workspace = require_workspace_uid(inputs.current_profile.expected_workspace_id)
    except RemoteCommandError as error:
        raise InstallPortsConfigError(error.code) from None
    if workspace != profile_workspace:
        raise InstallPortsConfigError("expected workspace does not match the selected profile")
    if target == current_app or overlapping_posix_roots(target, current_app) or overlapping_posix_roots(target, data):
        raise InstallPortsConfigError("target must be an absent directory beside the current app")
    scratch = _admit_scratch(inputs.capability_scratch_parent, target, data)
    executable = inputs.ssh_executable
    if not isinstance(executable, str) or not executable:
        raise InstallPortsConfigError("ssh executable is required")
    served_expected = _admit_expected_served(inputs.expected_served_ui_sha256)
    return replace(
        inputs,
        target_app_dir=target,
        expected_workspace_id=workspace,
        owner=owner,
        capability_scratch_parent=scratch,
        expected_served_ui_sha256=served_expected,
    )


def _admit_expected_served(value: object) -> str | None:
    if value is None:
        return None
    admitted = served_digest(value)
    if admitted is None:
        raise InstallPortsConfigError(
            "expected_served_ui_sha256 must be sha256:<64 lowercase hex>"
        )
    return admitted


def _admit_scratch(value: object, target: str, data: str) -> str | None:
    if value is None:
        return None
    try:
        scratch = validated_posix_path(value, "capability_scratch_parent")
    except RemoteCommandError as error:
        raise InstallPortsConfigError(error.code) from None
    parent = target.rsplit("/", 1)[0]
    if scratch != parent or scratch == data or scratch.startswith(data + "/"):
        raise InstallPortsConfigError("scratch parent must be the selected application parent")
    return scratch


def admit_operation_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_OPERATION_ID:
        raise PortRefusal("operation_id_invalid")
    if any(ord(character) < 32 or character == "\x7f" for character in value):
        raise PortRefusal("operation_id_invalid")
    return value


class RemoteUpdateInstallPorts:
    """One object for D preview, prepare, probe, and verification ports."""

    def __init__(self, inputs: InstallPortInputs) -> None:
        self._inputs = admit_inputs(inputs)
        self._record: OperationRecord | None = None
        self._payload: bytes | None = None

    @property
    def inputs(self) -> InstallPortInputs:
        return self._inputs

    def candidate_profile(self) -> DriverProfile:
        return target_profile(self._inputs.current_profile, self._inputs.target_app_dir)

    def describe(self) -> PreviewFacts:
        target = self._inspect(self.candidate_profile())
        current = self._inspect(self._inputs.current_profile)
        facts = None if target is None else target.facts
        if self._inputs.capability_scratch_parent is not None:
            measured = self._scratch_facts()
            if measured is not None:
                facts = measured
        capability, method = project_offer(facts, self._inputs.install_mode)
        served = self._served_ui()
        remote = None if current is None else install_product(current.facts)
        protocol = None if current is None else install_protocol(current.facts)
        session = self._session()
        desktop = session.desktop_version if session.desktop_version else DESKTOP_VERSION
        return preview_facts(
            desktop_version=desktop,
            remote_version=remote,
            served_ui_version=served,
            protocol_version=protocol,
            capability=capability,
            method=method,
        )

    def prepare(self, operation_id: str) -> PrepareOutcome:
        if self._begin(operation_id) == "observe":
            return self.observe(operation_id)
        if self._inputs.install_mode == MODE_TRANSACTIONAL:
            return self._prepare_transactional(operation_id)
        return self._prepare_unpack(operation_id)

    def observe(self, operation_id: str) -> PrepareOutcome:
        record = self._require_record(operation_id)
        retained = self._retention()
        if record.method == MODE_VERIFIED_UNPACK:
            document = self._unpack_document(UNPACK_VERIFY)
            if document is None:
                raise LostResponse(operation_id)
            outcome = observe_prepare_from_unpack(
                document, self._inputs.artifact, retained[0], retained[1]
            )
            receipt = mapping_of(document)
        else:
            inspection = self._inspect(self.candidate_profile())
            if inspection is None:
                raise LostResponse(operation_id)
            outcome = observe_prepare_from_facts(
                inspection.facts,
                self._inputs.artifact,
                MODE_TRANSACTIONAL,
                retained[0],
                retained[1],
            )
            receipt = record.receipt
        self._record = replace(record, prepare=outcome, receipt=receipt)
        return outcome

    def probe(self, operation_id: str) -> ProbeOutcome:
        record = self._require_record(operation_id)
        if not preparation_verified(record):
            return ProbeOutcome(status="unknown", workspace_match="unknown")
        inspection = self._inspect(self.candidate_profile())
        if inspection is None:
            return ProbeOutcome(status="unknown", workspace_match="unknown")
        expected = self._inputs.expected_workspace_id
        artifact = self._inputs.artifact
        if record.method == MODE_VERIFIED_UNPACK:
            document = self._unpack_document(UNPACK_VERIFY)
            return probe_from_unpack(document, inspection.facts, artifact, expected)
        return probe_from_facts(inspection.facts, artifact, expected)

    def verify(self, operation_id: str) -> VerifyOutcome:
        record = self._require_record(operation_id)
        session = self._session()
        expected = self._inputs.expected_workspace_id
        desktop = session.desktop_version if session.desktop_version else DESKTOP_VERSION
        if not preparation_verified(record):
            return VerifyOutcome(status="unknown", desktop_version=desktop)
        inspection = self._inspect(self.candidate_profile())
        served = self._served_ui()
        observed_workspace = session.workspace_id
        runtime_digest, remote, protocol = self._runtime_identity(record, inspection)
        if inspection is not None and observed_workspace is None:
            observed_workspace = data_workspace(inspection.facts)
        return verify_outcome(
            runtime_digest=runtime_digest,
            artifact=self._inputs.artifact,
            workspace=observed_workspace if observed_workspace is not None else "",
            expected_workspace=expected,
            served_ui=served,
            desktop_version=desktop,
            remote_version=remote,
            protocol_version=protocol,
            expected_served_ui=self._inputs.expected_served_ui_sha256,
        )

    def admit_retained_preparation(self, retained: RetainedPreparation) -> bool:
        """Public read-only recovery: re-verify a staging this process did not run.

        A restarted desktop has no in-memory operation, and the read-only
        stages must not be admitted by a fabricated request record.  This is
        the seam that admits them instead, and it admits nothing on the
        retained evidence alone: the evidence only says *which* candidate to go
        and look at, and this re-runs the frozen verified-unpack ``verify``
        exchange -- the same read-only helper ``observe`` uses, which places
        nothing, replaces nothing and activates nothing -- against the
        canonical target.  Only an actual ``identity_verified`` receipt for the
        admitted archive and manifest, describing that exact directory, admits
        the retained identity.

        The record it installs is the real one: issued, because the previous
        process really did stage the candidate; carrying the receipt this call
        just read back; and carrying the ``verified`` preparation outcome that
        ``probe`` and ``verify`` keep requiring.  Nothing here relaxes that
        gate, and an operation already in flight is never replaced.
        """

        token = self._admit_retained(retained)
        if token is None:
            return False
        document = self._unpack_document(UNPACK_VERIFY)
        if document is None or not reverified_preparation(
            document, self._inputs.artifact, self._inputs.target_app_dir
        ):
            return False
        previous_app, previous_profile = self._retention()
        outcome = observe_prepare_from_unpack(
            document, self._inputs.artifact, previous_app, previous_profile
        )
        if outcome.status != "verified":
            return False
        self._record = OperationRecord(
            token,
            request_fingerprint(
                token,
                MODE_VERIFIED_UNPACK,
                self._inputs.target_app_dir,
                self._inputs.artifact,
                self._inputs.expected_workspace_id,
            ),
            True,
            PLACEMENT_IDENTITY,
            MODE_VERIFIED_UNPACK,
            mapping_of(document),
            outcome,
        )
        return True

    def _admit_retained(self, retained: RetainedPreparation) -> str | None:
        """The retained operation id, when the evidence is this port's own."""

        if self._record is not None:
            return None
        if self._inputs.install_mode != MODE_VERIFIED_UNPACK:
            return None
        if not retained_matches_inputs(
            retained,
            self._inputs.target_app_dir,
            self._inputs.expected_workspace_id,
            self._inputs.artifact,
        ):
            return None
        try:
            return admit_operation_id(retained.operation_id)
        except PortRefusal:
            return None

    def _runtime_identity(
        self,
        record: OperationRecord,
        inspection: DriverInspection | None,
    ) -> tuple[str | None, str | None, str | None]:
        if record.method == MODE_VERIFIED_UNPACK:
            document = self._unpack_document(UNPACK_VERIFY)
            return (
                unpack_runtime_digest(document),
                unpack_product(document),
                unpack_protocol(document),
            )
        if inspection is None:
            return None, None, None
        facts = inspection.facts
        return install_digest(facts), install_product(facts), install_protocol(facts)

    def _begin(self, operation_id: str) -> str:
        token = admit_operation_id(operation_id)
        digest = request_fingerprint(
            token,
            self._inputs.install_mode,
            self._inputs.target_app_dir,
            self._inputs.artifact,
            self._inputs.expected_workspace_id,
        )
        record = self._record
        if record is not None:
            if record.operation_id != token:
                if record.issued:
                    raise PortRefusal("operation_id_mismatch")
            elif record.fingerprint != digest:
                raise PortRefusal("prepare_fingerprint_mismatch")
            elif record.issued:
                return "observe"
        self._record = OperationRecord(
            token,
            digest,
            False,
            "absent",
            self._inputs.install_mode,
            None,
            None,
        )
        return "issue"

    def _require_record(self, operation_id: str) -> OperationRecord:
        token = admit_operation_id(operation_id)
        record = self._record
        if record is None or record.operation_id != token:
            raise PortRefusal("operation_id_mismatch")
        return record

    def _mark(self, **changes: object) -> OperationRecord:
        record = self._record
        if record is None:
            raise PortRefusal("operation_id_mismatch")
        updated = replace(record, **changes)
        self._record = updated
        return updated

    def _prepare_transactional(self, operation_id: str) -> PrepareOutcome:
        inspection = self._inspect(self.candidate_profile())
        if inspection is None:
            raise PortRefusal("DRIVER_TRANSPORT_FAILED")
        capability, method = project_offer(inspection.facts, MODE_TRANSACTIONAL)
        if capability == "unavailable":
            return self._capability_unavailable(method)
        return self._apply_install(operation_id, inspection, capability, method)

    def _apply_install(
        self,
        operation_id: str,
        inspection: DriverInspection,
        capability: str,
        method: str,
    ) -> PrepareOutcome:
        try:
            result = apply_remote_install(
                inspection,
                self._inputs.artifact,
                apply=True,
                ssh_executable=self._inputs.ssh_executable,
                timeout=self._inputs.install_timeout,
                probe_timeout=self._inputs.probe_timeout,
                process_factory=self._inputs.process_factory,
            )
        except DriverError as error:
            raise PortRefusal(error.code) from None
        retained = self._retention()
        kind, prepared, receipt = prepare_from_install(
            result, capability, method, retained[0], retained[1]
        )
        if kind == "lost":
            self._mark(issued=True, placement="unknown", prepare=prepared, receipt=receipt)
            raise LostResponse(operation_id)
        if kind == "refused":
            raise PortRefusal(result.code or "REMOTE_INSTALL_FAILED")
        self._mark(issued=True, placement="ready_candidate", prepare=prepared, receipt=receipt)
        return prepared

    def _prepare_unpack(self, operation_id: str) -> PrepareOutcome:
        inspection = self._inspect(self.candidate_profile())
        capability, method = project_offer(
            None if inspection is None else inspection.facts, MODE_VERIFIED_UNPACK
        )
        if capability == "unavailable":
            return self._capability_unavailable(method)
        return self._apply_unpack(operation_id)

    def _apply_unpack(self, operation_id: str) -> PrepareOutcome:
        exchange = self._unpack_exchange(UNPACK_PLACE)
        if exchange.kind == "refused":
            raise PortRefusal(exchange.code or "DRIVER_TRANSPORT_FAILED")
        if exchange.kind == "lost" or exchange.document is None:
            self._mark(issued=True, placement="unknown")
            raise LostResponse(operation_id)
        retained = self._retention()
        kind, prepared, receipt, placement = prepare_from_unpack(
            exchange.document, self._inputs.artifact, retained[0], retained[1]
        )
        if kind == "refused":
            raise PortRefusal(unpack_code(exchange.document))
        self._mark(issued=True, placement=placement, prepare=prepared, receipt=receipt)
        if kind == "lost":
            raise LostResponse(operation_id)
        return prepared

    def _capability_unavailable(self, method: str) -> PrepareOutcome:
        retained = self._retention()
        outcome = PrepareOutcome(
            status="failed",
            capability="unavailable",
            method=method,
            previous_app_retained=retained[0],
            previous_profile_retained=retained[1],
        )
        self._mark(prepare=outcome, issued=False)
        return outcome

    def _unpack_exchange(self, operation: str):
        profile = self.candidate_profile()
        try:
            command = build_ssh_unpack_command(
                profile,
                self._inputs.ssh_executable,
                operation,
                self._inputs.target_app_dir,
            )
            payload = self._unpack_payload()
        except (TransportError, RemoteCommandError) as error:
            code = getattr(error, "code", "REMOTE_PROTOCOL_INVALID")
            raise PortRefusal(code) from None
        return run_unpack_exchange(
            self._inputs.process_factory, command, payload, self._inputs.install_timeout
        )

    def _unpack_payload(self) -> bytes:
        if self._payload is None:
            try:
                self._payload = build_unpack_payload_source(self._inputs.artifact)
            except TransportError as error:
                raise PortRefusal(error.code) from None
        return self._payload

    def _unpack_document(self, operation: str):
        exchange = self._unpack_exchange(operation)
        if exchange.kind != "output":
            return None
        return exchange.document

    def _inspect(self, profile: DriverProfile) -> DriverInspection | None:
        try:
            return inspect_remote_target(
                profile,
                self._inputs.owner,
                self._inputs.artifact,
                ssh_executable=self._inputs.ssh_executable,
                timeout=self._inputs.probe_timeout,
                process_factory=self._inputs.process_factory,
            )
        except (DriverError, ProbeError, TypeError, ValueError):
            return None

    def _scratch_facts(self) -> dict[str, object] | None:
        parent = self._inputs.capability_scratch_parent
        if parent is None:
            return None
        try:
            return run_remote_provision_probe(
                self.candidate_profile(),
                self._inputs.owner,
                ssh_executable=self._inputs.ssh_executable,
                timeout=self._inputs.probe_timeout,
                process_factory=self._inputs.process_factory,
                capability_scratch_parent=parent,
            )
        except (ProbeError, DriverError, TypeError, ValueError):
            return None

    def _session(self) -> LoopbackSession:
        try:
            session = self._inputs.observe_current()
        except (TypeError, ValueError, OSError, RuntimeError):
            return LoopbackSession()
        if isinstance(session, LoopbackSession):
            return session
        return LoopbackSession()

    def _served_ui(self) -> str | None:
        session = self._session()
        if session.base_url is None:
            return None
        expected = session.workspace_id or self._inputs.expected_workspace_id
        try:
            measured = observe_served_ui(session.base_url, expected)
        except (TypeError, ValueError, OSError, RuntimeError):
            return None
        if measured == UI_UNKNOWN:
            return None
        return served_digest(measured)

    def _retention(self) -> tuple[bool | None, bool | None]:
        current = self._inspect(self._inputs.current_profile)
        if current is None:
            return None, True
        exists = install_exists(current.facts)
        if exists is True:
            return True, True
        if exists is False:
            return False, True
        return None, True


__all__ = [
    "InstallPortInputs",
    "InstallPortsConfigError",
    "LoopbackSession",
    "ObserveCurrent",
    "RemoteUpdateInstallPorts",
    "RetainedPreparation",
    "admit_inputs",
]

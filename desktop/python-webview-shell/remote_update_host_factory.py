"""Compose the one production ``RemoteUpdateFlow`` for a selected connection.

This is the factory the desktop host calls, and the only place the update
flow's seven ports are wired to the real adapters:

===============  ==================================================
``preview``      ``RemoteUpdateInstallPorts`` (provision + unpack)
``prepare``      the same object, through :class:`CodeOnlyStagingPorts`
``probe``        the same object
``verification`` the same object
``owner``        ``RemoteOwnerStopPort`` (authenticated ``stop-owned``)
``backup``       ``RemoteMaintenanceBackupPort`` (supported maintenance)
``activation``   ``RegistryFlowActivationPort`` (connection registry)
===============  ==================================================

Nothing here downloads, installs, kills, writes SSOT or opens a registry of its
own.  It admits typed inputs the host has already resolved, refuses when any of
them is missing or inconsistent, and otherwise returns a flow whose every stage
runs against production code.  A refusal is the whole point: while
``build_remote_update_flow`` cannot construct one of these, the host has no
remote update capability to offer, and it says so instead of mounting a page
over simulated ports.

The run order is the explicitly selected ``prepare_before_stop=True`` mode of
``UX-PREPARE-BEFORE-BACKUP-DECISION.md``: PREVIEW, code-only PREPARE, STOP,
BACKUP, PROBE, ACTIVATE, VERIFY.  Staging first is what puts a verified new
application tree on the remote before the owner observation and the maintenance
helper have to run *from* one, which is the 1.0.8 bootstrap dependency; it
starts no server and touches no store, and the safety gate before the first
operation that could run new code against the store -- PROBE and everything
after it -- still requires the verified stop and the verified backup.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
_ROOT = str(Path(_SHELL_DIR).parents[1])
for _path in (_SHELL_DIR, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from connection_registry import SshConnectionProfile  # noqa: E402
from connection_registry_mutations import (  # noqa: E402
    ConnectionRegistryMutationService,
)
from remote_command_contract import (  # noqa: E402
    RemoteCommandError,
    require_posix_owner,
    require_workspace_uid,
    validated_posix_path,
)
from remote_provision_artifact import ArtifactError, ArtifactSelection  # noqa: E402
from remote_provision_driver import DriverProfile, select_artifact  # noqa: E402
from remote_update_activation_adapter import (  # noqa: E402
    RegistryActivationAdapter,
)
from remote_update_backup_port import (  # noqa: E402
    RemoteMaintenanceBackupPort,
    RestoreSource,
)
from remote_update_flow import RemoteUpdateFlow  # noqa: E402
from remote_update_flow_contract import PortRefusal, RemoteUpdatePorts  # noqa: E402
from remote_update_host_factory_activation import (  # noqa: E402
    RegistryFlowActivationPort,
)
from remote_update_host_recovery_gate import (  # noqa: E402
    EVIDENCE_PREPARED,
    GATED_ACTIVATION,
    GATED_OWNER,
    DurableEvidenceGate,
    RestoreSourceRecorder,
    guarded_port,
)
from remote_update_install_ports import (  # noqa: E402
    InstallPortInputs,
    InstallPortsConfigError,
    RemoteUpdateInstallPorts,
)
from remote_update_install_ports_inspection import (  # noqa: E402
    MODE_VERIFIED_UNPACK,
    LoopbackSession,
    RetainedPreparation,
)
from remote_update_journal import (  # noqa: E402
    JournalBinding,
    JournalUnavailable,
    RemoteUpdateJournal,
)
from remote_update_maintenance_transport import MaintenanceTarget  # noqa: E402
from remote_update_owner_port import RemoteOwnerStopPort  # noqa: E402
from remote_update_skill import build_skill_port  # noqa: E402
from remote_verified_unpack import admit_unpack_bundle  # noqa: E402
from ssh_profile_metadata import run_remote_profile_metadata_check  # noqa: E402
from ssot_connection import RemoteConnectionProfile  # noqa: E402


#: The prepared application tree carries both remote helpers the update needs.
#: They execute from the *staged* tree, never from the application being
#: stopped, which on a 1.0.8 remote does not ship either of them.
MAINTENANCE_RELATIVE = "desktop/python-webview-shell/remote_update_maintenance.py"
OWNER_OBSERVATION_RELATIVE = (
    "desktop/python-webview-shell/remote_update_owner_observation.py"
)
MAINTENANCE_RECEIPTS_RELATIVE = (
    "desktop/python-webview-shell/remote_update_maintenance_receipts.py"
)

#: Exactly the helper payload this flow executes out of the staged tree.  The
#: maintenance adapter reads and writes its receipts through its own receipts
#: module, so shipping the adapter without it is not a helper-bearing bundle.
REQUIRED_HELPERS = (
    OWNER_OBSERVATION_RELATIVE,
    MAINTENANCE_RELATIVE,
    MAINTENANCE_RECEIPTS_RELATIVE,
)

#: The document the remote runtime serves at its root, and therefore the one
#: whose admitted digest the verification stage compares what it measured to.
SERVED_UI_RELATIVE = "frontend/dist/index.html"

#: Where the desktop keeps the Linux application bundle it offers to a remote.
BUNDLE_DIRECTORY = "remote"

REFUSED_NO_PROFILE = "factory_no_selected_profile"
REFUSED_NO_TOKEN = "factory_no_session_token"
REFUSED_NO_ARTIFACT = "factory_no_admitted_artifact"
REFUSED_TARGET = "factory_target_invalid"
REFUSED_INPUTS = "factory_inputs_invalid"
REFUSED_MODE = "factory_install_mode_not_code_only"
REFUSED_JOURNAL = "factory_journal_unavailable"
#: Read-only stages have nothing to read until a candidate has been staged.
REFUSED_NOT_STAGED = "prepare_not_staged"
#: A bundle that predates the helpers the staged tree has to execute from.
REFUSED_BUNDLE_INCOMPATIBLE = "factory_bundle_predates_helpers"
#: A bundle carrying no served root document to verify the update against.
REFUSED_BUNDLE_NO_SERVED_UI = "factory_bundle_has_no_served_ui"


class RemoteUpdateFactoryRefused(RuntimeError):
    """The host cannot compose a production flow from what it has.

    Carries one bounded symbolic code.  There is no partial construction and no
    fallback port: the caller either gets a flow every stage of which reaches
    real code, or gets this.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CodeOnlyStagingPorts:
    """``RemoteUpdateInstallPorts`` plus the code-only staging entry point.

    ``CodeOnlyPreparePort`` asks for ``prepare_code_only``: stage the new
    application into an absent directory, verify the archive, the sidecar,
    every payload hash, the imports and the completion receipt, and stop.  In
    ``verified_unpack`` mode the install port's own ``prepare`` already *is*
    exactly that -- it runs the frozen verified-unpack helper, which by
    contract places an absent directory, touches no store, activates nothing
    and replaces no application -- so this delegates to it under the same
    operation id and reconciles through the same ``observe``.

    It is deliberately not available in ``transactional`` mode, where
    preparation is a full remote install and running it before the owner has
    stopped is the one thing the prepare-first order must never do.  That
    refusal is a construction error, not something discovered at the stage.

    It also routes the two *read-only* stages to the staging identity.  The
    flow gives PROBE and VERIFY fresh operation ids -- correctly, since neither
    is a mutation it records -- while ``RemoteUpdateInstallPorts.probe`` and
    ``.verify`` answer only for the operation that staged the candidate, and
    refuse any other id outright.  Both stages are about the *same* staged
    directory, so this remembers the id the staging ran under and reads the
    candidate back through it.  Nothing is issued either way; a stage that runs
    before anything was staged is refused rather than answered.

    This class exists only until ``RemoteUpdateInstallPorts`` exposes
    ``prepare_code_only``; it adds no second staging implementation, no second
    archive verifier, and forwards every call to the real port.
    """

    def __init__(
        self,
        ports: RemoteUpdateInstallPorts,
        retained: RetainedPreparation | None = None,
        on_prepared: Callable[[RetainedPreparation], bool] | None = None,
        gate: DurableEvidenceGate | None = None,
    ) -> None:
        if ports.inputs.install_mode != MODE_VERIFIED_UNPACK:
            raise RemoteUpdateFactoryRefused(REFUSED_MODE)
        self._ports = ports
        self._staged: str | None = None
        self._retained = retained
        self._on_prepared = on_prepared
        self._gate = gate

    @property
    def ports(self) -> RemoteUpdateInstallPorts:
        return self._ports

    @property
    def staged_operation_id(self) -> str | None:
        """The identity the candidate was staged under, once one has run."""

        return self._staged

    def prepare_code_only(self, operation_id: str):
        return self.prepare(operation_id)

    def prepare(self, operation_id: str):
        self._staged = operation_id
        outcome = self._ports.prepare(operation_id)
        if getattr(outcome, "status", "") == "verified":
            self._publish_retained(operation_id)
        return outcome

    def retained_evidence(self, operation_id: str) -> RetainedPreparation:
        """What a later process needs to re-verify this exact staging."""

        inputs = self._ports.inputs
        return RetainedPreparation(
            operation_id=operation_id,
            target_app_dir=inputs.target_app_dir,
            expected_workspace_id=inputs.expected_workspace_id,
            artifact_digest=inputs.artifact.digest,
            artifact_manifest_sha256=inputs.artifact.manifest_digest,
        )

    def _publish_retained(self, operation_id: str) -> None:
        """Hand the host the evidence, the moment the staging actually verified.

        Not before: an unissued or lost preparation is not something a restart
        may read a candidate back on.  The staging really did verify and its
        outcome is returned untouched -- a local write that did not land is not
        a remote failure.  A write that did not land, false answer or raised one
        alike, stays outstanding in the gate under this same operation id, and
        the next mutating stage refuses until it does.
        """

        publish = self._on_prepared
        if publish is None or self._gate is None:
            return
        evidence = self.retained_evidence(operation_id)
        self._gate.record(EVIDENCE_PREPARED, operation_id, lambda: publish(evidence))

    def observe(self, operation_id: str):
        return self._ports.observe(operation_id)

    def describe(self):
        return self._ports.describe()

    def probe(self, operation_id: str):
        # The first stage that runs the new code against the store, and the
        # first that can see a backup archive the host failed to record.
        if self._gate is not None:
            self._gate.require()
        return self._ports.probe(self._readable(operation_id))

    def verify(self, operation_id: str):
        return self._ports.verify(self._readable(operation_id))

    def _readable(self, operation_id: str) -> str:
        """The identity the candidate can be read back under, in this process.

        A staging that ran here is read back under its own identity.  After a
        restart there is none -- the staging settled in the previous process --
        so the durable evidence the host kept is offered to the port's public
        read-only recovery entry point, which re-verifies the actual unpack
        receipt for this exact admitted artifact and canonical target before
        admitting anything.  Nothing is fabricated and nothing is issued: a
        process with no retained evidence, or one whose candidate no longer
        re-verifies, is refused here rather than answered.
        """

        staged = self._staged
        if staged is not None:
            return staged
        retained = self._retained
        if retained is None:
            raise PortRefusal(REFUSED_NOT_STAGED)
        if not self._ports.admit_retained_preparation(retained):
            raise PortRefusal(REFUSED_NOT_STAGED)
        self._staged = retained.operation_id
        return self._staged


@dataclass(frozen=True)
class RemoteUpdateHostInputs:
    """Everything the host resolved, before anything is composed.

    Every field is supplied.  Nothing here is discovered, defaulted from the
    environment or inferred from the current application: a value the host
    could not establish is absent, and an absent value is a refusal.
    """

    state_root: Path
    profile: SshConnectionProfile
    remote_profile: RemoteConnectionProfile
    owner: str
    session_token: object
    session_id: str
    update_attempt_id: str
    artifact: ArtifactSelection
    target_app_dir: str
    #: The *pre-activation* application directory the candidate sits beside.
    #: Empty means the selected profile still points at it; after the
    #: activation has moved the registry it no longer does, and the recovering
    #: host supplies the directory the attempt actually started from.
    current_app_dir: str
    remote_state_root: str
    remote_backup_root: str
    ssh_executable: str
    observe_current: Callable[[], LoopbackSession]
    install_mode: str = MODE_VERIFIED_UNPACK
    restore_source: RestoreSource | None = None
    #: The admitted manifest's own digest for the document the remote serves at
    #: its root.  ``None`` leaves the verification's served comparison unknown.
    expected_served_ui_sha256: str | None = None
    #: Durable evidence of a staging a previous process ran, re-verified by the
    #: install port before any read-only stage is answered.
    retained_preparation: RetainedPreparation | None = None
    #: Told once, immediately, when a staging verifies and when the backup port
    #: learns the verified archive it would restore from.  Each answers whether
    #: the write landed; anything else, an exception included, keeps that
    #: evidence outstanding and refuses the next mutating stage.
    on_prepared: Callable[[RetainedPreparation], bool] | None = None
    on_restore_source: Callable[[RestoreSource], bool] | None = None
    #: Told once when the update is whole and the flow has durably released
    #: its journal anchor, so the host may retire the side record of the attempt
    #: that just finished -- and only that one.  Never before the anchor is
    #: gone: a record retired first would strand the next process.
    on_verified: Callable[[], object] | None = None
    profile_probe: Callable[[SshConnectionProfile], object] = run_remote_profile_metadata_check
    process_factory: Callable[..., object] | None = None
    stop_runner: object = None
    mutation_service: ConnectionRegistryMutationService | None = None
    activation_adapter: RegistryActivationAdapter | None = None
    journal_factory: Callable[[Path, JournalBinding], object] | None = None


@dataclass(frozen=True)
class ComposedRemoteUpdate:
    """The flow, and the production objects it was composed from.

    The host keeps the parts because they answer questions the snapshot cannot:
    the backup port names the verified archive a restart has to recover, and
    the activation port names the candidate probe the registry was moved on.
    """

    flow: RemoteUpdateFlow
    session_id: str
    workspace_id: str
    profile_id: str
    update_attempt_id: str
    target_app_dir: str
    install: RemoteUpdateInstallPorts
    prepare: "CodeOnlyStagingPorts"
    owner: RemoteOwnerStopPort
    backup: RemoteMaintenanceBackupPort
    activation: RegistryFlowActivationPort
    journal: object
    current_app_dir: str = ""
    operation_ids: tuple[str, ...] = field(default_factory=tuple)
    #: Which recovery evidence is still not on disk, and so what the next
    #: mutating stage is refusing on.
    gate: DurableEvidenceGate | None = None


def candidate_app_dir(current_app_dir: str, product_version: str) -> str:
    """The absent directory beside the current application, named by version.

    A sibling, never a child: the update must be able to fail with the current
    application untouched and inspectable, and a candidate underneath it would
    make the two share a fate.
    """

    try:
        current = validated_posix_path(current_app_dir, "remote_app_dir")
    except RemoteCommandError:
        raise RemoteUpdateFactoryRefused(REFUSED_TARGET) from None
    if not product_version or any(
        character in product_version for character in "/\\ \t\r\n"
    ):
        raise RemoteUpdateFactoryRefused(REFUSED_TARGET)
    try:
        return validated_posix_path(f"{current}-{product_version}", "remote_app_dir")
    except RemoteCommandError:
        raise RemoteUpdateFactoryRefused(REFUSED_TARGET) from None


#: Where the remote adapter keeps its own state and archives, outside the store.
REMOTE_STATE_RELATIVE = ".workstack-maintenance/state"
REMOTE_BACKUP_RELATIVE = ".workstack-maintenance/backups"


def remote_maintenance_roots(remote_data_dir: str) -> tuple[str, str]:
    """The adapter's state and archive roots, beside the store and never in it.

    The adapter re-checks this on its own side; deriving the pair here keeps the
    two halves of the answer from drifting apart.
    """

    parent = remote_data_dir.rsplit("/", 1)[0] or "/"
    base = parent if parent.endswith("/") else parent + "/"
    return f"{base}{REMOTE_STATE_RELATIVE}", f"{base}{REMOTE_BACKUP_RELATIVE}"


def maintenance_helper_path(target_app_dir: str) -> str:
    """Where the maintenance adapter runs from: the staged tree, not the old app."""

    try:
        return validated_posix_path(
            f"{target_app_dir}/{MAINTENANCE_RELATIVE}", "remote_helper_path"
        )
    except RemoteCommandError:
        raise RemoteUpdateFactoryRefused(REFUSED_TARGET) from None


def bundle_paths(install_root: Path, product_version: str) -> tuple[Path, Path]:
    """The installed Linux bundle for this version, archive and sidecar."""

    directory = Path(install_root) / BUNDLE_DIRECTORY
    stem = f"WorkStack-Linux-{product_version}-cp312-manylinux_2_17_x86_64"
    return directory / f"{stem}.zip", directory / f"{stem}.json"


@dataclass(frozen=True)
class AdmittedBundle:
    """One admitted artifact, plus what its own manifest says the update serves."""

    artifact: ArtifactSelection
    expected_served_ui_sha256: str


def admitted_bundle(archive: Path, sidecar: Path) -> AdmittedBundle:
    """Admit the installed bundle through the existing artifact selection.

    Reads two files and hands their bytes to ``select_artifact``.  There is no
    second archive validator here and nothing is fetched: an absent, unreadable
    or unadmitted bundle simply means this desktop has no remote update to
    offer.
    """

    try:
        archive_bytes = Path(archive).read_bytes()
        sidecar_bytes = Path(sidecar).read_bytes()
    except OSError:
        raise RemoteUpdateFactoryRefused(REFUSED_NO_ARTIFACT) from None
    try:
        artifact = select_artifact(archive_bytes, sidecar_bytes)
    except (ArtifactError, Exception):  # noqa: BLE001 - DriverError family is open
        raise RemoteUpdateFactoryRefused(REFUSED_NO_ARTIFACT) from None
    return require_helper_bearing(artifact)


def manifest_identities(artifact: ArtifactSelection) -> dict[str, str]:
    """Every file identity the admitted manifest itself carries.

    This is the same admission gate the remote unpack runs, applied here to the
    bytes already on this disk.  It reads the archive and its sidecar and
    nothing else: no network, no remote, no second manifest parser.
    """

    try:
        admitted = admit_unpack_bundle(artifact.archive_bytes, artifact.sidecar_bytes)
    except Exception:  # noqa: BLE001 - the installer refusal family is open
        raise RemoteUpdateFactoryRefused(REFUSED_NO_ARTIFACT) from None
    identities: dict[str, str] = {}
    for record in admitted.get("files") or ():
        if not isinstance(record, dict):
            continue
        path = record.get("path")
        digest = record.get("sha256")
        if isinstance(path, str) and isinstance(digest, str) and digest:
            identities[path] = digest
    return identities


def require_helper_bearing(artifact: ArtifactSelection) -> AdmittedBundle:
    """Refuse a bundle whose own manifest does not carry the helpers.

    The owner observation and the maintenance adapter -- and the receipts
    module the adapter records through -- execute out of the *staged* tree.
    Whether they are there is a fact about the admitted artifact, and the
    manifest states it as file identities, so that is what is checked.  A
    product version is not a capability: the published bytes of this very
    version do not carry these helpers, and a version comparison would offer
    the page and then discover the absent executable halfway through a stop.

    The served root document is admitted here too, because the verification
    stage compares what the updated remote actually serves against the digest
    this bundle declares; a bundle that declares none could never be verified.

    This reads two local files.  It installs nothing and probes nothing.
    """

    identities = manifest_identities(artifact)
    for relative in REQUIRED_HELPERS:
        if not identities.get(relative):
            raise RemoteUpdateFactoryRefused(REFUSED_BUNDLE_INCOMPATIBLE)
    served = identities.get(SERVED_UI_RELATIVE)
    if not served:
        raise RemoteUpdateFactoryRefused(REFUSED_BUNDLE_NO_SERVED_UI)
    return AdmittedBundle(artifact=artifact, expected_served_ui_sha256=served)


def _operation_ids() -> Callable[[str], str]:
    """One fresh canonical identity per issued mutation.

    Not derived from the stage: two attempts at the same stage are two
    different mutations, and reusing an identity would make the second one
    reconcile the first one's answer.  Durability is the journal's job.
    """

    def mint(_stage: str) -> str:
        return str(uuid.uuid4())

    return mint


def _admit(inputs: RemoteUpdateHostInputs) -> tuple[str, str]:
    """Check the host's inputs name one real selected remote, or refuse."""

    if not isinstance(inputs.profile, SshConnectionProfile):
        raise RemoteUpdateFactoryRefused(REFUSED_NO_PROFILE)
    if not isinstance(inputs.remote_profile, RemoteConnectionProfile):
        raise RemoteUpdateFactoryRefused(REFUSED_NO_PROFILE)
    if not isinstance(inputs.session_token, str) or not inputs.session_token:
        raise RemoteUpdateFactoryRefused(REFUSED_NO_TOKEN)
    if not isinstance(inputs.artifact, ArtifactSelection):
        raise RemoteUpdateFactoryRefused(REFUSED_NO_ARTIFACT)
    if not callable(inputs.observe_current):
        raise RemoteUpdateFactoryRefused(REFUSED_INPUTS)
    try:
        workspace = require_workspace_uid(inputs.profile.expected_workspace_id)
        require_posix_owner(inputs.owner)
    except RemoteCommandError:
        raise RemoteUpdateFactoryRefused(REFUSED_INPUTS) from None
    if inputs.remote_profile.workspace_id != workspace:
        raise RemoteUpdateFactoryRefused(REFUSED_INPUTS)
    for value in (inputs.session_id, inputs.update_attempt_id, inputs.ssh_executable):
        if not isinstance(value, str) or not value:
            raise RemoteUpdateFactoryRefused(REFUSED_INPUTS)
    return workspace, inputs.profile.profile_id


def _install_ports(
    inputs: RemoteUpdateHostInputs, workspace: str
) -> RemoteUpdateInstallPorts:
    current = DriverProfile(
        ssh_host_alias=inputs.profile.ssh_host_alias,
        remote_python=inputs.remote_profile.remote_python or "",
        remote_app_dir=inputs.current_app_dir or inputs.profile.remote_app_dir,
        remote_data_dir=inputs.profile.remote_data_dir,
        expected_workspace_id=workspace,
    )
    extra: dict[str, object] = {}
    if inputs.process_factory is not None:
        extra["process_factory"] = inputs.process_factory
    try:
        return RemoteUpdateInstallPorts(
            InstallPortInputs(
                current_profile=current,
                target_app_dir=inputs.target_app_dir,
                artifact=inputs.artifact,
                expected_workspace_id=workspace,
                owner=inputs.owner,
                ssh_executable=inputs.ssh_executable,
                install_mode=inputs.install_mode,
                observe_current=inputs.observe_current,
                expected_served_ui_sha256=inputs.expected_served_ui_sha256,
                **extra,
            )
        )
    except InstallPortsConfigError:
        raise RemoteUpdateFactoryRefused(REFUSED_TARGET) from None


def _backup_port(
    inputs: RemoteUpdateHostInputs, workspace: str
) -> RemoteMaintenanceBackupPort:
    target = MaintenanceTarget(
        ssh_host_alias=inputs.profile.ssh_host_alias,
        remote_python=inputs.remote_profile.remote_python or "",
        remote_helper_path=maintenance_helper_path(inputs.target_app_dir),
        remote_workspace_dir=inputs.profile.remote_data_dir,
        workspace_id=workspace,
        remote_state_root=inputs.remote_state_root,
        remote_backup_root=inputs.remote_backup_root,
    )
    extra: dict[str, object] = {}
    if inputs.process_factory is not None:
        extra["process_factory"] = inputs.process_factory
    try:
        return RemoteMaintenanceBackupPort(
            target,
            ssh_executable=inputs.ssh_executable,
            restore_source=inputs.restore_source,
            **extra,
        )
    except Exception:  # noqa: BLE001 - the transport's refusal family is open
        raise RemoteUpdateFactoryRefused(REFUSED_INPUTS) from None


def _journal(inputs: RemoteUpdateHostInputs, workspace: str, profile_id: str) -> object:
    try:
        binding = JournalBinding(
            workspace_id=workspace,
            profile_id=profile_id,
            update_attempt_id=inputs.update_attempt_id,
        )
    except JournalUnavailable:
        raise RemoteUpdateFactoryRefused(REFUSED_INPUTS) from None
    factory = inputs.journal_factory or RemoteUpdateJournal
    try:
        return factory(inputs.state_root, binding)
    except JournalUnavailable:
        raise RemoteUpdateFactoryRefused(REFUSED_JOURNAL) from None


def build_remote_update_flow(
    inputs: RemoteUpdateHostInputs,
) -> ComposedRemoteUpdate:
    """Compose the production flow, or refuse with one bounded code.

    Order matters here only in one way: the journal is loaded by
    ``RemoteUpdateFlow.__init__``, so it is built last and any unreadable
    record refuses the whole composition rather than starting a second
    mutation over an identity nobody reconciled.
    """

    workspace, profile_id = _admit(inputs)
    # No host recorder is no recovery evidence to keep, and so nothing to gate.
    gate = DurableEvidenceGate()
    guard = inputs.on_prepared is not None or inputs.on_restore_source is not None
    install = _install_ports(inputs, workspace)
    prepare = CodeOnlyStagingPorts(
        install,
        retained=inputs.retained_preparation,
        on_prepared=inputs.on_prepared,
        gate=gate,
    )
    owner = RemoteOwnerStopPort(
        inputs.remote_profile,
        inputs.session_token,
        observation_app_dir=inputs.target_app_dir,
        ssh_executable=inputs.ssh_executable,
        runner=inputs.stop_runner,
    )
    backup = _backup_port(inputs, workspace)
    backup_port = (
        backup
        if inputs.on_restore_source is None
        else RestoreSourceRecorder(backup, inputs.on_restore_source, gate=gate)
    )
    mutations = inputs.mutation_service or ConnectionRegistryMutationService(
        inputs.state_root
    )
    adapter = inputs.activation_adapter or RegistryActivationAdapter(
        inputs.state_root, mutation_service=mutations
    )
    activation = RegistryFlowActivationPort(
        inputs.state_root,
        profile_id=profile_id,
        target_app_dir=inputs.target_app_dir,
        expected_workspace_id=workspace,
        adapter=adapter,
        mutation_service=mutations,
        probe=inputs.profile_probe,
    )
    journal = _journal(inputs, workspace, profile_id)
    try:
        flow = RemoteUpdateFlow(
            RemoteUpdatePorts(
                preview=prepare,
                # The stop is the first mutation after the staging; the
                # activation is the one that cannot be taken back without the
                # evidence.  Observations and the rollback stay ungated.
                owner=guarded_port(owner, gate, GATED_OWNER, guard),
                backup=backup_port,
                prepare=prepare,
                probe=prepare,
                activation=guarded_port(activation, gate, GATED_ACTIVATION, guard),
                verification=prepare,
                # Optional and ungated.  The Skill step touches no owner, no
                # activation and no SSOT, so there is no durable evidence to
                # guard; it is bound to the same install ports the update
                # prepared, and is simply absent -- and never offered -- where
                # this build carries no reviewed skill command builder.
                skill=build_skill_port(install),
            ),
            operation_ids=_operation_ids(),
            journal=journal,
            prepare_before_stop=True,
            on_completed=inputs.on_verified,
        )
    except JournalUnavailable:
        raise RemoteUpdateFactoryRefused(REFUSED_JOURNAL) from None
    except ValueError:
        raise RemoteUpdateFactoryRefused(REFUSED_MODE) from None
    return ComposedRemoteUpdate(
        flow=flow,
        session_id=inputs.session_id,
        workspace_id=workspace,
        profile_id=profile_id,
        update_attempt_id=inputs.update_attempt_id,
        target_app_dir=inputs.target_app_dir,
        install=install,
        prepare=prepare,
        owner=owner,
        backup=backup,
        activation=activation,
        journal=journal,
        current_app_dir=install.inputs.current_profile.remote_app_dir,
        gate=gate,
    )


__all__ = [
    "AdmittedBundle",
    "BUNDLE_DIRECTORY",
    "ComposedRemoteUpdate",
    "CodeOnlyStagingPorts",
    "MAINTENANCE_RECEIPTS_RELATIVE",
    "MAINTENANCE_RELATIVE",
    "OWNER_OBSERVATION_RELATIVE",
    "REQUIRED_HELPERS",
    "REFUSED_BUNDLE_NO_SERVED_UI",
    "SERVED_UI_RELATIVE",
    "manifest_identities",
    "REFUSED_INPUTS",
    "REFUSED_JOURNAL",
    "REFUSED_MODE",
    "REFUSED_NO_ARTIFACT",
    "REFUSED_NO_PROFILE",
    "REFUSED_BUNDLE_INCOMPATIBLE",
    "REFUSED_NOT_STAGED",
    "REFUSED_NO_TOKEN",
    "REFUSED_TARGET",
    "RemoteUpdateFactoryRefused",
    "RemoteUpdateHostInputs",
    "admitted_bundle",
    "build_remote_update_flow",
    "bundle_paths",
    "candidate_app_dir",
    "require_helper_bearing",
    "maintenance_helper_path",
    "REMOTE_BACKUP_RELATIVE",
    "REMOTE_STATE_RELATIVE",
    "remote_maintenance_roots",
]

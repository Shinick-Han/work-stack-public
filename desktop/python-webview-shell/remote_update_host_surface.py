"""What ``WorkStackDesktopHost`` has to own for the native update page to work.

``RemoteUpdateHostBridgeMixin`` declares four host-owned seams and refuses
rather than simulating any of them.  This mixin is where they stop being
refusals: it resolves the selected connection into a real
:class:`~remote_update_host_factory.ComposedRemoteUpdate`, mounts the dedicated
WebView the page paints into, answers the frontend's ``remote-open`` request,
turns what this start actually observed into ``StartupEvidence``, and begins the
restart a pending activation requires.

Three rules shape all of it.

**Capability follows construction.**  ``remote_update_capability`` is true only
when :func:`build_remote_update_flow` has actually produced a flow for the
current selection.  No configured profile, no session token, no installed Linux
bundle, no readable journal -- any of those and the desktop offers no remote
update at all, rather than offering one that would refuse at the first click.

**One selection at a time.**  The composed flow, the session id and the page are
bound to one *runtime* selection, not merely to a profile id: one canonical
endpoint fingerprint over the connection fields the ports address, plus the
reallocated local forward port, the settled session's own generation and its
token digest.  A reconnect that rotates any of those retires the page and
recomposes, while a same-profile move to another host, data root, remote port
or interpreter changes the fingerprint the durable record is bound to, so the
previous endpoint's evidence is never adopted.  The token is never stored.

**Close does not complete anything.**  Retiring the page tears down the view and
nothing else.  A restart is begun by closing the window and letting the existing
shutdown path run; the relaunch happens after that path has released the single
instance, and the pending activation stays pending until the next start
*observes* that it was selected.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_SHELL_DIR = str(Path(__file__).resolve(strict=True).parent)
_ROOT = str(Path(_SHELL_DIR).parents[1])
for _path in (_SHELL_DIR, _ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from connection_registry import SshConnectionProfile  # noqa: E402
from remote_update_activation_binding import (  # noqa: E402
    ActivationBindingError,
    ActivationBindingStore,
)
from remote_update_controller import RemoteUpdateSelection  # noqa: E402
from remote_update_flow_contract import StartupEvidence  # noqa: E402
from remote_update_host_bridge import RemoteUpdateHostBridgeMixin  # noqa: E402
from remote_update_host_surface_view import RemoteUpdateHostViewMixin  # noqa: E402
from remote_update_backup_port import RestoreSource  # noqa: E402
from remote_update_host_factory import (  # noqa: E402
    REMOTE_BACKUP_RELATIVE,
    REMOTE_STATE_RELATIVE,
    ComposedRemoteUpdate,
    RemoteUpdateFactoryRefused,
    RemoteUpdateHostInputs,
    admitted_bundle,
    build_remote_update_flow,
    bundle_paths,
    candidate_app_dir,
    remote_maintenance_roots,
)
from remote_update_host_surface_record import (  # noqa: E402
    PreparedCandidate,
    RemoteEndpoint,
    RemoteUpdateSession,
    clear_session,
    endpoint_of,
    load_session,
    new_attempt_id,
    retire_session,
    write_session,
)
from remote_update_install_ports_inspection import (  # noqa: E402
    LoopbackSession,
    RetainedPreparation,
)
from workstack import __version__ as DESKTOP_VERSION  # noqa: E402


#: The branded launcher an activation restart comes back through.
DESKTOP_LAUNCHER = "WorkStack.exe"

#: The binding digest joins its parts on a character none of them may contain.
BINDING_SEPARATOR = "\n"

#: The durable side record could not be read, and therefore could not be shown
#: to be absent.  Present-but-unadmitted is not the same as never begun.
REFUSED_RECORD_AMBIGUOUS = "host_attempt_record_ambiguous"
#: The attempt could not be made durable, so nothing may be issued under it.
REFUSED_RECORD_NOT_DURABLE = "host_attempt_record_not_durable"


@dataclass(frozen=True)
class RemoteUpdateCandidate:
    """Which two directories, which attempt, and what the record retained.

    ``current_app_dir`` is the *pre-activation* application.  Once the
    activation has moved the registry, the selected profile is the candidate
    itself, and deriving a new candidate from it would name a directory nobody
    ever staged; the recorded pair is used instead, unchanged.
    """

    current_app_dir: str
    target_app_dir: str
    update_attempt_id: str
    prepared: PreparedCandidate | None = None
    restore_source: RestoreSource | None = None


def workspace_match(observed: object, expected: str) -> str:
    """Three answers, never two.  Unreachable is unknown, not a mismatch."""

    if observed is None:
        return "unknown"
    return "verified" if observed == expected else "failed"


class RemoteUpdateHostSurfaceMixin(RemoteUpdateHostViewMixin, RemoteUpdateHostBridgeMixin):
    """The desktop half of the remote update page."""

    # -- lifetime ---------------------------------------------------------

    def _init_remote_update_surface(self) -> None:
        """Publish every field the bridge and the controller may read.

        Called at the end of ``__init__``, before any WebView, thread or
        callback exists, so no later code can observe a half-built surface.
        """

        self._init_remote_update_view()
        self.remote_update_composed: ComposedRemoteUpdate | None = None
        self.remote_update_selected_profile_id = ""
        self.remote_update_selection_binding = ""
        self.remote_update_prepared: PreparedCandidate | None = None
        self.remote_update_restore_source: RestoreSource | None = None
        self.remote_update_generation = 0
        self.remote_update_restart_requested = False
        self.remote_update_refusal = ""
        self.remote_update_available = False
        self._init_remote_update_bridge()

    def _settle_remote_update_capability(self) -> None:
        """Compose the flow once, off the UI thread, and resume what it holds.

        Composition reads the installed Linux bundle and resolves the alias's
        OpenSSH configuration, so it belongs on the host's startup thread
        rather than in the status post the WebView asks for.  The answer is
        cached, and the page is offered only when a real flow exists.

        A retained activation receipt is resumed here too, against what this
        start observed.  It confirms nothing on its own: absent or disagreeing
        evidence leaves the receipt exactly as pending as it was.
        """

        try:
            self.remote_update_available = self.remote_update_capability()
        except Exception as error:  # noqa: BLE001 - never fail a start over this
            self.remote_update_available = False
            self._trace(f"remote update capability unavailable: {type(error).__name__}")
            return
        if self.remote_update_available:
            self._resume_remote_update_after_restart()

    def _remote_update_profile_id(self) -> str:
        return str(getattr(self, "runtime_connection_profile_id", "") or "")

    def _remote_update_endpoint(self) -> RemoteEndpoint | None:
        """The one canonical endpoint identity, exactly as the ports address it.

        Every field is a value some production port is handed: the alias and
        data root the transports open, the port the remote itself serves on,
        the interpreter the helpers run with, and the profile and workspace
        claimed.  Both bindings -- the in-memory one below and the durable
        record's -- digest this one object, not their own subsets.
        """

        return endpoint_of(
            self._registry_profile(), getattr(self, "remote_profile", None)
        )

    def _remote_update_endpoint_fingerprint(self) -> str:
        endpoint = self._remote_update_endpoint()
        return "" if endpoint is None else endpoint.fingerprint()

    def _remote_update_binding(self) -> str:
        """One digest over everything the composed flow actually speaks for.

        The profile id alone is not the selection.  A reconnect of the same
        profile mints a new attempt generation and a new session token while
        keeping that id, and a flow composed against the previous one would go
        on presenting a settled session the runtime has already replaced.

        This is the endpoint fingerprint plus what is *not* in it, and the
        split is the point.  The durable record is bound to the fingerprint
        alone, so a rotated token or a reallocated local forward does not
        discard evidence produced against that very endpoint; they recompose
        this process's view of it and nothing more.

        The token is never kept, published or traced: it contributes its own
        digest and nothing else.
        """

        endpoint = self._remote_update_endpoint()
        profile = self._registry_profile()
        remote = getattr(self, "remote_profile", None)
        session = self._settled_remote_session()
        token = getattr(session, "token", None)
        if endpoint is None or profile is None:
            return ""
        if not isinstance(token, str) or not token:
            return ""
        parts = (
            endpoint.fingerprint(),
            profile.remote_app_dir,
            str(getattr(remote, "workspace_id", "")),
            str(getattr(remote, "remote_app_dir", "")),
            str(getattr(remote, "remote_data_dir", "")),
            str(getattr(remote, "ssh_host_alias", "")),
            # Reallocated whenever the tunnel is rebuilt, so it belongs to this
            # process's view of the endpoint and never to the record.
            str(getattr(remote, "local_forward_port", "")),
            str(getattr(session, "generation", "")),
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
        )
        return hashlib.sha256(BINDING_SEPARATOR.join(parts).encode("utf-8")).hexdigest()

    def _registry_profile(self) -> SshConnectionProfile | None:
        registry = getattr(self, "connection_registry_snapshot", None)
        profile_id = self._remote_update_profile_id()
        if registry is None or not profile_id:
            return None
        for profile in registry.profiles:
            if profile.profile_id == profile_id:
                return profile if isinstance(profile, SshConnectionProfile) else None
        return None

    # -- composition ------------------------------------------------------

    def _remote_update_inputs(self) -> RemoteUpdateHostInputs | None:
        """Resolve the selected connection into the factory's typed inputs.

        Every absent answer here is a reason this desktop has no remote update,
        and each returns ``None`` rather than a placeholder.
        """

        profile = self._registry_profile()
        remote_profile = getattr(self, "remote_profile", None)
        if profile is None or remote_profile is None:
            return None
        session = self._settled_remote_session()
        token = None if session is None else getattr(session, "token", None)
        if not isinstance(token, str) or not token:
            return None
        try:
            bundle = admitted_bundle(*bundle_paths(self.install_root, DESKTOP_VERSION))
            candidate = self._remote_update_candidate(profile, bundle.artifact)
        except RemoteUpdateFactoryRefused as error:
            self.remote_update_refusal = error.code
            return None
        if candidate is None:
            return None
        self.remote_update_prepared = candidate.prepared
        self.remote_update_restore_source = candidate.restore_source
        state_root, backup_root = remote_maintenance_roots(profile.remote_data_dir)
        return RemoteUpdateHostInputs(
            state_root=self.state_root,
            profile=profile,
            remote_profile=remote_profile,
            owner=self._remote_update_owner(),
            session_token=token,
            session_id=str(uuid.uuid4()),
            update_attempt_id=candidate.update_attempt_id,
            artifact=bundle.artifact,
            target_app_dir=candidate.target_app_dir,
            current_app_dir=candidate.current_app_dir,
            remote_state_root=state_root,
            remote_backup_root=backup_root,
            ssh_executable=self._remote_update_ssh_executable(),
            observe_current=self._remote_update_loopback,
            restore_source=candidate.restore_source,
            expected_served_ui_sha256=bundle.expected_served_ui_sha256,
            retained_preparation=self._retained_preparation(
                candidate, profile.expected_workspace_id
            ),
            on_prepared=self._record_remote_update_prepared,
            on_restore_source=self._record_remote_update_restore_source,
            on_verified=self._verified_retire(candidate),
        )

    @staticmethod
    def _retained_preparation(
        candidate: RemoteUpdateCandidate, workspace_id: str
    ) -> RetainedPreparation | None:
        """The durable staging evidence, addressed to this exact candidate."""

        prepared = candidate.prepared
        if prepared is None:
            return None
        return RetainedPreparation(
            operation_id=prepared.operation_id,
            target_app_dir=candidate.target_app_dir,
            expected_workspace_id=workspace_id,
            artifact_digest=prepared.artifact_digest,
            artifact_manifest_sha256=prepared.artifact_manifest_sha256,
        )

    def _compose_remote_update(self) -> ComposedRemoteUpdate | None:
        """The flow for the current selection, composed once and reused.

        A selection change is not a refresh: the previous flow is retired and a
        wholly new one is composed under a new session id, because the old one
        speaks for another runtime's mutations.  "The same selection" means the
        same binding digest, so a same-profile reconnect that rotated the
        session or its token recomposes rather than being reused.
        """

        binding = self._remote_update_binding()
        composed = self.remote_update_composed
        if composed is not None and binding and self.remote_update_selection_binding == binding:
            return composed
        if composed is not None:
            self._retire_remote_update_surface()
        return self._settle_composition(binding)

    def _settle_composition(self, binding: str) -> ComposedRemoteUpdate | None:
        """Build one flow for this binding, or leave the desktop without one.

        The durable record is written before the flow is published.  A flow the
        next process could not address is not a capability: it could still be
        stopped, backed up and activated, and then have no attempt identity to
        resume, so a failed write refuses here instead of being telemetry.
        """

        profile_id = self._remote_update_profile_id()
        inputs = self._remote_update_inputs()
        if inputs is None:
            return self._no_composition(binding, profile_id)
        try:
            composed = build_remote_update_flow(inputs)
        except RemoteUpdateFactoryRefused as error:
            self.remote_update_refusal = error.code
            self._trace(f"remote update unavailable: {error.code}")
            return self._no_composition(binding, profile_id)
        if not self._persist_remote_update_session(composed):
            self.remote_update_refusal = REFUSED_RECORD_NOT_DURABLE
            self._trace("remote update unavailable: the attempt is not durable")
            return self._no_composition(binding, profile_id)
        self.remote_update_refusal = ""
        self.remote_update_composed = composed
        self.remote_update_selected_profile_id = profile_id
        self.remote_update_selection_binding = binding
        return composed

    def _no_composition(self, binding: str, profile_id: str) -> None:
        """Settle the failed answer without leaving a half-published flow."""

        self.remote_update_composed = None
        self.remote_update_selected_profile_id = profile_id
        self.remote_update_selection_binding = binding
        return None

    def remote_update_capability(self) -> bool:
        """Whether this desktop can actually run a remote update right now."""

        return self._compose_remote_update() is not None

    def _retire_remote_update_surface(self) -> None:
        """Drop the previous selection's page, flow and session together."""

        self.remote_update_generation += 1
        self._retire_remote_update_page()
        self.remote_update_composed = None
        self.remote_update_selected_profile_id = ""
        self.remote_update_selection_binding = ""
        self.remote_update_prepared = None
        self.remote_update_restore_source = None

    # -- host-owned seams -------------------------------------------------

    def _remote_update_selection(self) -> RemoteUpdateSelection | None:
        composed = self._compose_remote_update()
        if composed is None:
            return None
        return RemoteUpdateSelection(
            session_id=composed.session_id,
            workspace_id=composed.workspace_id,
            flow=composed.flow,
        )

    def _remote_update_startup_evidence(self) -> StartupEvidence | None:
        """What this start observed about the activation the flow is holding.

        Three independent observations, and the absence of any one of them is
        ``unknown``: which operation's receipt the live registry currently
        selects, whether it selects it, and which workspace the runtime this
        start brought up actually serves.  Nothing here confirms anything; the
        flow decides, and only on all three.
        """

        composed = self._compose_remote_update()
        if composed is None:
            return None
        operation_id, selected = self._selected_activation_operation()
        if operation_id is None:
            return None
        return StartupEvidence(
            selected_activation_id=operation_id,
            authority_selected=selected,
            workspace_match=workspace_match(
                self._remote_update_observed_workspace(), composed.workspace_id
            ),
        )

    def _selected_activation_operation(self) -> tuple[str | None, str]:
        """Whether the registry currently selects *this flow's* own activation.

        The registry mints its own activation id inside its own lock, so the
        flow's operation id and the receipt's are different identifiers.  The
        durable binding between them is the one the activation adapter already
        wrote, and the only operation that matters is the exact one this flow
        is holding: an older binding sitting in front of it in the store
        belongs to some earlier attempt, and reading its selection would report
        that attempt's outcome as this one's startup evidence.  So the retained
        receipt names the operation, the store is asked for that operation
        alone, and an absent binding or an unreadable receipt stays unknown --
        which leaves the receipt exactly as pending as it was.
        """

        composed = self.remote_update_composed
        retained = None if composed is None else composed.flow.retained_activation()
        if composed is None or retained is None:
            return None, "unknown"
        operation_id = retained.operation_id
        try:
            binding = ActivationBindingStore(self.state_root).find(operation_id)
        except ActivationBindingError:
            return None, "unknown"
        if binding is None:
            return None, "unknown"
        observed = self._observe_activation(composed, operation_id)
        selection = None if observed is None else observed.selection
        if selection == "selected":
            return operation_id, "verified"
        if selection == "not_selected":
            return operation_id, "failed"
        return operation_id, "unknown"

    def _observe_activation(self, composed: ComposedRemoteUpdate, operation_id: str):
        try:
            return composed.activation.observe_receipt(operation_id)
        except Exception:  # noqa: BLE001 - an unreadable receipt is not evidence
            return None

    def _remote_update_restart(self) -> bool:
        """Begin the restart, without claiming the activation is finished.

        Closing the window runs the existing shutdown path -- the owned server
        stop, the monitor teardown, the single-instance release -- and the
        relaunch happens after it, from the same finally block that already
        hands off a pending PC update.  Nothing is confirmed here; the next
        start has to observe the selection for itself.
        """

        form = getattr(self, "form", None)
        if form is None or bool(getattr(form, "IsDisposed", False)):
            self._trace("remote update restart refused: no window to close")
            return False
        self.remote_update_restart_requested = True
        # The bridge's own UI marshalling seam, not a second one: the close has
        # to happen on the thread the window belongs to, exactly like a paint.
        if not self._dispatch_remote_update_ui(form.Close):
            self.remote_update_restart_requested = False
            self._trace("remote update restart could not be marshalled")
            return False
        return True

    def _remote_update_pc_actions(self) -> tuple[str, ...]:
        """None.  The PC actions live in the frontend's own update popover.

        The remote wizard is one screen about one server; offering a PC action
        here would put it in front of the remote flow's own next action, which
        is exactly the ordering the presentation layer applies.
        """

        return ()

    # -- the frontend's entry point ---------------------------------------

    def _handle_remote_update_open(self) -> bool:
        """Answer ``workstack-update-host|remote-open`` from the frontend.

        Mounting is refused rather than faked: no capability, no native view,
        or a controller that will not admit the selection all end here with
        ``False`` and the page never appears.
        """

        if not self.remote_update_capability():
            self._trace("remote update page refused: no composed flow")
            return False
        if not self._mount_remote_update_view():
            return False
        if not self.remote_update_view_ready:
            # The control initializes asynchronously and is not usable yet.
            # The request is admitted and answered by the initialization
            # handler; nothing is painted and nothing covers the app until
            # then, so the first click no longer refuses behind a panel.
            self.remote_update_open_pending = True
            self._trace("remote update page waits for the view to initialize")
            return True
        return self._present_remote_update_page()

    # -- durable attempt identity -----------------------------------------

    def _remote_update_candidate(
        self, profile: SshConnectionProfile, artifact
    ) -> RemoteUpdateCandidate | None:
        """The exact pair of directories and the attempt this start resumes.

        Three answers, and only one of them derives anything.  A record that
        could not be read is ambiguous: an attempt may have been begun and its
        evidence lost, so no fresh attempt is minted over it and the bytes are
        left exactly as they are.  A record that names this workspace, this
        profile and either of its own two directories as the live one is
        resumed verbatim -- including after the activation, when the selected
        profile *is* the candidate and re-deriving would name a directory
        nobody ever staged.  Only a genuinely absent or foreign record begins a
        new attempt beside the live application.
        """

        read = load_session(self.state_root)
        if read.ambiguous:
            self.remote_update_refusal = REFUSED_RECORD_AMBIGUOUS
            self._trace("remote update unavailable: the attempt record is unreadable")
            return None
        recorded = read.session
        live = profile.remote_app_dir
        if recorded is not None and recorded.speaks_for(
            workspace_id=profile.expected_workspace_id,
            profile_id=profile.profile_id,
            endpoint_fingerprint=self._remote_update_endpoint_fingerprint(),
            live_app_dir=live,
        ):
            return RemoteUpdateCandidate(
                current_app_dir=recorded.current_app_dir,
                target_app_dir=recorded.target_app_dir,
                update_attempt_id=recorded.update_attempt_id,
                prepared=recorded.prepared,
                restore_source=recorded.restore_source,
            )
        return RemoteUpdateCandidate(
            current_app_dir=live,
            target_app_dir=candidate_app_dir(live, artifact.product_version),
            update_attempt_id=new_attempt_id(),
        )

    def _persist_remote_update_session(
        self, composed: ComposedRemoteUpdate | None
    ) -> bool:
        """Keep the attempt identity, the staging and the verified archive.

        One record, written whole: the attempt, both directories, the staging
        the install port may re-verify, and the archive a restore is bound to.
        Nothing is written piecemeal, so a later process never adopts a restore
        source without the attempt and target it belongs to.
        """

        if composed is None:
            return False
        # An unresolvable endpoint is not a record with an empty binding --
        # that would be adopted by whichever endpoint was selected next.
        fingerprint = self._remote_update_endpoint_fingerprint()
        if not fingerprint:
            return False
        return write_session(
            self.state_root,
            RemoteUpdateSession(
                workspace_id=composed.workspace_id,
                profile_id=composed.profile_id,
                endpoint_fingerprint=fingerprint,
                update_attempt_id=composed.update_attempt_id,
                target_app_dir=composed.target_app_dir,
                current_app_dir=composed.current_app_dir,
                prepared=self.remote_update_prepared,
                restore_source=self.remote_update_restore_source,
            ),
        )

    def _record_remote_update_prepared(self, retained: RetainedPreparation) -> bool:
        """Make a staging that just verified addressable by the next process.

        A write that does not land leaves the previous record intact and says
        so.  The staging really did verify out on the remote and this never
        contradicts that; it tells the gate the evidence is not durable, which
        holds the stop, backup, probe and activation.
        """

        previous = self.remote_update_prepared
        self.remote_update_prepared = PreparedCandidate(
            operation_id=retained.operation_id,
            artifact_digest=retained.artifact_digest,
            artifact_manifest_sha256=retained.artifact_manifest_sha256,
        )
        if self._persist_remote_update_session(self.remote_update_composed):
            return True
        self.remote_update_prepared = previous
        self._trace("remote update staging could not be recorded")
        return False

    def _record_remote_update_restore_source(self, source: RestoreSource) -> bool:
        """Bind the verified archive to this attempt, the moment it is known.

        A write that does not land leaves the previous record intact and the
        restore source unknown to the next process, which is the truthful
        answer: an unknown archive refuses a restore rather than guessing one.
        """

        previous = self.remote_update_restore_source
        self.remote_update_restore_source = source
        if self._persist_remote_update_session(self.remote_update_composed):
            return True
        self.remote_update_restore_source = previous
        self._trace("remote update restore source could not be recorded")
        return False

    def _forget_remote_update_session(self) -> None:
        clear_session(self.state_root)
        self.remote_update_prepared = None
        self.remote_update_restore_source = None

    def _verified_retire(
        self, candidate: RemoteUpdateCandidate
    ) -> Callable[[], bool] | None:
        """The hook a terminal ``update_ready`` calls, closed over this attempt.

        The identity is taken from the candidate the composition is *about*,
        before the flow exists, so what it compares against disk cannot drift.
        A stale hook, held by a flow this host already retired, finds a
        successor's record when it fires, and refuses.
        """

        fingerprint = self._remote_update_endpoint_fingerprint()
        attempt = candidate.update_attempt_id
        if not fingerprint or not attempt:
            return None

        def retire() -> bool:
            return self._retire_remote_update_session(fingerprint, attempt)

        return retire

    def _retire_remote_update_session(self, fingerprint: str, attempt: str) -> bool:
        """Retire exactly the attempt that finished, and nothing that outlived it.

        A finished attempt's target, attempt id, staging receipt and archive
        must not reach the next process: at the same desktop version it would
        present a fresh flow whose target is already the live application, and
        at a newer one the retained artifact would refuse.
        """

        retired = retire_session(
            self.state_root, endpoint_fingerprint=fingerprint, update_attempt_id=attempt
        )
        if not retired:
            self._trace("remote update record left in place: it is not this attempt")
            return False
        composed = self.remote_update_composed
        if composed is not None and composed.update_attempt_id == attempt:
            self.remote_update_prepared = None
            self.remote_update_restore_source = None
        return True

    # -- observations the host alone can make ------------------------------

    def _remote_update_observed_workspace(self) -> str | None:
        """Which workspace the runtime this start brought up actually serves."""

        try:
            metadata = self._read_remote_storage_metadata()
        except (RuntimeError, OSError, ValueError):
            return None
        observed = metadata.get("workspace_id")
        return observed if isinstance(observed, str) and observed else None

    def _remote_update_loopback(self) -> LoopbackSession:
        """The desktop's current loopback session, for the verification port."""

        from workstack import __version__ as desktop_version

        profile = getattr(self, "remote_profile", None)
        return LoopbackSession(
            base_url=str(getattr(self, "workstack_url", "") or "") or None,
            workspace_id=None if profile is None else profile.workspace_id,
            session_generation=str(self.remote_update_generation),
            desktop_version=desktop_version,
        )

    def _remote_update_owner(self) -> str:
        """The POSIX account the remote application runs as.

        Read from the alias's own evaluated OpenSSH configuration through the
        existing discovery helper, which resolves without connecting.  An alias
        that resolves to nothing simply means no capability.
        """

        from ssh_config_discovery import resolve_ssh_host

        profile = self._registry_profile()
        if profile is None:
            return ""
        try:
            return resolve_ssh_host(profile.ssh_host_alias).user
        except (RuntimeError, ValueError):
            return ""

    def _remote_update_ssh_executable(self) -> str:
        from ssot_connection import find_ssh_executable

        try:
            return find_ssh_executable()
        except Exception:  # noqa: BLE001 - an absent ssh is simply no capability
            return ""

    # -- restart hand-off --------------------------------------------------

    def _launch_remote_update_restart(
        self, spawn: Callable[..., object] = subprocess.Popen
    ) -> bool:
        """Relaunch the desktop, after the shutdown path has finished.

        Called from the same finally block that hands off a pending PC update,
        so the single instance is already released and the owned server is
        already stopped.  A launcher that is not there is reported, never
        worked around.
        """

        if not self.remote_update_restart_requested:
            return False
        self.remote_update_restart_requested = False
        launcher = Path(self.install_root) / DESKTOP_LAUNCHER
        if not launcher.is_file():
            self._trace("remote update restart skipped: the launcher is missing")
            return False
        try:
            spawn(
                self._remote_update_restart_command(launcher),
                cwd=str(self.install_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            self._trace(f"remote update restart failed to launch: {type(error).__name__}")
            return False
        return True

    def _remote_update_restart_command(self, launcher: Path) -> list[str]:
        """Relaunch *this* installation against *this* state, explicitly.

        The roots are supported arguments and this process may well have been
        started with them.  They are not derivable from the working directory:
        an omitted ``--state-root`` defaults to ``%LOCALAPPDATA%/WorkStack``, so
        a desktop running against any other one would come back to a different
        registry, journal, activation-binding store and workspace observation --
        and the pending activation it restarted for would be invisible there.
        Stating both keeps the restarted process the same host instance.
        """

        return [
            str(launcher),
            "--install-root",
            str(self.install_root),
            "--state-root",
            str(self.state_root),
        ]


__all__ = [
    "DESKTOP_LAUNCHER",
    "REFUSED_RECORD_AMBIGUOUS",
    "REFUSED_RECORD_NOT_DURABLE",
    "RemoteUpdateCandidate",
    "REMOTE_BACKUP_RELATIVE",
    "REMOTE_STATE_RELATIVE",
    "RemoteUpdateHostSurfaceMixin",
    "remote_maintenance_roots",
    "workspace_match",
]

"""The desktop half: capability, entry point, restart, and one selection.

``RemoteUpdateHostSurfaceMixin`` is exercised as the real host mixes it in --
the same class, the same methods, the same controller underneath -- against a
stand-in that supplies only what ``WorkStackDesktopHost`` supplies: a state
root, an install root, the selected registry profile and remote profile, a
settled session, and the WinForms handles.  Nothing in the mixin is replaced.

Two host answers are overridden in the stand-in and named here so the gap is
not hidden:

* ``_remote_update_owner`` resolves the alias's OpenSSH configuration with a
  real ``ssh -G``, which no test may depend on; the stand-in supplies the POSIX
  account directly.
* ``_read_remote_storage_metadata`` reads the live remote over the tunnel; the
  stand-in returns what such a read would have returned, or raises the way an
  unreachable remote does.
* ``_dispatch_remote_update_ui`` marshals onto the WinForms UI thread, which
  needs pythonnet; the stand-in records what would have been marshalled.

The WinForms and WebView2 half (``_create_remote_update_view``, the paint
dispatch, the clipboard) needs a live host and is not executed here; what *is*
executed is that every one of those paths refuses when no native view exists,
rather than pretending to have mounted.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import connection_registry as REGISTRY  # noqa: E402
import remote_update_host_factory as FACTORY  # noqa: E402
import remote_update_host_surface as SURFACE  # noqa: E402
import remote_update_host_surface_record as RECORD  # noqa: E402
from connection_registry_mutations import ConnectionRegistryMutationService  # noqa: E402
from remote_update_activation_adapter import RegistryActivationAdapter  # noqa: E402
from remote_update_activation_binding import (  # noqa: E402
    ActivationBinding,
    ActivationBindingStore,
)
from remote_update_backup_port import RestoreSource  # noqa: E402
from remote_update_controller import RemoteUpdateSelection  # noqa: E402
from remote_update_flow import RemoteUpdateFlow  # noqa: E402
from remote_update_flow_contract import PortRefusal, StartupEvidence  # noqa: E402
from remote_update_install_ports_inspection import LoopbackSession  # noqa: E402
from remote_update_journal import remote_update_journal_lock  # noqa: E402

from tests.test_remote_update_host_factory import (  # noqa: E402
    CURRENT_APP,
    DATA_DIR,
    OTHER_PROFILE_ID,
    PROFILE_ID,
    SERVED_UI_HTML,
    TARGET_APP,
    WORKSPACE,
    DRIVER_TESTS,
    install_root_with_bundle,
    make_artifact,
    registry_of,
    remote_profile,
    served_metadata,
    ssh_profile,
)
from tests.test_remote_update_host_journey import (  # noqa: E402
    REMOTE_BACKUPS,
    REMOTE_STATE,
    SESSION_TOKEN,
    RemoteHost,
)

#: The endpoint the whole fixture speaks for: the fixture registry profile and
#: the fixture runtime profile, digested the way production digests them.
FIXTURE_ENDPOINT = RECORD.endpoint_of(ssh_profile(), remote_profile()).fingerprint()
#: Any other well-formed endpoint digest; the value itself is never parsed.
FOREIGN_ENDPOINT = "sha256:" + "3e" * 32

from workstack import __version__ as BUNDLE_VERSION  # noqa: E402


class FakeSession:
    """What ``_settled_remote_session`` hands back: a token and nothing else."""

    def __init__(self, token: str = SESSION_TOKEN) -> None:
        self.token = token


class FakeForm:
    def __init__(self) -> None:
        self.IsDisposed = False
        self.closed = 0
        self.invoked: list[object] = []

    def Close(self) -> None:
        self.closed += 1

    def BeginInvoke(self, action) -> None:
        self.invoked.append(action)


class RecordingSpawn:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        if self.fail:
            raise OSError("the launcher could not be started")
        return object()


class SurfaceHost(SURFACE.RemoteUpdateHostSurfaceMixin):
    """Only what ``WorkStackDesktopHost`` gives the mixin, and nothing more."""

    def __init__(self, state_root: Path, install_root: Path) -> None:
        self.state_root = state_root
        self.install_root = install_root
        self.current_theme = "dark"
        self.form = None
        self.source_event_handlers: list[object] = []
        self.update_status: dict[str, object] = {"state": "current"}
        self.traces: list[str] = []
        self.pc_calls: list[str] = []
        self.artifact = make_artifact()
        self.remote = RemoteHost(self.artifact, f"{CURRENT_APP}-{BUNDLE_VERSION}")
        self.remote_profile = remote_profile()
        self.connection_registry_snapshot = REGISTRY.load_connection_registry(state_root)
        self.runtime_connection_profile_id = PROFILE_ID
        self.session: FakeSession | None = FakeSession()
        self.observed_workspace: str | None = WORKSPACE
        self.metadata_readable = True
        self.mutations = ConnectionRegistryMutationService(state_root, proof_ttl_seconds=60)
        self.adapter = RegistryActivationAdapter(state_root, mutation_service=self.mutations)
        self._init_remote_update_surface()

    # -- what the real host supplies -------------------------------------

    def _trace(self, message: str) -> None:
        self.traces.append(message)

    def _settled_remote_session(self):
        return self.session

    def _start_update_check(self, *, force_download: bool = False) -> None:
        self.pc_calls.append("download" if force_download else "check")

    def _install_downloaded_update(self) -> None:
        self.pc_calls.append("install")

    # -- the two named overrides ------------------------------------------

    def _remote_update_owner(self) -> str:
        return DRIVER_TESTS.OWNER

    def _read_remote_storage_metadata(self, *, timeout: float = 3.0):
        if not self.metadata_readable:
            raise RuntimeError("the remote did not answer")
        return {"workspace_id": self.observed_workspace}

    def _dispatch_remote_update_ui(self, action) -> bool:
        form = getattr(self, "form", None)
        if form is None or bool(getattr(form, "IsDisposed", False)):
            return False
        form.invoked.append(action)
        return True

    # -- the transport the composed flow is given --------------------------

    def _remote_update_inputs(self):
        inputs = super()._remote_update_inputs()
        if inputs is None:
            return None
        from dataclasses import replace

        return replace(
            inputs,
            process_factory=self.remote.process,
            stop_runner=self.remote.run,
            profile_probe=lambda _profile: served_metadata(),
            mutation_service=self.mutations,
            activation_adapter=self.adapter,
            observe_current=lambda: LoopbackSession(workspace_id=WORKSPACE),
        )


class SurfaceTestCase(unittest.TestCase):
    def host(self, *, bundle: bool = True) -> SurfaceHost:
        state = tempfile.TemporaryDirectory()
        install = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.addCleanup(install.cleanup)
        state_root = Path(state.name)
        install_root = Path(install.name)
        REGISTRY.save_connection_registry(
            state_root,
            registry_of(
                PROFILE_ID,
                ssh_profile(),
                ssh_profile(
                    profile_id=OTHER_PROFILE_ID,
                    label="Another remote",
                    ssh_host_alias="fixture-other",
                    remote_app_dir="/fixture/other/app",
                    remote_data_dir="/fixture/other/data",
                    expected_workspace_id="22222222-2222-4222-8222-222222222222",
                    preferred_forward_port=18766,
                ),
            ),
        )
        if bundle:
            install_root_with_bundle(install_root)
        host = SurfaceHost(state_root, install_root)
        self.addCleanup(host._close_remote_update_bridge)
        return host


class CapabilityTests(SurfaceTestCase):
    """The page is offered only when a real flow was actually constructed."""

    def test_a_composed_flow_is_the_capability(self) -> None:
        host = self.host()
        self.assertTrue(host.remote_update_capability())
        selection = host._remote_update_selection()
        self.assertIsInstance(selection, RemoteUpdateSelection)
        self.assertIsInstance(selection.flow, RemoteUpdateFlow)
        self.assertEqual(selection.workspace_id, WORKSPACE)

    def test_no_installed_bundle_is_no_capability(self) -> None:
        host = self.host(bundle=False)
        self.assertFalse(host.remote_update_capability())
        self.assertIsNone(host._remote_update_selection())

    def test_no_settled_session_is_no_capability(self) -> None:
        host = self.host()
        host.session = None
        self.assertFalse(host.remote_update_capability())

    def test_a_local_selection_is_no_capability(self) -> None:
        host = self.host()
        host.runtime_connection_profile_id = ""
        self.assertFalse(host.remote_update_capability())

    def test_the_candidate_is_a_sibling_of_the_current_application(self) -> None:
        host = self.host()
        composed = host._compose_remote_update()
        self.assertEqual(composed.target_app_dir, f"{CURRENT_APP}-{BUNDLE_VERSION}")
        self.assertNotEqual(composed.target_app_dir, CURRENT_APP)

    def test_the_page_offers_no_this_pc_action(self) -> None:
        host = self.host()
        self.assertEqual(host._remote_update_pc_actions(), ())
        self.assertEqual(host.pc_calls, [])


class SelectionTests(SurfaceTestCase):
    """One selection at a time; a profile change retires rather than repaints."""

    def test_the_same_selection_reuses_the_same_flow(self) -> None:
        host = self.host()
        first = host._compose_remote_update()
        self.assertIs(host._compose_remote_update(), first)
        self.assertEqual(host.remote_update_generation, 0)

    def test_a_profile_change_retires_the_page_and_the_flow(self) -> None:
        host = self.host()
        first = host._compose_remote_update()
        host.runtime_connection_profile_id = OTHER_PROFILE_ID
        host.remote_profile = remote_profile(
            workspace_id="22222222-2222-4222-8222-222222222222",
            remote_app_dir="/fixture/other/app",
            remote_data_dir="/fixture/other/data",
            ssh_host_alias="fixture-other",
        )
        second = host._compose_remote_update()
        self.assertIsNotNone(second)
        self.assertIsNot(second, first)
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertNotEqual(second.workspace_id, first.workspace_id)
        self.assertEqual(host.remote_update_generation, 1)

    def test_a_profile_change_to_a_local_store_leaves_no_flow_behind(self) -> None:
        host = self.host()
        host._compose_remote_update()
        host.runtime_connection_profile_id = ""
        self.assertIsNone(host._compose_remote_update())
        self.assertIsNone(host.remote_update_composed)
        self.assertEqual(host.remote_update_generation, 1)


class EntryPointTests(SurfaceTestCase):
    """The frontend's request mounts a real view or refuses to mount at all."""

    def test_the_page_refuses_without_a_native_view(self) -> None:
        host = self.host()
        self.assertTrue(host.remote_update_capability())
        self.assertFalse(host._handle_remote_update_open())

    def test_the_page_refuses_without_a_capability(self) -> None:
        host = self.host(bundle=False)
        self.assertFalse(host._handle_remote_update_open())
        self.assertIn("remote update page refused: no composed flow", host.traces)

    def test_mounting_refuses_when_there_is_no_window(self) -> None:
        host = self.host()
        self.assertFalse(host._mount_remote_update_view())
        host.form = FakeForm()
        host.form.IsDisposed = True
        self.assertFalse(host._mount_remote_update_view())

    def test_navigation_is_refused_while_nothing_is_rendered(self) -> None:
        host = self.host()
        self.assertFalse(host._remote_update_admits_navigation("https://example.invalid/"))
        self.assertFalse(host._remote_update_admits_navigation("about:blank"))

    def test_a_foreign_web_message_is_not_this_page(self) -> None:
        host = self.host()
        self.assertFalse(host._handle_remote_update_message("workstack-update-host|check"))


class RestartTests(SurfaceTestCase):
    """A restart is begun by closing; it completes nothing on its own."""

    def test_no_window_means_no_restart(self) -> None:
        host = self.host()
        self.assertFalse(host._remote_update_restart())
        self.assertFalse(host.remote_update_restart_requested)

    def test_the_restart_closes_the_window_and_confirms_nothing(self) -> None:
        host = self.host()
        composed = host._compose_remote_update()
        host.form = FakeForm()
        self.assertTrue(host._remote_update_restart())
        self.assertTrue(host.remote_update_restart_requested)
        self.assertEqual(len(host.form.invoked), 1)
        self.assertEqual(composed.flow.snapshot().stage, "idle")
        self.assertIsNone(composed.flow.retained_activation())

    def test_the_relaunch_only_happens_when_one_was_asked_for(self) -> None:
        host = self.host()
        spawn = RecordingSpawn()
        self.assertFalse(host._launch_remote_update_restart(spawn))
        self.assertEqual(spawn.calls, [])

    def test_the_relaunch_keeps_this_installation_and_this_state(self) -> None:
        """A non-default state root is a supported identity, not a default.

        Relaunching bare would send the desktop to %LOCALAPPDATA%/WorkStack,
        which holds a different registry, journal, binding store and workspace
        observation -- and the pending activation would be invisible there.
        """

        host = self.host()
        launcher = host.install_root / SURFACE.DESKTOP_LAUNCHER
        launcher.write_bytes(b"MZ")
        host.remote_update_restart_requested = True
        spawn = RecordingSpawn()
        self.assertTrue(host._launch_remote_update_restart(spawn))
        self.assertEqual(
            spawn.calls,
            [
                [
                    str(launcher),
                    "--install-root",
                    str(host.install_root),
                    "--state-root",
                    str(host.state_root),
                ]
            ],
        )
        self.assertNotEqual(str(host.state_root), str(Path.home()))
        self.assertFalse(host.remote_update_restart_requested)

    def test_an_absent_launcher_is_reported_rather_than_worked_around(self) -> None:
        host = self.host()
        host.remote_update_restart_requested = True
        spawn = RecordingSpawn()
        self.assertFalse(host._launch_remote_update_restart(spawn))
        self.assertEqual(spawn.calls, [])
        self.assertIn(
            "remote update restart skipped: the launcher is missing", host.traces
        )


class StartupEvidenceTests(SurfaceTestCase):
    """What a start observed, and never more than that."""

    def activated(self, host: SurfaceHost):
        composed = host._compose_remote_update()
        flow = composed.flow
        for _ in range(6):
            snapshot = flow.advance()
            if snapshot.code == "activate_pending_restart":
                return composed
        raise AssertionError(f"never activated; ended at {flow.snapshot().code}")

    def test_no_activation_means_no_evidence(self) -> None:
        host = self.host()
        host._compose_remote_update()
        self.assertIsNone(host._remote_update_startup_evidence())

    def test_the_evidence_names_the_operation_the_registry_selects(self) -> None:
        host = self.host()
        composed = self.activated(host)
        retained = composed.flow.retained_activation()
        evidence = host._remote_update_startup_evidence()
        self.assertIsInstance(evidence, StartupEvidence)
        self.assertEqual(evidence.selected_activation_id, retained.operation_id)
        self.assertEqual(evidence.authority_selected, "verified")
        self.assertEqual(evidence.workspace_match, "verified")

    def test_an_unreachable_remote_leaves_the_workspace_unknown(self) -> None:
        host = self.host()
        self.activated(host)
        host.metadata_readable = False
        evidence = host._remote_update_startup_evidence()
        self.assertEqual(evidence.workspace_match, "unknown")

    def test_another_workspace_is_a_failure_and_not_an_unknown(self) -> None:
        host = self.host()
        self.activated(host)
        host.observed_workspace = "22222222-2222-4222-8222-222222222222"
        evidence = host._remote_update_startup_evidence()
        self.assertEqual(evidence.workspace_match, "failed")

    def test_the_three_workspace_answers_stay_apart(self) -> None:
        self.assertEqual(SURFACE.workspace_match(None, WORKSPACE), "unknown")
        self.assertEqual(SURFACE.workspace_match(WORKSPACE, WORKSPACE), "verified")
        self.assertEqual(SURFACE.workspace_match("other", WORKSPACE), "failed")


class SessionRecordTests(SurfaceTestCase):
    """The attempt identity and the verified archive survive the process."""

    def test_the_attempt_is_recorded_when_the_flow_is_composed(self) -> None:
        host = self.host()
        composed = host._compose_remote_update()
        recorded = RECORD.read_session(host.state_root)
        self.assertIsNotNone(recorded)
        self.assertEqual(recorded.update_attempt_id, composed.update_attempt_id)
        self.assertEqual(recorded.workspace_id, WORKSPACE)
        self.assertEqual(recorded.target_app_dir, composed.target_app_dir)

    def test_a_second_process_resumes_the_same_attempt(self) -> None:
        host = self.host()
        first = host._compose_remote_update()
        successor = SurfaceHost(host.state_root, host.install_root)
        self.addCleanup(successor._close_remote_update_bridge)
        second = successor._compose_remote_update()
        self.assertEqual(second.update_attempt_id, first.update_attempt_id)
        self.assertNotEqual(second.session_id, first.session_id)

    def test_a_record_for_another_profile_is_not_adopted(self) -> None:
        host = self.host()
        RECORD.write_session(
            host.state_root,
            RECORD.RemoteUpdateSession(
                workspace_id=WORKSPACE,
                profile_id=OTHER_PROFILE_ID,
                endpoint_fingerprint=FOREIGN_ENDPOINT,
                update_attempt_id="foreign-attempt",
                target_app_dir=TARGET_APP,
                current_app_dir=CURRENT_APP,
            ),
        )
        composed = host._compose_remote_update()
        self.assertNotEqual(composed.update_attempt_id, "foreign-attempt")

    def test_an_unreadable_record_refuses_rather_than_overwriting_it(self) -> None:
        """Present-but-unadmitted is not absent, and must not start an attempt.

        An attempt may have been begun and its evidence lost.  Minting a fresh
        one over it would let a second mutation be issued while an external
        effect from the first was still standing.
        """

        host = self.host()
        path = RECORD.session_path(host.state_root)
        path.write_bytes(b"{ not json")
        self.assertIsNone(host._compose_remote_update())
        self.assertFalse(host.remote_update_capability())
        self.assertEqual(host.remote_update_refusal, SURFACE.REFUSED_RECORD_AMBIGUOUS)
        # The bytes are exactly as they were: nothing repaired, nothing rewrote.
        self.assertEqual(path.read_bytes(), b"{ not json")

    def test_the_three_answers_of_a_record_read_stay_apart(self) -> None:
        host = self.host()
        path = RECORD.session_path(host.state_root)
        self.assertEqual(RECORD.load_session(host.state_root).state, RECORD.STATE_ABSENT)
        host._compose_remote_update()
        self.assertEqual(RECORD.load_session(host.state_root).state, RECORD.STATE_PRESENT)
        path.unlink()
        path.mkdir()
        self.assertEqual(
            RECORD.load_session(host.state_root).state, RECORD.STATE_AMBIGUOUS
        )

    def test_an_oversized_record_is_ambiguous_and_left_alone(self) -> None:
        host = self.host()
        path = RECORD.session_path(host.state_root)
        payload = b"x" * (RECORD.MAX_SESSION_BYTES + 1)
        path.write_bytes(payload)
        self.assertTrue(RECORD.load_session(host.state_root).ambiguous)
        self.assertIsNone(host._compose_remote_update())
        self.assertEqual(path.read_bytes(), payload)

    def test_an_attempt_that_cannot_be_recorded_is_not_a_capability(self) -> None:
        """Durable preparation is the gate, not best-effort telemetry.

        A flow published over a state root it cannot write could still stop the
        owner, take the backup and move the registry, and then have no attempt
        identity for the next process to address.
        """

        host = self.host()
        original = SURFACE.write_session
        SURFACE.write_session = lambda *_args, **_kwargs: False
        self.addCleanup(setattr, SURFACE, "write_session", original)
        self.assertIsNone(host._compose_remote_update())
        self.assertIsNone(host.remote_update_composed)
        self.assertFalse(host.remote_update_capability())
        self.assertEqual(host.remote_update_refusal, SURFACE.REFUSED_RECORD_NOT_DURABLE)

    def test_the_verified_archive_identity_round_trips(self) -> None:
        source = RestoreSource(operation_id="backup-op", backup_digest="sha256:" + "7f" * 32)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        session = RECORD.RemoteUpdateSession(
            workspace_id=WORKSPACE,
            profile_id=PROFILE_ID,
            endpoint_fingerprint=FIXTURE_ENDPOINT,
            update_attempt_id="attempt-1",
            target_app_dir=TARGET_APP,
            current_app_dir=CURRENT_APP,
            restore_source=source,
        )
        self.assertTrue(RECORD.write_session(root, session))
        self.assertEqual(RECORD.read_session(root), session)

    def test_a_digest_that_is_not_the_adapters_spelling_is_no_record(self) -> None:
        document = RECORD.RemoteUpdateSession(
            workspace_id=WORKSPACE,
            profile_id=PROFILE_ID,
            endpoint_fingerprint=FIXTURE_ENDPOINT,
            update_attempt_id="attempt-1",
            target_app_dir=TARGET_APP,
            current_app_dir=CURRENT_APP,
        ).document()
        document["restore_source"] = {"operation_id": "op", "backup_digest": "f" * 64}
        self.assertIsNone(RECORD.admit_session(document))

    def test_an_absent_record_is_simply_absent(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assertIsNone(RECORD.read_session(Path(directory.name)))

    def test_a_record_with_extra_keys_is_refused(self) -> None:
        document = RECORD.RemoteUpdateSession(
            workspace_id=WORKSPACE,
            profile_id=PROFILE_ID,
            endpoint_fingerprint=FIXTURE_ENDPOINT,
            update_attempt_id="attempt-1",
            target_app_dir=TARGET_APP,
            current_app_dir=CURRENT_APP,
        ).document()
        document["extra"] = 1
        self.assertIsNone(RECORD.admit_session(document))

    def test_forgetting_a_finished_attempt_removes_the_record(self) -> None:
        host = self.host()
        host._compose_remote_update()
        host._forget_remote_update_session()
        self.assertIsNone(RECORD.read_session(host.state_root))


class TerminalRetirementTests(SurfaceTestCase):
    """A finished attempt retires its own record, and only its own."""

    def composed_with_hook(self, host: SurfaceHost):
        """The composition, and the completion hook the factory was handed."""

        captured: list[object] = []
        original = SURFACE.build_remote_update_flow

        def capture(inputs):
            captured.append(inputs.on_verified)
            return original(inputs)

        SURFACE.build_remote_update_flow = capture
        self.addCleanup(setattr, SURFACE, "build_remote_update_flow", original)
        composed = host._compose_remote_update()
        SURFACE.build_remote_update_flow = original
        return composed, captured[-1]

    def test_the_finished_attempt_is_the_one_that_is_retired(self) -> None:
        host = self.host()
        composed, finished = self.composed_with_hook(host)
        self.assertEqual(
            RECORD.read_session(host.state_root).update_attempt_id,
            composed.update_attempt_id,
        )
        self.assertTrue(finished())
        self.assertEqual(
            RECORD.load_session(host.state_root).state, RECORD.STATE_ABSENT
        )
        self.assertIsNone(host.remote_update_prepared)
        self.assertIsNone(host.remote_update_restore_source)

    def test_a_stale_hook_cannot_retire_a_successors_record(self) -> None:
        """The exact counterexample: an already-superseded flow finishing late.

        The hook is closed over the attempt its own composition was about.  By
        the time it fires, a later attempt has written its own record, and that
        record is what the next process needs; the comparison refuses rather
        than unlinking it.
        """

        host = self.host()
        _composed, stale = self.composed_with_hook(host)
        successor = RECORD.RemoteUpdateSession(
            workspace_id=WORKSPACE,
            profile_id=PROFILE_ID,
            endpoint_fingerprint=FIXTURE_ENDPOINT,
            update_attempt_id="a-newer-attempt",
            target_app_dir=TARGET_APP,
            current_app_dir=CURRENT_APP,
        )
        self.assertTrue(RECORD.write_session(host.state_root, successor))
        self.assertFalse(stale())
        self.assertEqual(RECORD.read_session(host.state_root), successor)
        self.assertIn(
            "remote update record left in place: it is not this attempt", host.traces
        )

    def test_a_record_from_another_endpoint_is_never_retired(self) -> None:
        host = self.host()
        composed, finished = self.composed_with_hook(host)
        foreign = RECORD.RemoteUpdateSession(
            workspace_id=WORKSPACE,
            profile_id=PROFILE_ID,
            endpoint_fingerprint=FOREIGN_ENDPOINT,
            update_attempt_id=composed.update_attempt_id,
            target_app_dir=TARGET_APP,
            current_app_dir=CURRENT_APP,
        )
        self.assertTrue(RECORD.write_session(host.state_root, foreign))
        self.assertFalse(finished())
        self.assertEqual(RECORD.read_session(host.state_root), foreign)

    def test_a_retired_record_leaves_no_target_or_attempt_to_readopt(self) -> None:
        """After a second successful restart, the next update starts fresh.

        Nothing of the finished attempt may be handed to the process that comes
        after it: not the old target, not the old attempt id, not the staging
        receipt and not the archive.  With the registry now selecting the
        candidate, re-deriving is exactly right -- there is no attempt in
        flight to resume.
        """

        host = self.host()
        composed, finished = self.composed_with_hook(host)
        for _ in range(6):
            if composed.flow.advance().code == "activate_pending_restart":
                break
        live = REGISTRY.load_connection_registry(host.state_root)
        selected = next(
            profile for profile in live.profiles if profile.profile_id == PROFILE_ID
        )
        self.assertEqual(selected.remote_app_dir, composed.target_app_dir)
        self.assertTrue(finished())

        # The next process, over the same disk and the moved registry.
        successor = SurfaceHost(host.state_root, host.install_root)
        self.addCleanup(successor._close_remote_update_bridge)
        successor.remote_profile = remote_profile(remote_app_dir=selected.remote_app_dir)
        successor.connection_registry_snapshot = live
        candidate = successor._remote_update_candidate(selected, successor.artifact)
        self.assertNotEqual(candidate.update_attempt_id, composed.update_attempt_id)
        self.assertNotEqual(candidate.target_app_dir, composed.target_app_dir)
        self.assertEqual(candidate.current_app_dir, selected.remote_app_dir)
        self.assertIsNone(candidate.prepared)
        self.assertIsNone(candidate.restore_source)


class EndpointBindingTests(SurfaceTestCase):
    """Durable evidence belongs to the endpoint that produced it, not to an id."""

    def repoint(self, host: SurfaceHost, **overrides: object):
        """Edit the selected profile in place, keeping its id and workspace."""

        edited = ssh_profile(**overrides)
        REGISTRY.save_connection_registry(host.state_root, registry_of(PROFILE_ID, edited))
        host.connection_registry_snapshot = REGISTRY.load_connection_registry(
            host.state_root
        )
        return edited

    def test_a_same_profile_endpoint_change_adopts_no_prior_evidence(self) -> None:
        """The review's third counterexample, against the real record.

        The same profile id is re-pointed at another host and another data
        root.  Everything the previous endpoint produced -- the attempt, the
        staging it verified, the archive a restore would put back -- lives on
        the machine that is no longer selected, and none of it may be adopted
        into a flow that speaks to the new one.
        """

        host = self.host()
        first = host._compose_remote_update()
        host.remote_update_prepared = RECORD.PreparedCandidate(
            operation_id="staged-on-host-a",
            artifact_digest=FILLER_DIGEST,
            artifact_manifest_sha256=FILLER_DIGEST,
        )
        host.remote_update_restore_source = RestoreSource(
            operation_id="backup-on-host-a", backup_digest=FILLER_DIGEST
        )
        self.assertTrue(host._persist_remote_update_session(first))

        self.repoint(host, ssh_host_alias="fixture-host-b", remote_data_dir="/srv/data-b")
        host.remote_profile = remote_profile(remote_data_dir="/srv/data-b")
        second = host._compose_remote_update()
        self.assertIsNotNone(second)
        self.assertNotEqual(second.update_attempt_id, first.update_attempt_id)
        self.assertIsNone(second.backup.restore_source)
        recorded = RECORD.read_session(host.state_root)
        self.assertEqual(recorded.update_attempt_id, second.update_attempt_id)
        self.assertIsNone(recorded.prepared)
        self.assertIsNone(recorded.restore_source)

    def test_a_reallocated_local_forward_keeps_the_endpoints_evidence(self) -> None:
        """The tunnel is rebuilt on every start; the endpoint is not.

        The desktop's local forward port is reallocated whenever the tunnel
        comes back, so folding it into the durable binding would make the same
        machine and the same store stop recognising the attempt it is in the
        middle of.  It belongs to this process's view of the endpoint, which
        does recompose, and to nothing on disk.
        """

        host = self.host()
        first = host._compose_remote_update()
        binding = host.remote_update_selection_binding
        host.remote_profile = remote_profile(local_forward_port=18999)
        second = host._compose_remote_update()
        self.assertIsNot(second, first)
        self.assertNotEqual(host.remote_update_selection_binding, binding)
        self.assertEqual(second.update_attempt_id, first.update_attempt_id)
        self.assertEqual(second.target_app_dir, first.target_app_dir)
        self.assertEqual(
            RECORD.read_session(host.state_root).endpoint_fingerprint,
            RECORD.endpoint_of(ssh_profile(), remote_profile()).fingerprint(),
        )

    def test_the_remote_port_is_part_of_the_durable_endpoint(self) -> None:
        """The port the alias actually reaches is what the record is bound to."""

        host = self.host()
        first = host._compose_remote_update()
        moved = replace(remote_profile(), remote_port=9876)
        host.remote_profile = moved
        second = host._compose_remote_update()
        self.assertIsNotNone(second)
        self.assertNotEqual(second.update_attempt_id, first.update_attempt_id)
        self.assertNotEqual(
            RECORD.endpoint_of(ssh_profile(), moved).fingerprint(), FIXTURE_ENDPOINT
        )

    def test_the_remote_interpreter_is_part_of_the_selection(self) -> None:
        host = self.host()
        first = host._compose_remote_update()
        host.remote_profile = remote_profile(remote_python="/usr/bin/python3.13")
        second = host._compose_remote_update()
        self.assertIsNotNone(second)
        self.assertNotEqual(second.update_attempt_id, first.update_attempt_id)

    def test_a_rotated_token_keeps_the_endpoints_own_evidence(self) -> None:
        """Rotation recomposes; it does not discard what the endpoint produced.

        The token and the settled generation are not the endpoint.  A reconnect
        that rotates them is still speaking to the same machine and the same
        store, so the attempt it is in the middle of is still its own.
        """

        host = self.host()
        first = host._compose_remote_update()
        host.session = FakeSession(token="a-freshly-rotated-session-token")
        second = host._compose_remote_update()
        self.assertIsNot(second, first)
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertEqual(second.update_attempt_id, first.update_attempt_id)
        self.assertEqual(second.target_app_dir, first.target_app_dir)

    def test_the_two_bindings_share_one_fingerprint(self) -> None:
        host = self.host()
        composed = host._compose_remote_update()
        fingerprint = host._remote_update_endpoint_fingerprint()
        self.assertEqual(
            RECORD.read_session(host.state_root).endpoint_fingerprint, fingerprint
        )
        self.assertEqual(fingerprint, FIXTURE_ENDPOINT)
        self.assertIsNotNone(composed)
        # A digest, not the fields themselves: no host or path is published.
        self.assertNotIn(DATA_DIR, fingerprint)


class HookedOs:
    """The ``os`` the record module sees, with one call given a side effect.

    The effect fires once, right after the named call returns, so a replacement
    can be dropped at an exact point in the read: after the pre-open ``lstat``,
    which is the window the review named, or after the handle is closed, which
    is the window an open handle can never see by itself.
    """

    def __init__(self, real, name: str, effect, after: int = 1) -> None:
        self._real = real
        self._name = name
        self._effect = effect
        self._after = after
        self.calls = 0
        self.fired = 0

    def __getattr__(self, name: str):
        attribute = getattr(self._real, name)
        if name != self._name:
            return attribute

        def hooked(*args: object, **kwargs: object):
            answer = attribute(*args, **kwargs)
            self.calls += 1
            if not self.fired and self.calls >= self._after:
                self.fired += 1
                self._effect()
            return answer

        return hooked


class ReplacedRecordTests(SurfaceTestCase):
    """A record swapped under the read is ambiguous, and is left where it is."""

    def successor_bytes(self) -> bytes:
        """A perfectly valid record, written by somebody else since."""

        return json.dumps(
            RECORD.RemoteUpdateSession(
                workspace_id=WORKSPACE,
                profile_id=PROFILE_ID,
                endpoint_fingerprint=FIXTURE_ENDPOINT,
                update_attempt_id="a-record-written-since",
                target_app_dir=TARGET_APP,
                current_app_dir=CURRENT_APP,
            ).document(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    def replace_at(self, host: SurfaceHost, call: str, after: int = 1) -> bytes:
        """Arrange for the record path to be replaced after the nth ``call``."""

        path = RECORD.session_path(host.state_root)
        payload = self.successor_bytes()
        other = path.with_suffix(".written-since")
        other.write_bytes(payload)
        original = RECORD.os
        RECORD.os = HookedOs(original, call, lambda: os.replace(other, path), after)
        self.addCleanup(setattr, RECORD, "os", original)
        return payload

    def test_a_path_replaced_before_the_open_is_ambiguous(self) -> None:
        """The counterexample the review named, made to actually happen.

        The path was stat'ed, and by the time it is opened it names another
        file.  Reading the path a second time -- which is what ``read_bytes``
        does -- would admit that other file as this attempt's own record.
        """

        host = self.host()
        self.assertIsNotNone(host._compose_remote_update())
        payload = self.replace_at(host, "lstat")
        self.assertEqual(
            RECORD.load_session(host.state_root).state, RECORD.STATE_AMBIGUOUS
        )
        # Nothing was repaired, rewritten or removed.
        self.assertEqual(RECORD.session_path(host.state_root).read_bytes(), payload)

    def test_a_path_replaced_after_the_handle_is_closed_is_ambiguous(self) -> None:
        """The window an open handle cannot see by itself.

        ``os.replace`` onto the name leaves the handle pointing at the old,
        still perfectly readable inode, so both ``fstat`` calls agree and the
        bytes read are intact.  Only a fresh lookup of the path says that the
        record admitted is no longer the record there.
        """

        host = self.host()
        self.assertIsNotNone(host._compose_remote_update())
        payload = self.replace_at(host, "close")
        self.assertEqual(
            RECORD.load_session(host.state_root).state, RECORD.STATE_AMBIGUOUS
        )
        self.assertEqual(RECORD.session_path(host.state_root).read_bytes(), payload)

    def test_a_replacement_refuses_rather_than_starting_a_fresh_attempt(self) -> None:
        host = self.host()
        host._compose_remote_update()
        host._retire_remote_update_surface()
        payload = self.replace_at(host, "lstat")
        self.assertIsNone(host._compose_remote_update())
        self.assertEqual(host.remote_update_refusal, SURFACE.REFUSED_RECORD_AMBIGUOUS)
        self.assertEqual(RECORD.session_path(host.state_root).read_bytes(), payload)

    def test_a_symlinked_record_is_ambiguous_where_links_can_be_made(self) -> None:
        host = self.host()
        host._compose_remote_update()
        path = RECORD.session_path(host.state_root)
        target = path.with_suffix(".target")
        target.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks unavailable here: {type(error).__name__}")
        self.assertEqual(
            RECORD.load_session(host.state_root).state, RECORD.STATE_AMBIGUOUS
        )

    def test_a_replacement_after_the_admission_refuses_the_unlink(self) -> None:
        """The window between admitting the record and removing it.

        The retire admits the record, then re-stats the path to prove it still
        names that same file.  A writer that ignored the gate entirely and
        replaced the path in between is caught there, so the record it wrote --
        which the next process may well be depending on -- is not unlinked.

        The replacement lands after the read's own final lookup and before the
        retire's, which is the only place a foreign writer could reach.
        """

        host = self.host()
        composed = host._compose_remote_update()
        payload = self.replace_at(host, "lstat", after=2)
        self.assertFalse(
            RECORD.retire_session(
                host.state_root,
                endpoint_fingerprint=host._remote_update_endpoint_fingerprint(),
                update_attempt_id=composed.update_attempt_id,
            )
        )
        self.assertEqual(RECORD.session_path(host.state_root).read_bytes(), payload)


class RecordGateTests(SurfaceTestCase):
    """Replacing the record and retiring it are one exclusion, not two."""

    def test_neither_a_write_nor_a_retire_lands_while_the_gate_is_held(self) -> None:
        """The counterexample a re-stat alone cannot close.

        An ordinary successor write is a plain ``os.replace`` onto the name,
        and nothing about re-stating the path stops one from landing between
        the retire's proof and its unlink.  What stops it is that both take the
        same gate: while it is held, neither the write nor the retire can even
        begin, and each answers ``False`` at its bound rather than waiting or
        acting.
        """

        host = self.host()
        composed = host._compose_remote_update()
        before = RECORD.session_path(host.state_root).read_bytes()
        successor = RECORD.RemoteUpdateSession(
            workspace_id=WORKSPACE,
            profile_id=PROFILE_ID,
            endpoint_fingerprint=FIXTURE_ENDPOINT,
            update_attempt_id="a-successor-attempt",
            target_app_dir=TARGET_APP,
            current_app_dir=CURRENT_APP,
        )
        with remote_update_journal_lock(host.state_root):
            self.assertFalse(RECORD.write_session(host.state_root, successor))
            self.assertFalse(
                RECORD.retire_session(
                    host.state_root,
                    endpoint_fingerprint=host._remote_update_endpoint_fingerprint(),
                    update_attempt_id=composed.update_attempt_id,
                )
            )
        # Neither of them touched the record they could not take the gate for.
        self.assertEqual(RECORD.session_path(host.state_root).read_bytes(), before)

    def test_the_gate_is_released_again_for_the_next_change(self) -> None:
        host = self.host()
        composed = host._compose_remote_update()
        with remote_update_journal_lock(host.state_root):
            pass
        self.assertTrue(
            RECORD.retire_session(
                host.state_root,
                endpoint_fingerprint=host._remote_update_endpoint_fingerprint(),
                update_attempt_id=composed.update_attempt_id,
            )
        )
        self.assertEqual(
            RECORD.load_session(host.state_root).state, RECORD.STATE_ABSENT
        )

    def test_a_successor_already_on_disk_is_never_retired(self) -> None:
        host = self.host()
        composed = host._compose_remote_update()
        successor = RECORD.RemoteUpdateSession(
            workspace_id=WORKSPACE,
            profile_id=PROFILE_ID,
            endpoint_fingerprint=FIXTURE_ENDPOINT,
            update_attempt_id="a-successor-attempt",
            target_app_dir=TARGET_APP,
            current_app_dir=CURRENT_APP,
        )
        self.assertTrue(RECORD.write_session(host.state_root, successor))
        self.assertFalse(
            RECORD.retire_session(
                host.state_root,
                endpoint_fingerprint=host._remote_update_endpoint_fingerprint(),
                update_attempt_id=composed.update_attempt_id,
            )
        )
        self.assertEqual(RECORD.read_session(host.state_root), successor)


OLDER_OPERATION = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
FILLER_DIGEST = "sha256:" + "5c" * 32


class RecoveryLane(SurfaceTestCase):
    """Helpers for driving one attempt and then the process that follows it."""

    def activated(self, host: SurfaceHost):
        composed = host._compose_remote_update()
        for _ in range(6):
            if composed.flow.advance().code == "activate_pending_restart":
                return composed
        raise AssertionError(f"never activated; at {composed.flow.snapshot().code}")

    def live_app_dir(self, host: SurfaceHost) -> str:
        registry = REGISTRY.load_connection_registry(host.state_root)
        return next(
            profile.remote_app_dir
            for profile in registry.profiles
            if profile.profile_id == PROFILE_ID
        )

    def successor(self, host: SurfaceHost) -> SurfaceHost:
        """The next desktop process, over the same disk and the same Linux host."""

        successor = SurfaceHost(host.state_root, host.install_root)
        self.addCleanup(successor._close_remote_update_bridge)
        successor.remote.apps = dict(host.remote.apps)
        successor.remote.backups = dict(host.remote.backups)
        successor.remote.owner_running = host.remote.owner_running
        successor.remote_profile = remote_profile(remote_app_dir=self.live_app_dir(host))
        return successor


class RecoveryTests(RecoveryLane):
    """What the process after the activation restart actually recomposes."""

    def test_the_activated_registry_does_not_become_a_second_candidate(self) -> None:
        """The counterexample the review named, against the real registry.

        The first process derived the candidate from the live application and
        activated it, so the selected profile now IS the candidate.  Deriving
        again from that live value would name a doubly suffixed directory that
        nobody staged, and would strand the journal under a new attempt id.
        """

        host = self.host()
        first = self.activated(host)
        self.assertEqual(self.live_app_dir(host), first.target_app_dir)

        second = self.successor(host)
        composed = second._compose_remote_update()
        self.assertIsNotNone(composed)
        self.assertEqual(composed.target_app_dir, first.target_app_dir)
        self.assertEqual(composed.current_app_dir, CURRENT_APP)
        self.assertEqual(composed.update_attempt_id, first.update_attempt_id)
        doubled = f"{CURRENT_APP}-{BUNDLE_VERSION}-{BUNDLE_VERSION}"
        self.assertNotEqual(composed.target_app_dir, doubled)
        self.assertTrue(second.remote_update_capability())

    def test_the_next_process_recovers_the_pending_activation(self) -> None:
        host = self.host()
        first = self.activated(host)
        retained = first.flow.retained_activation()
        second = self.successor(host)
        composed = second._compose_remote_update()
        self.assertEqual(composed.flow.retained_activation(), retained)
        evidence = second._remote_update_startup_evidence()
        self.assertEqual(evidence.selected_activation_id, retained.operation_id)
        self.assertEqual(evidence.authority_selected, "verified")
        self.assertEqual(
            composed.flow.resume_after_restart(evidence).code, "activate_committed"
        )

    def test_the_read_only_stage_is_admitted_by_a_reverified_receipt(self) -> None:
        """After the restart, VERIFY reads the candidate back through recovery.

        Nothing fabricates a request record: the install port re-runs the
        verified-unpack verify exchange for the exact retained operation, the
        admitted artifact and the canonical target.
        """

        host = self.host()
        self.activated(host)
        recorded = RECORD.read_session(host.state_root)
        self.assertIsNotNone(recorded.prepared)
        second = self.successor(host)
        composed = second._compose_remote_update()
        evidence = second._remote_update_startup_evidence()
        composed.flow.resume_after_restart(evidence)
        before = len(second.remote.unpack_operations)
        composed.flow.advance()
        record = composed.install._record  # noqa: SLF001 - the record is the subject
        self.assertIsNotNone(record)
        self.assertEqual(record.operation_id, recorded.prepared.operation_id)
        self.assertTrue(record.issued)
        self.assertEqual(record.prepare.status, "verified")
        self.assertIn("verify", second.remote.unpack_operations[before:])
        self.assertNotIn("place", second.remote.unpack_operations[before:])

    def test_a_candidate_that_no_longer_reverifies_refuses_the_read(self) -> None:
        host = self.host()
        self.activated(host)
        second = self.successor(host)
        second.remote.unpack_identity_broken = True
        composed = second._compose_remote_update()
        composed.flow.resume_after_restart(second._remote_update_startup_evidence())
        composed.flow.advance()
        self.assertIsNone(composed.install._record)  # noqa: SLF001 - nothing made up

    def test_a_record_with_no_staging_never_admits_a_read_only_stage(self) -> None:
        host = self.host()
        composed = host._compose_remote_update()
        self.assertIsNone(RECORD.read_session(host.state_root).prepared)
        with self.assertRaises(PortRefusal) as caught:
            composed.prepare.verify("a-fresh-verify-identity")
        self.assertEqual(caught.exception.code, FACTORY.REFUSED_NOT_STAGED)


class ActivationEvidenceBindingTests(RecoveryLane):
    """Startup evidence is about THIS flow's activation and no other."""

    def test_an_older_binding_is_not_this_attempts_startup_evidence(self) -> None:
        host = self.host()
        ActivationBindingStore(host.state_root).bind(
            ActivationBinding(
                operation_id=OLDER_OPERATION,
                profile_id=PROFILE_ID,
                profile_digest=FILLER_DIGEST,
                previous_registry_digest=FILLER_DIGEST,
                activated_registry_digest=FILLER_DIGEST,
                proof_digest=FILLER_DIGEST,
            )
        )
        composed = self.activated(host)
        retained = composed.flow.retained_activation()
        bindings = ActivationBindingStore(host.state_root).load()
        self.assertEqual(bindings[0].operation_id, OLDER_OPERATION)
        self.assertNotEqual(retained.operation_id, OLDER_OPERATION)
        evidence = host._remote_update_startup_evidence()
        self.assertEqual(evidence.selected_activation_id, retained.operation_id)
        self.assertEqual(evidence.authority_selected, "verified")

    def test_an_unbound_retained_activation_stays_unknown(self) -> None:
        host = self.host()
        composed = self.activated(host)
        retained = composed.flow.retained_activation()
        ActivationBindingStore(host.state_root).release(retained.operation_id)
        self.assertIsNone(host._remote_update_startup_evidence())


class RestoreSourceRecordTests(RecoveryLane):
    """The verified archive is bound to this attempt the moment it is known."""

    def advance_to_backup(self, host: SurfaceHost):
        composed = host._compose_remote_update()
        for _ in range(6):
            if composed.flow.advance().code == "backup_verified":
                return composed
        raise AssertionError("never backed up")

    def test_the_verified_archive_is_recorded_after_the_backup(self) -> None:
        host = self.host()
        composed = self.advance_to_backup(host)
        source = composed.backup.restore_source
        self.assertIsNotNone(source)
        recorded = RECORD.read_session(host.state_root)
        self.assertEqual(recorded.restore_source, source)
        self.assertEqual(recorded.update_attempt_id, composed.update_attempt_id)
        self.assertEqual(recorded.target_app_dir, composed.target_app_dir)

    def test_the_next_process_restores_from_that_exact_archive(self) -> None:
        host = self.host()
        first = self.advance_to_backup(host)
        second = self.successor(host)
        composed = second._compose_remote_update()
        self.assertEqual(composed.backup.restore_source, first.backup.restore_source)

    def test_a_failed_persistence_leaves_the_archive_unknown(self) -> None:
        """An unwritable record does not become a fabricated restore source.

        And it does not merely trace: the probe -- the next thing that would
        run the new code -- is refused before it is issued, so the attempt
        never reaches an activation it could not recover from.
        """

        host = self.host()
        composed = host._compose_remote_update()
        # The staging is kept; only the archive's own write is made to fail.
        for _ in range(6):
            if composed.flow.advance().code == "prepare_ready":
                break
        original = SURFACE.write_session
        SURFACE.write_session = lambda *_args, **_kwargs: False
        self.addCleanup(setattr, SURFACE, "write_session", original)
        for _ in range(3):
            if composed.flow.advance().code == "backup_verified":
                break
        self.assertIsNotNone(composed.backup.restore_source)
        before = list(host.remote.unpack_operations)
        self.assertEqual(composed.flow.advance().code, "probe_failed")
        self.assertEqual(host.remote.unpack_operations, before)
        self.assertIsNone(composed.flow.retained_activation())

        SURFACE.write_session = original
        recorded = RECORD.read_session(host.state_root)
        self.assertIsNone(recorded.restore_source)
        self.assertIsNone(host.remote_update_restore_source)
        self.assertIn("remote update restore source could not be recorded", host.traces)
        # The same identity persists on retry, and the flow may go on.
        self.assertEqual(composed.flow.retry().code, "probe_verified")
        self.assertEqual(
            RECORD.read_session(host.state_root).restore_source,
            composed.backup.restore_source,
        )

    def test_a_record_for_another_profile_is_never_inherited(self) -> None:
        host = self.host()
        RECORD.write_session(
            host.state_root,
            RECORD.RemoteUpdateSession(
                workspace_id=WORKSPACE,
                profile_id=OTHER_PROFILE_ID,
                endpoint_fingerprint=FOREIGN_ENDPOINT,
                update_attempt_id="foreign-attempt",
                target_app_dir=TARGET_APP,
                current_app_dir=CURRENT_APP,
                restore_source=RestoreSource(
                    operation_id="foreign-backup", backup_digest="sha256:" + "9a" * 32
                ),
            ),
        )
        composed = host._compose_remote_update()
        self.assertNotEqual(composed.update_attempt_id, "foreign-attempt")
        self.assertIsNone(composed.backup.restore_source)


class ReconnectBindingTests(SurfaceTestCase):
    """A rotated session is a new selection, even under the same profile id."""

    def test_a_rotated_token_retires_and_recomposes(self) -> None:
        host = self.host()
        first = host._compose_remote_update()
        host.session = FakeSession(token="a-freshly-rotated-session-token")
        second = host._compose_remote_update()
        self.assertIsNotNone(second)
        self.assertIsNot(second, first)
        self.assertNotEqual(second.session_id, first.session_id)
        self.assertEqual(host.remote_update_generation, 1)

    def test_a_rotated_attempt_generation_retires_and_recomposes(self) -> None:
        host = self.host()
        host.session = FakeSession()
        host.session.generation = 4
        first = host._compose_remote_update()
        host.session.generation = 5
        second = host._compose_remote_update()
        self.assertIsNot(second, first)
        self.assertEqual(host.remote_update_generation, 1)

    def test_an_unchanged_runtime_selection_keeps_the_same_flow(self) -> None:
        host = self.host()
        first = host._compose_remote_update()
        self.assertIs(host._compose_remote_update(), first)
        self.assertEqual(host.remote_update_generation, 0)

    def test_the_session_token_is_never_kept_or_traced(self) -> None:
        host = self.host()
        host._compose_remote_update()
        self.assertNotIn(SESSION_TOKEN, host.remote_update_selection_binding)
        for trace in host.traces:
            self.assertNotIn(SESSION_TOKEN, trace)


class BundleCapabilityTests(SurfaceTestCase):
    """Helper capability comes from the admitted manifest, not the version."""

    def host_with(self, **kwargs: object) -> SurfaceHost:
        state = tempfile.TemporaryDirectory()
        install = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.addCleanup(install.cleanup)
        state_root = Path(state.name)
        install_root = Path(install.name)
        REGISTRY.save_connection_registry(
            state_root, registry_of(PROFILE_ID, ssh_profile())
        )
        install_root_with_bundle(install_root, **kwargs)
        host = SurfaceHost(state_root, install_root)
        self.addCleanup(host._close_remote_update_bridge)
        return host

    def test_a_same_version_bundle_without_the_helpers_is_no_capability(self) -> None:
        host = self.host_with(helpers=False)
        self.assertFalse(host.remote_update_capability())
        self.assertEqual(host.remote_update_refusal, FACTORY.REFUSED_BUNDLE_INCOMPATIBLE)

    def test_the_helper_bearing_bundle_of_this_version_is_the_capability(self) -> None:
        host = self.host_with()
        self.assertTrue(host.remote_update_capability())

    def test_the_bundle_is_chosen_by_the_declared_version_not_the_last_name(self) -> None:
        """A newer-looking filename beside it does not become the selection."""

        host = self.host_with()
        directory = host.install_root / FACTORY.BUNDLE_DIRECTORY
        stem = "WorkStack-Linux-9.9.9-cp312-manylinux_2_17_x86_64"
        (directory / f"{stem}.zip").write_bytes(b"not an artifact at all")
        (directory / f"{stem}.json").write_bytes(b"{}")
        composed = host._compose_remote_update()
        self.assertIsNotNone(composed)
        self.assertEqual(composed.target_app_dir, f"{CURRENT_APP}-{BUNDLE_VERSION}")


class FakeCore:
    """The bridge's ``CoreWebView2``: it records what was painted at it."""

    def __init__(self) -> None:
        self.pages: list[str] = []
        self.navigated: list[str] = []

    def NavigateToString(self, page: str) -> None:
        self.pages.append(page)

    def Navigate(self, target: str) -> None:
        self.navigated.append(target)


class FakeControl:
    """A WinForms control's own visibility, parenting and disposal."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.Visible = True
        self.Parent = None
        self.raised = 0
        self.disposed = 0

    def BringToFront(self) -> None:
        self.raised += 1

    def Dispose(self) -> None:
        self.disposed += 1


class FakeControls:
    def __init__(self) -> None:
        self.removed: list[FakeControl] = []

    def Remove(self, control: FakeControl) -> None:
        self.removed.append(control)


class FakeParent:
    def __init__(self) -> None:
        self.Controls = FakeControls()


class ViewHost(SurfaceHost):
    """The surface with a stand-in for the WinForms/WebView2 controls only.

    ``_create_remote_update_view`` is the one method that needs pythonnet, a
    real form and a real WebView2; everything the finding is about -- when the
    panel becomes visible, what happens on a failed initialization, and what
    retirement actually does to the controls -- is host code and runs here
    unchanged.  The control's initialization event is delivered by calling the
    same handler the real control would call.
    """

    def _create_remote_update_view(self, form) -> None:
        viewport = FakeControl("viewport")
        viewport.Visible = False
        viewport.Parent = FakeParent()
        view = FakeControl("webview")
        view.Parent = viewport
        view.CoreWebView2 = None
        generation = self.remote_update_view_generation + 1
        handler = ("view-ready", view, generation)
        self.source_event_handlers.append(handler)
        self.remote_update_view_handlers = [handler]
        self.remote_update_viewport = viewport
        self.remote_update_webview = view
        self.remote_update_view_ready = False
        self.remote_update_view_generation = generation
        self.mounted = (view, generation)

    def complete_initialization(
        self, success: bool = True, mounted: object = None
    ) -> None:
        """Deliver ``CoreWebView2InitializationCompleted``, as the control does.

        The real subscription carries the control it was created for and the
        mount it belongs to; a stale answer is delivered by passing an earlier
        ``mounted`` pair, exactly as a retired control would.
        """

        view, generation = self.mounted if mounted is None else mounted
        if success:
            view.CoreWebView2 = FakeCore()
        self._on_remote_update_view_ready(view, generation, success)


class ViewLifetimeTests(SurfaceTestCase):
    """The page is never a blank panel, and retirement really takes it down."""

    def view_host(self) -> ViewHost:
        state = tempfile.TemporaryDirectory()
        install = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.addCleanup(install.cleanup)
        state_root = Path(state.name)
        install_root = Path(install.name)
        REGISTRY.save_connection_registry(
            state_root, registry_of(PROFILE_ID, ssh_profile())
        )
        install_root_with_bundle(install_root)
        host = ViewHost(state_root, install_root)
        self.addCleanup(host._close_remote_update_bridge)
        host.form = FakeForm()
        return host

    def test_a_click_before_initialization_paints_nothing_and_covers_nothing(self) -> None:
        host = self.view_host()
        self.assertTrue(host._handle_remote_update_open())
        self.assertTrue(host.remote_update_open_pending)
        self.assertFalse(host.remote_update_view_ready)
        self.assertFalse(host.remote_update_viewport.Visible)
        self.assertIsNone(host.remote_update_webview.CoreWebView2)

    def test_initialization_completing_opens_the_page_and_only_then_shows_it(self) -> None:
        host = self.view_host()
        host._handle_remote_update_open()
        host.complete_initialization(True)
        self.assertTrue(host.remote_update_view_ready)
        self.assertFalse(host.remote_update_open_pending)
        self.assertTrue(host.remote_update_viewport.Visible)
        self.assertEqual(host.remote_update_viewport.raised, 1)
        self.assertTrue(host.remote_update_webview.CoreWebView2.pages)

    def test_a_failed_initialization_takes_the_panel_away(self) -> None:
        host = self.view_host()
        host._handle_remote_update_open()
        viewport = host.remote_update_viewport
        view = host.remote_update_webview
        host.complete_initialization(False)
        self.assertFalse(viewport.Visible)
        self.assertEqual(viewport.disposed, 1)
        self.assertEqual(view.disposed, 1)
        self.assertIsNone(host.remote_update_viewport)
        self.assertIsNone(host.remote_update_webview)
        self.assertFalse(host.remote_update_open_pending)
        self.assertIn("remote update view failed to initialize", host.traces)

    def test_retirement_disposes_the_viewport_rather_than_blanking_it(self) -> None:
        host = self.view_host()
        host._handle_remote_update_open()
        host.complete_initialization(True)
        viewport = host.remote_update_viewport
        view = host.remote_update_webview
        core = view.CoreWebView2
        host._retire_remote_update_view()
        self.assertEqual(core.navigated, ["about:blank"])
        self.assertFalse(viewport.Visible)
        self.assertIn(viewport, viewport.Parent.Controls.removed)
        self.assertEqual(viewport.disposed, 1)
        self.assertEqual(view.disposed, 1)
        self.assertIsNone(host.remote_update_webview)
        self.assertFalse(host.remote_update_view_ready)

    def test_a_profile_change_retires_the_whole_view(self) -> None:
        host = self.view_host()
        host._handle_remote_update_open()
        host.complete_initialization(True)
        viewport = host.remote_update_viewport
        host._retire_remote_update_surface()
        self.assertEqual(viewport.disposed, 1)
        self.assertIsNone(host.remote_update_viewport)

    def test_a_retired_views_late_answer_cannot_mark_the_new_one_ready(self) -> None:
        """The stale-callback rule, applied to the control's own initialization.

        A retired view can still answer: its initialization was already in
        flight when the profile changed and the panel was disposed.  That
        answer belongs to a control this host no longer owns, so it must not
        mark the newly mounted view ready or paint through it.
        """

        host = self.view_host()
        host._handle_remote_update_open()
        retired = host.mounted
        host._retire_remote_update_view()
        host._handle_remote_update_open()
        current = host.mounted
        self.assertIsNot(current[0], retired[0])

        host.complete_initialization(True, mounted=retired)
        self.assertFalse(host.remote_update_view_ready)
        self.assertIsNone(current[0].CoreWebView2)
        self.assertTrue(host.remote_update_open_pending)
        self.assertIn(
            "remote update view initialization ignored: a retired view", host.traces
        )

    def test_a_retired_views_late_failure_cannot_dispose_the_new_one(self) -> None:
        host = self.view_host()
        host._handle_remote_update_open()
        retired = host.mounted
        host._retire_remote_update_view()
        host._handle_remote_update_open()
        current = host.mounted

        host.complete_initialization(False, mounted=retired)
        self.assertIs(host.remote_update_webview, current[0])
        self.assertIsNotNone(host.remote_update_viewport)
        self.assertEqual(current[0].disposed, 0)
        self.assertEqual(host.remote_update_viewport.disposed, 0)

        host.complete_initialization(True)
        self.assertTrue(host.remote_update_view_ready)
        self.assertTrue(host.remote_update_viewport.Visible)

    def test_a_disposed_view_stops_being_retained_by_the_host(self) -> None:
        host = self.view_host()
        host._handle_remote_update_open()
        handlers = list(host.remote_update_view_handlers)
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertIn(handler, host.source_event_handlers)
        host._retire_remote_update_view()
        for handler in handlers:
            self.assertNotIn(handler, host.source_event_handlers)
        self.assertEqual(host.remote_update_view_handlers, [])

    def test_the_page_can_be_mounted_again_after_a_retirement(self) -> None:
        host = self.view_host()
        host._handle_remote_update_open()
        host.complete_initialization(True)
        host._retire_remote_update_view()
        self.assertTrue(host._handle_remote_update_open())
        host.complete_initialization(True)
        self.assertTrue(host.remote_update_viewport.Visible)


class MaintenanceRootTests(unittest.TestCase):
    """The adapter's own roots sit beside the store, never inside it."""

    def test_the_roots_are_siblings_of_the_store(self) -> None:
        state, backups = SURFACE.remote_maintenance_roots(DATA_DIR)
        self.assertEqual(state, REMOTE_STATE)
        self.assertEqual(backups, REMOTE_BACKUPS)
        for root in (state, backups):
            self.assertFalse(root.startswith(DATA_DIR + "/"))
            self.assertNotEqual(root, DATA_DIR)


class DesktopWiringTests(unittest.TestCase):
    """The host really mixes this in, and really publishes the capability."""

    def test_the_desktop_host_carries_the_surface(self) -> None:
        import workstack_desktop

        host = workstack_desktop.WorkStackDesktopHost
        order = [item.__name__ for item in host.__mro__]
        self.assertIn("RemoteUpdateHostSurfaceMixin", order)
        self.assertLess(
            order.index("RemoteUpdateHostSurfaceMixin"),
            order.index("RemoteUpdateHostBridgeMixin"),
        )

    def test_the_status_payload_publishes_the_settled_capability(self) -> None:
        import workstack_desktop

        host = workstack_desktop.WorkStackDesktopHost.__new__(
            workstack_desktop.WorkStackDesktopHost
        )
        host.update_status = {"type": "workstack-update-status", "state": "current"}
        host.update_preferences = type(
            "Preferences", (), {"auto_check": True, "auto_download": False, "install_on_exit": False}
        )()
        host.remote_update_available = False
        payload = host._update_payload()
        self.assertIs(payload["remote_update_available"], False)
        host.remote_update_available = True
        self.assertIs(host._update_payload()["remote_update_available"], True)
        # The frontend admits the field as an optional boolean; it must encode.
        json.dumps(host._update_payload())

    def test_the_restart_states_the_roots_parse_args_would_otherwise_default(self) -> None:
        """The two supported roots are not derivable from the working directory."""

        import workstack_desktop

        argv = sys.argv
        sys.argv = ["workstack_desktop.py"]
        try:
            defaults = workstack_desktop.parse_args()
        finally:
            sys.argv = argv
        host = SurfaceHost.__new__(SurfaceHost)
        host.install_root = Path("C:/Program Files/WorkStack")
        host.state_root = Path("D:/work-stack-state")
        self.assertNotEqual(defaults.state_root, host.state_root)
        self.assertNotEqual(defaults.install_root, host.install_root)
        command = host._remote_update_restart_command(
            host.install_root / SURFACE.DESKTOP_LAUNCHER
        )
        self.assertIn("--state-root", command)
        self.assertIn(str(host.state_root), command)
        self.assertIn("--install-root", command)
        self.assertIn(str(host.install_root), command)

    def test_the_remote_open_request_reaches_the_surface(self) -> None:
        import workstack_desktop

        source = Path(workstack_desktop.__file__).read_text(encoding="utf-8")
        self.assertIn('parts == [UPDATE_HOST_PREFIX, "remote-open"]', source)
        self.assertIn("self._handle_remote_update_open()", source)
        self.assertIn("self._launch_remote_update_restart()", source)
        self.assertIn("self._close_remote_update_bridge()", source)
        self.assertIn("self._settle_remote_update_capability()", source)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()

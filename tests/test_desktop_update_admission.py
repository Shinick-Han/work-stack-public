"""A remote may withhold the install; it may not withhold the download.

`_check_update_worker` used to read remote storage metadata *before* it would
report an update as available or fetch it, and any failure of that read - an
offline tunnel, another workspace, an older protocol - produced a "blocked"
card with nothing downloaded. The PC could then neither see nor stage its own
release because a machine on the other end of an SSH tunnel was unreachable.

These tests pin the separation. Discovery and verified download are the PC's
own, and no remote condition suppresses them. Installation still requires the
connected remote to be admitted, because the desktop that gets installed is the
one that will connect to it next - and that admission is settled while the
remote can still be asked, not in `run`'s finally block where the tunnel is
already gone.

They also pin the two edges of that lifecycle. Every settlement boundary - the
finished download, the install button, the first statement of window close -
observes the remote that is connected now, so a workspace or protocol that
changed after a download-time admission is caught before the applicator starts.
And the post-shutdown install is permitted only against this host's own record
of the close it performed: an SSH tunnel that dies between the settlement and
the launch reports no session too, and that is unknown, not a shutdown.

The scaffolding is the production one: `build_host` and `settle_remote_session`
from the existing version-status suite drive the real attempt protocol, the
real resource gate and the real startup state machine. Only the network, the
release download and the applicator launch are substituted. Nothing here
touches SSOT, a real installer, a real manifest or a real remote.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


TESTS = Path(__file__).resolve().parent
SHELL = TESTS.parent / "desktop" / "python-webview-shell"
for import_root in (TESTS, SHELL):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

# The admission module is imported before the host scaffold on purpose. The
# scaffold executes workstack_desktop.py inside `mock.patch.dict(sys.modules,
# ...)`, which removes every module that import pulled in when the context
# exits; importing this one first keeps a single module object, so the host's
# bound names and this suite's names are the same functions and dataclasses.
import desktop_update_admission as ADMISSION  # noqa: E402

import test_desktop_update_version_status as VS  # noqa: E402  the existing host scaffold

HOST = VS.HOST
WORKSPACE_ID = VS.WORKSPACE_ID
OTHER_WORKSPACE_ID = VS.OTHER_WORKSPACE_ID
RELEASES = "https://github.com/Shinick-Han/work-stack-public/releases"


def downloaded_release(version: str = "1.0.14", minimum_remote_protocol: int = 1):
    """One verified artifact already on disk, as `download_update` returns it."""

    updates = Path("fixture-state") / "updates"
    return types.SimpleNamespace(
        version=version,
        setup_path=updates / f"WorkStack-Setup-{version}.ps1",
        checksum_path=updates / f"WorkStack-Setup-{version}.ps1.sha256",
        release_url=f"{RELEASES}/tag/v{version}",
        minimum_remote_protocol=minimum_remote_protocol,
    )


def newer_manifest(version: str = "1.0.14", minimum_remote_protocol: int = 1):
    """A stable channel offering a release newer than the installed desktop."""

    return types.SimpleNamespace(
        is_newer=True,
        version=version,
        release_url=f"{RELEASES}/tag/v{version}",
        minimum_remote_protocol=minimum_remote_protocol,
    )


def build_update_host(*, remote: bool, settled: bool = True, auto_download: bool = True,
                      install_on_exit: bool = True):
    """The production host slice plus the local-update fields the seam reads."""

    host = VS.build_host(remote=remote, settled=settled)
    host.install_root = Path("fixture-app")
    host.state_root = Path("fixture-state")
    host.window = mock.Mock()
    host.form = None
    host._trace = mock.Mock()
    host.update_preferences = types.SimpleNamespace(
        auto_check=True, auto_download=auto_download, install_on_exit=install_on_exit
    )
    return host


def hold_download(host, downloaded=None, *, install_on_exit: bool = True):
    """Put a verified artifact on the host without running a check."""

    host.downloaded_update = downloaded or downloaded_release()
    host.install_update_on_exit = install_on_exit
    return host.downloaded_update


def run_check(host, manifest, **urlopen: object):
    """Drive one update check with the network and the download substituted."""

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(HOST, "fetch_url_bytes", return_value=b"manifest"))
        stack.enter_context(mock.patch.object(HOST, "parse_update_manifest", return_value=manifest))
        download = stack.enter_context(
            mock.patch.object(HOST, "download_update", return_value=downloaded_release(
                manifest.version, manifest.minimum_remote_protocol
            ))
        )
        if urlopen:
            stack.enter_context(mock.patch.object(HOST.urllib.request, "urlopen", **urlopen))
        host._check_update_worker(force_download=False)
    return download


def states(host) -> list[str]:
    return [call.args[0] for call in host._set_update_status.call_args_list]


def last_message(host) -> str:
    return str(host._set_update_status.call_args.kwargs["message"])


def press_install(host, *, applicator: object = None, **urlopen: object):
    """Press Install with the remote read and the applicator substituted.

    Returns the two mocks the assertions are about: whether the remote was
    actually asked at this boundary, and whether the applicator was started.
    """

    with contextlib.ExitStack() as stack:
        probe = stack.enter_context(mock.patch.object(HOST.urllib.request, "urlopen", **urlopen))
        launch = stack.enter_context(
            mock.patch.object(HOST, "launch_update_process", **(applicator or {}))
        )
        host._handle_update_message(HOST.UPDATE_HOST_PREFIX + "|install")
    return probe, launch


def settle(host, **urlopen: object):
    """Settle admission once with the remote read substituted."""

    with mock.patch.object(HOST.urllib.request, "urlopen", **urlopen):
        return ADMISSION.settle_update_admission(host, refusal=HOST.RemoteAuthorityMismatch)


class IndependentDiscoveryTests(unittest.TestCase):
    """An unusable remote must not hide or withhold this PC's own release."""

    def test_offline_remote_still_reports_available_without_probing_at_all(self) -> None:
        host = build_update_host(remote=True, auto_download=False)

        with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
            download = run_check(host, newer_manifest())

        urlopen.assert_not_called()
        download.assert_not_called()
        self.assertEqual("available", states(host)[-1])
        self.assertEqual("A verified Work Stack update is available", last_message(host))

    def test_offline_remote_still_downloads_and_keeps_the_verified_artifact(self) -> None:
        host = build_update_host(remote=True)

        download = run_check(host, newer_manifest(), side_effect=urllib.error.URLError("connection refused"))

        download.assert_called_once()
        self.assertEqual("1.0.14", host.downloaded_update.version)
        self.assertEqual(["downloading", "blocked"], states(host))
        self.assertIn("downloaded and kept", last_message(host))
        self.assertIn("could not be reached", last_message(host))
        self.assertNotIn("refused", last_message(host))

    def test_older_remote_protocol_no_longer_stops_the_download(self) -> None:
        host = build_update_host(remote=True)

        download = run_check(host, newer_manifest(minimum_remote_protocol=4),
                             return_value=VS.storage_response("1.0.9", 1))

        download.assert_called_once()
        self.assertIsNotNone(host.downloaded_update)
        self.assertEqual("blocked", states(host)[-1])
        self.assertIn("requires protocol 4", last_message(host))
        self.assertEqual(ADMISSION.REFUSED, host.update_admission.verdict)

    def test_remote_serving_another_workspace_still_downloads(self) -> None:
        host = build_update_host(remote=True)

        download = run_check(host, newer_manifest(),
                             return_value=VS.storage_response("1.0.14", 1, OTHER_WORKSPACE_ID))

        download.assert_called_once()
        self.assertIsNotNone(host.downloaded_update)
        self.assertEqual(ADMISSION.REFUSED, host.update_admission.verdict)
        self.assertIn("different workspace", last_message(host))

    def test_compatible_remote_downloads_and_is_admitted(self) -> None:
        host = build_update_host(remote=True)

        run_check(host, newer_manifest(), return_value=VS.storage_response("1.0.14", 2))

        self.assertEqual(["downloading", "ready"], states(host))
        self.assertEqual("Verified update will install when Work Stack closes", last_message(host))
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        self.assertEqual(("1.0.14", 2), VS.remembered(host))

    def test_local_desktop_never_probes_and_is_admitted(self) -> None:
        host = build_update_host(remote=False, install_on_exit=False)

        with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
            run_check(host, newer_manifest())

        urlopen.assert_not_called()
        self.assertEqual("ready", states(host)[-1])
        self.assertEqual("Verified update is ready to install", last_message(host))


class ExplicitInstallTests(unittest.TestCase):
    """The install button applies an admitted release and nothing else."""

    def test_admitted_release_launches_the_applicator_and_closes_the_window(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        settle(host, return_value=VS.storage_response("1.0.14", 1))
        process = mock.Mock(**{"poll.return_value": None})

        probe, launch = press_install(host, applicator={"return_value": process},
                                      return_value=VS.storage_response("1.0.14", 1))

        probe.assert_called_once()
        launch.assert_called_once()
        host.window.destroy.assert_called_once()
        self.assertEqual("installing", states(host)[-1])
        self.assertTrue(host.install_update_on_exit)

    def test_a_protocol_change_after_the_download_admission_refuses_the_install(self) -> None:
        """The remote dropped below the release floor after the download was admitted."""

        host = build_update_host(remote=True)
        run_check(host, newer_manifest(minimum_remote_protocol=2),
                  return_value=VS.storage_response("1.0.14", 2))
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        self.assertEqual("ready", states(host)[-1])
        artifact = host.downloaded_update

        probe, launch = press_install(host, return_value=VS.storage_response("1.0.9", 1))

        probe.assert_called_once()
        launch.assert_not_called()
        host.window.destroy.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual(ADMISSION.REFUSED, host.update_admission.verdict)
        self.assertEqual("blocked", states(host)[-1])
        self.assertIn("requires protocol 2", last_message(host))
        self.assertIn("downloaded and kept", last_message(host))

    def test_a_workspace_change_after_the_download_admission_refuses_the_install(self) -> None:
        """The endpoint started answering for another workspace after admission."""

        host = build_update_host(remote=True)
        run_check(host, newer_manifest(), return_value=VS.storage_response("1.0.14", 2))
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        artifact = host.downloaded_update

        probe, launch = press_install(
            host, return_value=VS.storage_response("1.0.14", 2, OTHER_WORKSPACE_ID)
        )

        probe.assert_called_once()
        launch.assert_not_called()
        host.window.destroy.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual(ADMISSION.REFUSED, host.update_admission.verdict)
        self.assertIn("different workspace", last_message(host))

    def test_a_remote_that_went_offline_after_admission_holds_the_install(self) -> None:
        host = build_update_host(remote=True)
        run_check(host, newer_manifest(), return_value=VS.storage_response("1.0.14", 2))
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        artifact = host.downloaded_update

        probe, launch = press_install(host, side_effect=urllib.error.URLError("connection refused"))

        probe.assert_called_once()
        launch.assert_not_called()
        host.window.destroy.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual(ADMISSION.UNKNOWN, host.update_admission.verdict)

    def test_incompatible_remote_refuses_the_install_and_keeps_the_artifact(self) -> None:
        host = build_update_host(remote=True)
        artifact = hold_download(host, downloaded_release(minimum_remote_protocol=3))

        with mock.patch.object(HOST.urllib.request, "urlopen",
                               return_value=VS.storage_response("1.0.9", 1)):
            with mock.patch.object(HOST, "launch_update_process") as launch:
                host._handle_update_message(HOST.UPDATE_HOST_PREFIX + "|install")

        launch.assert_not_called()
        host.window.destroy.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual("blocked", states(host)[-1])
        self.assertIn("requires protocol 3", last_message(host))
        self.assertIn("downloaded and kept", last_message(host))

    def test_wrong_workspace_refuses_the_install_and_keeps_the_artifact(self) -> None:
        host = build_update_host(remote=True)
        artifact = hold_download(host)

        with mock.patch.object(HOST.urllib.request, "urlopen",
                               return_value=VS.storage_response("1.0.14", 1, OTHER_WORKSPACE_ID)):
            with mock.patch.object(HOST, "launch_update_process") as launch:
                host._handle_update_message(HOST.UPDATE_HOST_PREFIX + "|install")

        launch.assert_not_called()
        host.window.destroy.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertIn("different workspace", last_message(host))

    def test_unreachable_remote_holds_the_install_without_closing_the_window(self) -> None:
        host = build_update_host(remote=True)
        artifact = hold_download(host)

        with mock.patch.object(HOST.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("connection refused")):
            with mock.patch.object(HOST, "launch_update_process") as launch:
                host._handle_update_message(HOST.UPDATE_HOST_PREFIX + "|install")

        launch.assert_not_called()
        host.window.destroy.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual(ADMISSION.UNKNOWN, host.update_admission.verdict)
        self.assertIn("installing it is still pending", last_message(host))

    def test_failed_handoff_leaves_the_window_open_and_disarms_install_on_exit(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)

        press_install(host, applicator={"side_effect": OSError("no applicator")},
                      return_value=VS.storage_response("1.0.14", 1))

        host.window.destroy.assert_not_called()
        self.assertFalse(host.install_update_on_exit)
        self.assertEqual("error", states(host)[-1])

    def test_no_download_forces_one_check_instead_of_refusing(self) -> None:
        host = build_update_host(remote=True)
        host.downloaded_update = None
        host.install_update_on_exit = False
        host._start_update_check = mock.Mock()

        host._handle_update_message(HOST.UPDATE_HOST_PREFIX + "|install")

        host._start_update_check.assert_called_once_with(force_download=True)


def close_host(host, stop_owned):
    """Run the real form-closing path with its outward effects substituted."""

    host.startup_recovery = HOST.StartupRecoveryLifetime()
    host.remote_shutdown_requested = threading.Event()
    host.connection_registry_worker = mock.Mock()
    host._stop_knowledge_desktop = mock.Mock()
    host._stop_remote_monitor = mock.Mock()
    host.server_started_by_host = False
    host.server_stop_thread = None
    host._stop_owned_server = stop_owned
    host._on_form_closing(None, None)
    if host.server_stop_thread is not None:
        host.server_stop_thread.join(timeout=10)


def close_and_shut_down(host, **urlopen):
    """Close the window with the remote answering, then end the session.

    This is the production install-on-exit order: `FormClosing` settles
    admission while the tunnel is alive, the owned connection is stopped, and
    only then does `run`'s finally block reach the launch.
    """

    with mock.patch.object(HOST.urllib.request, "urlopen", **urlopen) as probe:
        close_host(host, mock.Mock())
    VS.close_owned_session(host)
    return probe


class InstallOnExitTests(unittest.TestCase):
    """Admission is settled while the authority is alive, and reused after."""

    def test_admission_is_settled_before_the_tunnel_is_torn_down(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        observed: list[tuple[bool, bool]] = []
        stop_owned = mock.Mock()

        def probe(*_args, **_kwargs):
            observed.append((host._settled_remote_session() is not None, stop_owned.called))
            return VS.storage_response("1.0.14", 1)

        with mock.patch.object(HOST.urllib.request, "urlopen", side_effect=probe):
            close_host(host, stop_owned)

        self.assertEqual([(True, False)], observed)
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        stop_owned.assert_called_once()

    def test_the_close_this_host_performed_is_what_installs_after_shutdown(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)

        close_and_shut_down(host, return_value=VS.storage_response("1.0.14", 1))

        self.assertIsNone(host._settled_remote_session())
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        with mock.patch.object(HOST, "launch_update_process", return_value=mock.Mock()) as launch:
            self.assertTrue(host._launch_pending_update())

        launch.assert_called_once()

    def test_close_re_observes_the_remote_even_after_a_download_time_admission(self) -> None:
        """A protocol change between the download and the close must be seen."""

        host = build_update_host(remote=True)
        run_check(host, newer_manifest(minimum_remote_protocol=2),
                  return_value=VS.storage_response("1.0.14", 2))
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        artifact = host.downloaded_update

        probe = close_and_shut_down(host, return_value=VS.storage_response("1.0.9", 1))

        probe.assert_called_once()
        self.assertEqual(ADMISSION.REFUSED, host.update_admission.verdict)
        with mock.patch.object(HOST, "launch_update_process") as launch:
            self.assertFalse(host._launch_pending_update())

        launch.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual("blocked", states(host)[-1])
        self.assertIn("requires protocol 2", last_message(host))

    def test_a_workspace_change_before_close_holds_the_install_after_shutdown(self) -> None:
        host = build_update_host(remote=True)
        run_check(host, newer_manifest(), return_value=VS.storage_response("1.0.14", 2))
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        artifact = host.downloaded_update

        probe = close_and_shut_down(
            host, return_value=VS.storage_response("1.0.14", 2, OTHER_WORKSPACE_ID)
        )

        probe.assert_called_once()
        self.assertEqual(ADMISSION.REFUSED, host.update_admission.verdict)
        with mock.patch.object(HOST, "launch_update_process") as launch:
            self.assertFalse(host._launch_pending_update())

        launch.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertIn("different workspace", last_message(host))

    def test_a_tunnel_that_dies_between_settlement_and_launch_never_installs(self) -> None:
        """No session is not a shutdown: an unexpected death stays unknown."""

        host = build_update_host(remote=True)
        artifact = hold_download(host)
        settle(host, return_value=VS.storage_response("1.0.14", 1))
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        host.remote_ssh_process.exit(23)

        self.assertEqual(ADMISSION.NO_SESSION, ADMISSION.remote_session_identity(host))
        with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
            with mock.patch.object(HOST, "launch_update_process") as launch:
                self.assertFalse(host._launch_pending_update())

        urlopen.assert_not_called()
        launch.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual("blocked", states(host)[-1])
        self.assertIn("installing it is still pending", last_message(host))

    def test_a_tunnel_that_died_before_close_leaves_no_record_and_holds(self) -> None:
        host = build_update_host(remote=True)
        artifact = hold_download(host)
        settle(host, return_value=VS.storage_response("1.0.14", 1))
        host.remote_ssh_process.exit(23)

        probe = close_and_shut_down(host, return_value=VS.storage_response("1.0.14", 1))

        probe.assert_not_called()
        self.assertEqual(ADMISSION.UNKNOWN, host.update_admission.verdict)
        self.assertIsNone(getattr(host, "update_admission_close", None))
        with mock.patch.object(HOST, "launch_update_process") as launch:
            self.assertFalse(host._launch_pending_update())

        launch.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)

    def test_unknown_admission_holds_the_install_and_keeps_the_artifact(self) -> None:
        host = build_update_host(remote=True)
        artifact = hold_download(host)
        settle(host, side_effect=urllib.error.URLError("connection refused"))
        VS.close_owned_session(host)

        with mock.patch.object(HOST, "launch_update_process") as launch:
            self.assertFalse(host._launch_pending_update())

        launch.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual("blocked", states(host)[-1])
        self.assertIn("installing it is still pending", last_message(host))

    def test_launch_never_probes_the_remote_after_shutdown(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        close_and_shut_down(host, return_value=VS.storage_response("1.0.14", 1))

        with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
            with mock.patch.object(HOST, "launch_update_process", return_value=mock.Mock()):
                self.assertTrue(host._launch_pending_update())

        urlopen.assert_not_called()

    def test_a_disarmed_preference_still_leaves_the_artifact_in_place(self) -> None:
        host = build_update_host(remote=True)
        artifact = hold_download(host, install_on_exit=False)

        with mock.patch.object(HOST, "launch_update_process") as launch:
            self.assertFalse(host._launch_pending_update())

        launch.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)

    def test_one_applicator_is_launched_however_often_the_exit_path_runs(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        process = mock.Mock(**{"poll.return_value": None})

        _, launch = press_install(host, applicator={"return_value": process},
                                  return_value=VS.storage_response("1.0.14", 1))
        with mock.patch.object(HOST, "launch_update_process", return_value=process):
            VS.close_owned_session(host)
            self.assertTrue(host._launch_pending_update())
            self.assertTrue(host._launch_pending_update())

        launch.assert_called_once()


class AdmissionBindingTests(unittest.TestCase):
    """A verdict speaks for one artifact, one remote and one session only."""

    def admitted(self, host):
        return settle(host, return_value=VS.storage_response("1.0.14", 1))

    def test_a_replaced_session_cannot_reuse_the_previous_admission(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        first = self.admitted(host)
        VS.close_owned_session(host)
        VS.settle_remote_session(host)

        self.assertNotEqual(first.binding.session, ADMISSION.remote_session_identity(host))
        self.assertFalse(ADMISSION.admission_permits_install(first, host))

    def test_a_rebind_in_flight_cannot_reuse_the_previous_admission(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        admission = self.admitted(host)
        host._begin_remote_workspace_rebind(OTHER_WORKSPACE_ID)

        self.assertEqual(ADMISSION.INDETERMINATE_SESSION, ADMISSION.remote_session_identity(host))
        self.assertFalse(ADMISSION.admission_permits_install(admission, host))
        self.assertEqual(OTHER_WORKSPACE_ID, host.remote_rebind_target)

    def test_a_required_recovery_cannot_reuse_the_previous_admission(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        admission = self.admitted(host)
        host.remote_recovery_required.set()

        self.assertEqual(ADMISSION.INDETERMINATE_SESSION, ADMISSION.remote_session_identity(host))
        self.assertFalse(ADMISSION.admission_permits_install(admission, host))

    def test_a_different_artifact_cannot_reuse_the_previous_admission(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        admission = self.admitted(host)
        hold_download(host, downloaded_release("1.0.15"))

        self.assertFalse(ADMISSION.admission_permits_install(admission, host))

    def test_a_moved_endpoint_cannot_reuse_the_previous_admission(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        admission = self.admitted(host)
        host.workstack_url = "http://127.0.0.1:19999/"

        self.assertFalse(ADMISSION.admission_permits_install(admission, host))

    def test_a_session_settled_after_an_unknown_verdict_is_not_reused(self) -> None:
        host = build_update_host(remote=True, settled=False)
        hold_download(host)
        admission = settle(host)

        self.assertEqual(ADMISSION.UNKNOWN, admission.verdict)
        VS.settle_remote_session(host)
        self.assertFalse(ADMISSION.admission_permits_install(admission, host))

    def test_an_admitted_binding_is_re_observed_at_the_next_settlement(self) -> None:
        """An earlier admission is not a shortcut past the next probe."""

        host = build_update_host(remote=True)
        hold_download(host)
        first = self.admitted(host)

        with mock.patch.object(HOST.urllib.request, "urlopen",
                               return_value=VS.storage_response("1.0.14", 1, OTHER_WORKSPACE_ID)) as urlopen:
            again = ADMISSION.settle_update_admission(host, refusal=HOST.RemoteAuthorityMismatch)

        urlopen.assert_called_once()
        self.assertEqual(ADMISSION.ADMITTED, first.verdict)
        self.assertEqual(ADMISSION.REFUSED, again.verdict)
        self.assertIs(again, host.update_admission)
        self.assertEqual(first.binding, again.binding)

    def test_a_dead_tunnel_alone_never_stands_in_for_an_intentional_close(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        admission = self.admitted(host)
        host.remote_ssh_process.exit(23)

        self.assertEqual(ADMISSION.NO_SESSION, ADMISSION.remote_session_identity(host))
        self.assertIsNone(getattr(host, "update_admission_close", None))
        self.assertFalse(ADMISSION.admission_permits_install(admission, host))

    def test_a_captured_close_covers_only_the_session_it_was_captured_for(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        admission = self.admitted(host)
        ADMISSION.capture_intentional_close(host, admission)
        VS.close_owned_session(host)

        self.assertTrue(ADMISSION.admission_permits_install(admission, host))
        settled = admission.binding
        successor = ADMISSION.UpdateAdmission(
            ADMISSION.ADMITTED,
            ADMISSION.AdmissionBinding(settled.download, settled.workspace_id, settled.endpoint,
                                       settled.session + 1, settled.attempt + 1),
            ADMISSION.ADMITTED_DETAIL,
        )
        self.assertFalse(ADMISSION.admission_permits_install(successor, host))

    def test_no_close_is_captured_for_a_verdict_that_was_not_admitted(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        unknown = settle(host, side_effect=urllib.error.URLError("connection refused"))

        ADMISSION.capture_intentional_close(host, unknown)

        self.assertEqual(ADMISSION.UNKNOWN, unknown.verdict)
        self.assertIsNone(getattr(host, "update_admission_close", None))

    def test_an_unknown_verdict_is_re_observed_while_the_remote_is_reachable(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)
        self.assertEqual(ADMISSION.UNKNOWN, settle(host, side_effect=urllib.error.URLError("down")).verdict)

        self.assertEqual(ADMISSION.ADMITTED, self.admitted(host).verdict)
        self.assertTrue(ADMISSION.admission_permits_install(host.update_admission, host))

    def test_a_session_replaced_during_the_probe_yields_unknown(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)

        def replace(*_args, **_kwargs):
            VS.close_owned_session(host)
            VS.settle_remote_session(host)
            return VS.storage_response("1.0.14", 1)

        admission = settle(host, side_effect=replace)

        self.assertEqual(ADMISSION.UNKNOWN, admission.verdict)
        self.assertIn("changed while", admission.detail)

    def test_a_local_desktop_needs_no_record_because_no_remote_is_bound(self) -> None:
        host = build_update_host(remote=False)
        hold_download(host)

        self.assertFalse(hasattr(host, "update_admission"))
        self.assertTrue(ADMISSION.admission_permits_install(None, host))

    def test_a_bound_remote_always_needs_a_record(self) -> None:
        host = build_update_host(remote=True)
        hold_download(host)

        self.assertFalse(ADMISSION.admission_permits_install(None, host))

    def test_nothing_downloaded_is_unknown_and_never_permits_an_install(self) -> None:
        host = build_update_host(remote=True)
        host.downloaded_update = None
        host.install_update_on_exit = False

        admission = settle(host)

        self.assertEqual(ADMISSION.UNKNOWN, admission.verdict)
        self.assertFalse(ADMISSION.admission_permits_install(admission, host))


def install_replacement_session(host, *, dies: bool = True):
    """Install a real successor session through the host's own attempt protocol.

    The predecessor is released the way the host releases it, then a whole new
    attempt is begun, committed and driven to READY by `settle_remote_session`,
    so the successor's generation comes from the production state machine rather
    than from a number this test made up. Killing its captured tunnel afterwards
    reproduces the case that matters: the successor is gone too, so the launch
    path sees no session at all and must not fall back on the predecessor.
    """

    VS.close_owned_session(host)
    resources = VS.settle_remote_session(host)
    if dies:
        host.remote_ssh_process.exit(23)
    return resources


def replace_after_the_binding_check(host, replacement):
    """Run `replacement` in the window the settlement can no longer observe.

    `_remember_remote_metadata` is the last thing `observe_update_admission`
    does after it has re-read the binding and decided the answer is admitted,
    so a successor started from here lands past every check the settlement
    itself performs and before the close record is written. That is the exact
    scheduling point the capture has to defend, and it is a real host call
    rather than a patched module internal.
    """

    remember = host._remember_remote_metadata

    def remember_then_replace(metadata):
        remember(metadata)
        replacement()

    host._remember_remote_metadata = remember_then_replace


def launch_without_network(host, **applicator):
    """Reach the post-shutdown launch with no network available at all."""

    with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
        with mock.patch.object(HOST, "launch_update_process", **applicator) as launch:
            permitted = host._launch_pending_update()
    urlopen.assert_not_called()
    return permitted, launch


class SupersededSessionTests(unittest.TestCase):
    """A close record speaks for the last session, not merely for a missing one.

    The install happens after the tunnel is gone, so "no settled session" is the
    normal state at launch. It is also what a replaced session that has since
    died reports, and the two must not be confused: the predecessor's admission
    and close record still name a remote that something newer has already taken
    the place of. The host's retained attempt generation is what separates them,
    and these tests drive it through the production `FormClosing` and
    `_launch_pending_update()` paths.
    """

    def admit_and_close(self, host):
        """Hold a download, close the window with the remote answering, shut down."""

        artifact = hold_download(host)
        probe = close_and_shut_down(host, return_value=VS.storage_response("1.0.14", 1))
        probe.assert_called_once()
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        return artifact

    def test_a_close_record_still_installs_when_nothing_replaced_the_session(self) -> None:
        """The healthy same-generation case: this host closed the last session."""

        host = build_update_host(remote=True)
        self.admit_and_close(host)
        record = host.update_admission_close
        session, attempt = ADMISSION.remote_lifecycle_identity(host)

        permitted, launch = launch_without_network(host, return_value=mock.Mock())

        self.assertTrue(permitted)
        launch.assert_called_once()
        self.assertEqual(ADMISSION.NO_SESSION, session)
        self.assertEqual(record.binding.attempt, attempt)
        self.assertEqual(record.binding.session, attempt)

    def test_a_replacement_that_dies_before_the_launch_supersedes_the_record(self) -> None:
        """Generation 1 closed, generation 2 installed and dead: hold the artifact."""

        host = build_update_host(remote=True)
        artifact = self.admit_and_close(host)
        record = host.update_admission_close
        successor = install_replacement_session(host, dies=True)

        permitted, launch = launch_without_network(host)

        self.assertFalse(permitted)
        launch.assert_not_called()
        self.assertEqual(record.binding.session + 1, successor.generation)
        self.assertIsNone(host._settled_remote_session())
        self.assertEqual(
            (ADMISSION.NO_SESSION, successor.generation),
            ADMISSION.remote_lifecycle_identity(host),
        )
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual("blocked", states(host)[-1])
        self.assertIn("downloaded and kept", last_message(host))
        host.window.destroy.assert_not_called()

    def test_a_live_replacement_also_supersedes_the_record(self) -> None:
        """The successor need not die for the predecessor's record to be spent."""

        host = build_update_host(remote=True)
        artifact = self.admit_and_close(host)
        successor = install_replacement_session(host, dies=False)

        permitted, launch = launch_without_network(host)

        self.assertFalse(permitted)
        launch.assert_not_called()
        self.assertEqual(successor.generation, ADMISSION.remote_session_identity(host))
        self.assertIs(artifact, host.downloaded_update)

    def test_a_replacement_started_after_the_binding_check_captures_no_record(self) -> None:
        """The race the capture defends: the successor begins before the record."""

        host = build_update_host(remote=True)
        artifact = hold_download(host)
        replace_after_the_binding_check(
            host, lambda: install_replacement_session(host, dies=True)
        )

        probe = close_and_shut_down(host, return_value=VS.storage_response("1.0.14", 1))

        probe.assert_called_once()
        self.assertEqual(ADMISSION.ADMITTED, host.update_admission.verdict)
        self.assertIsNone(getattr(host, "update_admission_close", None))
        permitted, launch = launch_without_network(host)
        self.assertFalse(permitted)
        launch.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertIn("downloaded and kept", last_message(host))
        host.window.destroy.assert_not_called()

    def test_a_replacement_started_before_the_close_probe_is_seen_by_it(self) -> None:
        """Earlier in the same race, the settlement itself refuses to admit."""

        host = build_update_host(remote=True)
        artifact = hold_download(host)
        install_replacement_session(host, dies=True)

        probe = close_and_shut_down(host, return_value=VS.storage_response("1.0.14", 1))

        probe.assert_not_called()
        self.assertEqual(ADMISSION.UNKNOWN, host.update_admission.verdict)
        self.assertIsNone(getattr(host, "update_admission_close", None))
        permitted, launch = launch_without_network(host)
        self.assertFalse(permitted)
        launch.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)

    def test_a_superseded_record_is_not_repaired_by_pressing_install(self) -> None:
        """The explicit install re-probes, finds no session, and keeps the window."""

        host = build_update_host(remote=True)
        artifact = self.admit_and_close(host)
        install_replacement_session(host, dies=True)

        probe, launch = press_install(host, return_value=VS.storage_response("1.0.14", 1))

        probe.assert_not_called()
        launch.assert_not_called()
        host.window.destroy.assert_not_called()
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual(ADMISSION.UNKNOWN, host.update_admission.verdict)
        self.assertEqual("blocked", states(host)[-1])
        self.assertIn("no settled session", last_message(host))

    def test_a_failed_applicator_after_an_admitted_install_keeps_the_window(self) -> None:
        """A refused handoff never destroys the window or the artifact."""

        host = build_update_host(remote=True)
        artifact = hold_download(host)

        press_install(host, applicator={"side_effect": OSError("no applicator")},
                      return_value=VS.storage_response("1.0.14", 1))

        host.window.destroy.assert_not_called()
        self.assertFalse(host.install_update_on_exit)
        self.assertIs(artifact, host.downloaded_update)
        self.assertEqual("error", states(host)[-1])

    def test_the_retained_generation_outlives_the_attempt_it_names(self) -> None:
        """What the whole correction rests on, stated against the real machine."""

        host = build_update_host(remote=True)
        machine = host.remote_startup
        first = machine.last_attempt_generation

        VS.close_owned_session(host)
        self.assertIsNone(machine.active_attempt_id)
        self.assertEqual(first, machine.last_attempt_generation)
        self.assertEqual(first, ADMISSION.retained_attempt_generation(host))

        VS.settle_remote_session(host)
        self.assertEqual(first + 1, machine.last_attempt_generation)
        host.remote_ssh_process.exit(23)
        self.assertEqual(first + 1, ADMISSION.retained_attempt_generation(host))
        self.assertEqual(ADMISSION.NO_SESSION, ADMISSION.remote_session_identity(host))


if __name__ == "__main__":
    unittest.main()

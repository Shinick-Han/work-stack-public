"""The update card must separate the desktop verdict from the remote one.

The Windows installer replaces the desktop shell only, and in SSH mode the
interface is served by the remote installation. A current desktop therefore
cannot speak for the remote build, and an unreachable remote cannot be
flattened into "up to date". These tests pin the wording and the boundary:
the desktop update state machine is unchanged, only the reported claim is.
They also pin the three ways a claim could be manufactured rather than observed
- a malformed version whose leading run looks canonical, a valid answer that
belongs to a workspace or a binding other than the selected one, and a
configured profile that has no settled, live, owned session behind it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

STATUS_PATH = SHELL / "workstack_update_status.py"
STATUS_SPEC = importlib.util.spec_from_file_location("workstack_update_status_test", STATUS_PATH)
assert STATUS_SPEC is not None and STATUS_SPEC.loader is not None
STATUS = importlib.util.module_from_spec(STATUS_SPEC)
sys.modules[STATUS_SPEC.name] = STATUS
STATUS_SPEC.loader.exec_module(STATUS)

HOST_PATH = SHELL / "workstack_desktop.py"
HOST_SPEC = importlib.util.spec_from_file_location("workstack_desktop_version_status_test", HOST_PATH)
assert HOST_SPEC is not None and HOST_SPEC.loader is not None
HOST = importlib.util.module_from_spec(HOST_SPEC)
sys.modules[HOST_SPEC.name] = HOST
with mock.patch.dict(sys.modules, {"webview": types.SimpleNamespace(Window=object)}):
    HOST_SPEC.loader.exec_module(HOST)

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
DESKTOP_VERSION = HOST.WORKSTACK_VERSION


def storage_response(
    product_version: str,
    protocol_version: object = 1,
    workspace_id: str = WORKSPACE_ID,
):
    response = mock.MagicMock()
    response.read.return_value = json.dumps({"data": {
        "workspace_id": workspace_id,
        "product_version": product_version,
        "remote_protocol_version": protocol_version,
    }}).encode("utf-8")
    response.__enter__.return_value = response
    return response


def current_manifest(version: str = DESKTOP_VERSION, minimum_remote_protocol: int = 1):
    """A stable channel that offers nothing newer than the installed desktop."""

    return types.SimpleNamespace(
        is_newer=False,
        version=version,
        release_url=f"https://github.com/Shinick-Han/work-stack-public/releases/tag/v{version}",
        minimum_remote_protocol=minimum_remote_protocol,
    )


class OwnedTunnel:
    """The captured SSH process of an owned session: alive until it exits."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def exit(self, returncode: int = 1) -> None:
        self.returncode = returncode


def settle_remote_session(host, *, monitoring: bool = False):
    """Drive the real attempt protocol to a settled READY/MONITORING session.

    The scaffold takes no shortcut past the machinery under test: the
    generation, token and cleanup claim come from the host's own attempt start,
    the bundle is installed through the host's own commit, and READY is
    published by `commit_remote_ready`. Only the SSH process is substituted, so
    the resource gate and the startup state machine are the production ones and
    a test that wants an unsettled remote simply stops earlier.
    """

    generation = host._begin_remote_attempt()
    resources = HOST.RemoteAttemptResources(
        generation,
        host.remote_profile,
        host.remote_session_token,
        OwnedTunnel(),
        None,
        host.remote_attempt_claim,
    )
    assert host._advance_remote_startup(generation, "STARTING_TUNNEL")
    assert host._commit_remote_attempt_resources(resources, "WAITING_REMOTE_READY")
    assert host._advance_remote_startup(generation, "VERIFYING_AUTHORITY")
    assert HOST.commit_remote_ready(host, generation)
    if monitoring:
        assert host.remote_startup.mark_monitor_started(str(generation))
    return resources


def build_host(*, remote: bool, settled: bool = True):
    """A host with the whole remote slice published, as production builds it."""

    host = object.__new__(HOST.WorkStackDesktopHost)
    HOST.initialize_remote_attempt_state(
        host, HOST.RemoteStartupStateMachine(observer=host._publish_remote_startup_state)
    )
    host.downloaded_update = None
    host.install_update_on_exit = False
    host.update_preferences = types.SimpleNamespace(auto_download=False, install_on_exit=False)
    host._set_update_status = mock.Mock()
    if remote:
        host.remote_profile = HOST.RemoteConnectionProfile(
            "work-linux", "/app", "/ssot", 18765, WORKSPACE_ID
        )
        host.workstack_url = "http://127.0.0.1:18765/"
        if settled:
            settle_remote_session(host)
    else:
        host.remote_profile = None
        host.workstack_url = "http://127.0.0.1:8765/"
    return host


def close_owned_session(host) -> None:
    """Run the real intentional close with its outward effects substituted."""

    host._request_remote_stop_owned = mock.Mock()
    host._stop_remote_monitor = mock.Mock()
    host._terminate_owned_process = mock.Mock()
    HOST.stop_owned_connection(host)


def remembered(host) -> tuple[object, object]:
    """Whatever the host currently believes about the remote build."""

    return (
        getattr(host, "remote_product_version", None),
        getattr(host, "remote_protocol_version", None),
    )


def binding(host) -> tuple[object, ...]:
    """The session and rebind state the reporting probe must never touch."""

    return (
        host.remote_profile,
        host.workstack_url,
        host.remote_session_token,
        host.remote_rebind_target,
        host.remote_rebind_deadline,
        getattr(host, "remote_recovery_required", None),
        host.remote_attempt_gate.current_locked(),
        host.remote_startup.state,
        host.remote_ready_attempt_id,
    )


def run_check(host, manifest, **urlopen: object) -> None:
    """Drive one update check with the network fully substituted."""

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(HOST, "fetch_url_bytes", return_value=b"manifest"))
        stack.enter_context(mock.patch.object(HOST, "parse_update_manifest", return_value=manifest))
        if urlopen:
            stack.enter_context(mock.patch.object(HOST.urllib.request, "urlopen", **urlopen))
        host._check_update_worker(force_download=False)


def reported(host) -> tuple[str, str]:
    call = host._set_update_status.call_args
    return call.args[0], call.kwargs["message"]


class LocalDesktopReportingTests(unittest.TestCase):
    def test_local_current_desktop_keeps_the_existing_verdict_without_a_remote_probe(self) -> None:
        host = build_host(remote=False)

        with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
            run_check(host, current_manifest())

        urlopen.assert_not_called()
        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertEqual("Work Stack is up to date", message)


class RemoteDesktopReportingTests(unittest.TestCase):
    def test_matching_remote_version_is_reported_without_claiming_identical_files(self) -> None:
        host = build_host(remote=True)

        run_check(host, current_manifest(), return_value=storage_response(DESKTOP_VERSION))

        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertNotEqual("Work Stack is up to date", message)
        self.assertIn(f"Work Stack desktop {DESKTOP_VERSION} is up to date", message)
        self.assertIn(f"connected remote server reports {DESKTOP_VERSION}", message)
        self.assertIn("do not prove", message)

    def test_older_remote_server_is_named_and_not_covered_by_the_desktop_verdict(self) -> None:
        host = build_host(remote=True)

        run_check(host, current_manifest(), return_value=storage_response("1.0.5"))

        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertIn("connected remote server reports 1.0.5", message)
        self.assertIn(f"does not match desktop {DESKTOP_VERSION}", message)
        self.assertIn("replaces the Windows desktop only", message)

    def test_unverifiable_remote_metadata_stays_unknown_instead_of_up_to_date(self) -> None:
        host = build_host(remote=True)

        run_check(host, current_manifest(), side_effect=urllib.error.URLError("connection refused"))

        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertNotIn("Work Stack is up to date", message)
        self.assertIn("could not be verified", message)
        self.assertIn("unknown", message)
        self.assertNotIn("refused", message)
        self.assertNotIn("127.0.0.1", message)

    def test_invalid_remote_metadata_is_reported_as_unknown_without_raw_detail(self) -> None:
        host = build_host(remote=True)
        response = mock.MagicMock()
        response.read.return_value = b'{"data": {"workspace_id": "not-a-uuid"}}'
        response.__enter__.return_value = response

        run_check(host, current_manifest(), return_value=response)

        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertIn("could not be verified", message)
        self.assertNotIn("not-a-uuid", message)

    def test_newer_installed_desktop_still_reports_the_connected_remote(self) -> None:
        host = build_host(remote=True)
        rollback = HOST.OlderUpdateManifest("1.0.5", DESKTOP_VERSION)

        with (
            mock.patch.object(HOST, "fetch_url_bytes", return_value=b"manifest"),
            mock.patch.object(HOST, "parse_update_manifest", side_effect=rollback),
            mock.patch.object(
                HOST.urllib.request, "urlopen", return_value=storage_response("1.0.5")
            ),
        ):
            host._check_update_worker(force_download=False)

        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertIn(
            f"Installed Work Stack {DESKTOP_VERSION} is newer than the stable channel 1.0.5",
            message,
        )
        self.assertIn("connected remote server reports 1.0.5", message)

    def test_older_stable_channel_without_a_remote_keeps_the_established_wording(self) -> None:
        host = build_host(remote=False)
        rollback = HOST.OlderUpdateManifest("1.0.5", DESKTOP_VERSION)

        with (
            mock.patch.object(HOST, "fetch_url_bytes", return_value=b"manifest"),
            mock.patch.object(HOST, "parse_update_manifest", side_effect=rollback),
        ):
            host._check_update_worker(force_download=False)

        self.assertEqual(
            f"Installed Work Stack {DESKTOP_VERSION} is newer than the stable channel 1.0.5",
            reported(host)[1],
        )

    def test_remote_probe_failure_does_not_disturb_local_update_bookkeeping(self) -> None:
        host = build_host(remote=True)
        host.downloaded_update = object()
        host.install_update_on_exit = True

        run_check(host, current_manifest(), side_effect=OSError("socket closed"))

        self.assertIsNone(host.downloaded_update)
        self.assertFalse(host.install_update_on_exit)
        self.assertEqual("current", reported(host)[0])


class ProtocolVersusBuildTests(unittest.TestCase):
    """The protocol floor is a compatibility gate; it never proves equal builds."""

    def test_satisfied_protocol_is_described_as_compatibility_only(self) -> None:
        clause = STATUS.protocol_clause(2, 1)

        self.assertIn("meets the required 1", clause)
        self.assertIn("not the same build", clause)

    def test_unsatisfied_protocol_names_the_shortfall(self) -> None:
        self.assertIn("below the required 2", STATUS.protocol_clause(1, 2))

    def test_unknown_protocol_or_minimum_adds_no_claim(self) -> None:
        for actual, minimum in ((None, 1), (1, None), (True, 1), (1, True)):
            with self.subTest(actual=actual, minimum=minimum):
                self.assertEqual("", STATUS.protocol_clause(actual, minimum))

    def test_existing_remote_protocol_gate_is_unchanged_and_needs_no_network(self) -> None:
        metadata = {
            "workspace_id": WORKSPACE_ID,
            "product_version": "1.0.5",
            "remote_protocol_version": 1,
        }

        HOST.WorkStackDesktopHost._require_supported_remote_protocol(metadata, minimum=1)
        with self.assertRaises(HOST.RemoteAuthorityMismatch) as raised:
            HOST.WorkStackDesktopHost._require_supported_remote_protocol(
                metadata, minimum=2, purpose="update to Work Stack 1.0.7"
            )

        self.assertIn("reports protocol 1, but update to Work Stack 1.0.7", str(raised.exception))

    def test_a_current_desktop_reports_a_lagging_remote_protocol_without_blocking(self) -> None:
        host = build_host(remote=True)

        run_check(
            host,
            current_manifest(minimum_remote_protocol=2),
            return_value=storage_response("1.0.5", 1),
        )

        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertIn("Remote protocol 1 is below the required 2", message)


class ServedUiVersionTests(unittest.TestCase):
    """Served UI is a third version fact, never a desktop or remote alias."""

    def test_an_unknown_served_ui_stays_unknown(self) -> None:
        self.assertEqual(
            "The served UI version is unknown.",
            STATUS.served_ui_clause(served_ui_version=None, remote_version="1.0.13"),
        )
        self.assertEqual(
            "The served UI version is unknown.",
            STATUS.served_ui_clause(served_ui_version="1.0.13<script>", remote_version="1.0.13"),
        )

    def test_a_matching_served_ui_does_not_claim_rebuilt_files(self) -> None:
        message = STATUS.served_ui_clause(served_ui_version="1.0.13", remote_version="1.0.13")
        self.assertIn("served UI reports 1.0.13", message)
        self.assertIn("does not prove", message)

    def test_a_mismatch_names_both_observed_versions(self) -> None:
        message = STATUS.served_ui_clause(served_ui_version="1.0.5", remote_version="1.0.13")
        self.assertIn("served UI reports 1.0.5", message)
        self.assertIn("does not match remote 1.0.13", message)

    def test_served_ui_without_a_remote_is_not_a_desktop_verdict(self) -> None:
        message = STATUS.served_ui_clause(served_ui_version="1.0.13")
        self.assertIn("served UI reports 1.0.13", message)
        self.assertIn("not a desktop verdict", message)
        self.assertNotIn("up to date", message)


class VersionContractTests(unittest.TestCase):
    """A displayed version is the whole reported value or it is unknown."""

    def test_only_a_whole_bounded_three_part_version_is_accepted(self) -> None:
        self.assertEqual("1.0.5", STATUS.canonical_version("1.0.5"))
        self.assertEqual("0.0.0", STATUS.canonical_version("0.0.0"))
        for rejected in (
            None,
            "   ",
            "1.0.5\n<script>",
            "1.0.5-rc1",
            "1.0",
            "1.0.5.1",
            "01.0.5",
            "v1.0.5",
            " 1.0.5",
            "1.0.5 ",
            "9" * 200,
            "1" * 30 + ".0.5",
        ):
            with self.subTest(rejected=rejected):
                self.assertEqual("", STATUS.canonical_version(rejected))

    def test_a_malformed_version_whose_prefix_equals_the_desktop_is_never_truncated(self) -> None:
        """`1.0.13<script>` must not become a credible equal `1.0.13`."""

        message = STATUS.current_version_message(
            desktop_version="1.0.13",
            remote_connected=True,
            remote_version="1.0.13<script>",
        )

        self.assertIn("could not be verified", message)
        self.assertNotIn("reports 1.0.13", message)
        self.assertNotIn("do not prove", message)
        self.assertNotIn("<script>", message)

    def test_an_unprintable_remote_version_degrades_to_unknown_rather_than_a_match(self) -> None:
        message = STATUS.current_version_message(
            desktop_version="1.0.13",
            remote_connected=True,
            remote_version="\u0000\u0007 ",
        )

        self.assertIn("could not be verified", message)

    def test_the_composed_message_fits_the_status_field_budget(self) -> None:
        longest = "1" * 10 + "." + "2" * 10 + "." + "3" * 10
        self.assertEqual(STATUS.MAX_VERSION_CHARACTERS, len(longest))
        message = STATUS.current_version_message(
            desktop_version="1.0.13",
            remote_connected=True,
            remote_version=longest,
            remote_protocol=1,
            minimum_protocol=2,
        )

        self.assertEqual(longest, STATUS.canonical_version(longest))
        self.assertLessEqual(len(message), 500)


class RemoteAuthorityBindingTests(unittest.TestCase):
    """A reported version must be provably the selected remote's own."""

    def test_a_malformed_remote_version_cannot_reach_the_equal_version_branch(self) -> None:
        host = build_host(remote=True)

        run_check(
            host,
            current_manifest(),
            return_value=storage_response(f"{DESKTOP_VERSION}<script>"),
        )

        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertIn("could not be verified", message)
        self.assertNotIn(f"reports {DESKTOP_VERSION}", message)
        self.assertNotIn("<script>", message)

    def test_a_valid_version_from_another_workspace_is_reported_as_unknown(self) -> None:
        host = build_host(remote=True)
        before = binding(host)
        before_caches = remembered(host)

        run_check(
            host,
            current_manifest(),
            return_value=storage_response(DESKTOP_VERSION, workspace_id=OTHER_WORKSPACE_ID),
        )

        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertIn("could not be verified", message)
        self.assertNotIn("do not prove", message)
        self.assertNotIn(OTHER_WORKSPACE_ID, message)
        self.assertEqual(before_caches, remembered(host))
        self.assertEqual(before, binding(host))

    def test_a_replaced_session_during_the_probe_discards_the_answer(self) -> None:
        host = build_host(remote=True)

        def answer_then_replace_the_session(*args: object, **kwargs: object):
            host.remote_session_token = "session-b"
            return storage_response(DESKTOP_VERSION)

        run_check(host, current_manifest(), side_effect=answer_then_replace_the_session)

        self.assertIn("could not be verified", reported(host)[1])

    def test_an_endpoint_that_moves_during_the_probe_discards_the_answer(self) -> None:
        host = build_host(remote=True)

        def answer_then_move_the_endpoint(*args: object, **kwargs: object):
            host.workstack_url = "http://127.0.0.1:19000/"
            return storage_response(DESKTOP_VERSION)

        run_check(host, current_manifest(), side_effect=answer_then_move_the_endpoint)

        self.assertIn("could not be verified", reported(host)[1])

    def test_a_rebind_that_begins_during_the_probe_discards_the_answer(self) -> None:
        host = build_host(remote=True)

        def answer_then_begin_a_rebind(*args: object, **kwargs: object):
            host.remote_rebind_target = OTHER_WORKSPACE_ID
            host.remote_rebind_deadline = HOST.time.monotonic() + 60.0
            return storage_response(DESKTOP_VERSION)

        run_check(host, current_manifest(), side_effect=answer_then_begin_a_rebind)

        self.assertIn("could not be verified", reported(host)[1])

    def test_an_active_rebind_coordination_is_never_probed_at_all(self) -> None:
        host = build_host(remote=True)
        host.remote_rebind_target = OTHER_WORKSPACE_ID
        host.remote_rebind_deadline = HOST.time.monotonic() + 60.0

        with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
            run_check(host, current_manifest())

        urlopen.assert_not_called()
        self.assertIn("could not be verified", reported(host)[1])
        self.assertEqual(OTHER_WORKSPACE_ID, host.remote_rebind_target)

    def test_a_remote_already_flagged_for_recovery_is_not_spoken_for(self) -> None:
        host = build_host(remote=True)
        host.remote_recovery_required = HOST.threading.Event()
        host.remote_recovery_required.set()

        with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
            run_check(host, current_manifest())

        urlopen.assert_not_called()
        self.assertIn("could not be verified", reported(host)[1])

    def test_a_successful_report_still_writes_no_remote_or_session_state(self) -> None:
        host = build_host(remote=True)
        before = binding(host)
        before_caches = remembered(host)

        run_check(host, current_manifest(), return_value=storage_response(DESKTOP_VERSION))

        self.assertIn(f"reports {DESKTOP_VERSION}", reported(host)[1])
        self.assertEqual(before_caches, remembered(host))
        self.assertEqual(before, binding(host))


class SettledSessionAuthorityTests(unittest.TestCase):
    """A configured profile is not a session, and a token is not a tunnel.

    `start_remote_attempt` publishes a session token while entering PROBING,
    before any tunnel exists, and an intentional close leaves the profile in
    place with no token at all. Neither position may turn whatever answers the
    forwarded port into a credible statement about the connected remote.
    """

    def assert_unknown_without_a_probe(self, host) -> None:
        before = binding(host)
        before_caches = remembered(host)

        with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
            run_check(host, current_manifest())

        urlopen.assert_not_called()
        state, message = reported(host)
        self.assertEqual("current", state)
        self.assertIn("could not be verified", message)
        self.assertNotIn("do not prove", message)
        self.assertNotIn(f"reports {DESKTOP_VERSION}", message)
        self.assertEqual(before_caches, remembered(host))
        self.assertEqual(before, binding(host))

    def test_a_configured_profile_without_an_attempt_is_never_probed_at_all(self) -> None:
        host = build_host(remote=True, settled=False)

        self.assertEqual("IDLE", host.remote_lifecycle_state)
        self.assertIsNone(host.remote_session_token)
        self.assert_unknown_without_a_probe(host)

    def test_a_probing_attempt_with_a_fresh_token_is_not_yet_a_session(self) -> None:
        host = build_host(remote=True, settled=False)
        host._begin_remote_attempt()

        self.assertEqual("PROBING", host.remote_lifecycle_state)
        self.assertTrue(host.remote_session_token)
        self.assert_unknown_without_a_probe(host)

    def test_a_stopped_session_leaves_no_authority_to_speak_for(self) -> None:
        host = build_host(remote=True)
        close_owned_session(host)

        self.assertEqual("STOPPED", host.remote_lifecycle_state)
        self.assertIsNone(host.remote_session_token)
        self.assert_unknown_without_a_probe(host)

    def test_a_failed_attempt_is_not_reported_as_a_connected_remote(self) -> None:
        host = build_host(remote=True)
        host._fail_remote_attempt(host.remote_attempt_id, "tunnel lost")

        self.assertEqual("FAILED", host.remote_lifecycle_state)
        self.assert_unknown_without_a_probe(host)

    def test_a_host_without_the_attempt_protocol_reports_unknown(self) -> None:
        host = build_host(remote=True)
        del host.remote_attempt_gate

        with mock.patch.object(HOST.urllib.request, "urlopen") as urlopen:
            run_check(host, current_manifest())

        urlopen.assert_not_called()
        self.assertIn("could not be verified", reported(host)[1])

    def test_a_monitoring_session_still_reports_the_connected_remote(self) -> None:
        host = build_host(remote=True, settled=False)
        settle_remote_session(host, monitoring=True)
        before = binding(host)

        run_check(host, current_manifest(), return_value=storage_response(DESKTOP_VERSION))

        state, message = reported(host)
        self.assertEqual("MONITORING", host.remote_lifecycle_state)
        self.assertEqual("current", state)
        self.assertIn(f"connected remote server reports {DESKTOP_VERSION}", message)
        self.assertEqual(before, binding(host))

    def test_a_ready_session_reports_the_remote_it_actually_owns(self) -> None:
        host = build_host(remote=True)

        run_check(host, current_manifest(), return_value=storage_response(DESKTOP_VERSION))

        self.assertEqual("READY", host.remote_lifecycle_state)
        self.assertIn(f"connected remote server reports {DESKTOP_VERSION}", reported(host)[1])

    def test_a_tunnel_that_dies_during_the_probe_discards_the_answer(self) -> None:
        host = build_host(remote=True)
        tunnel = host.remote_ssh_process

        def answer_then_lose_the_tunnel(*args: object, **kwargs: object):
            tunnel.exit()
            return storage_response(DESKTOP_VERSION)

        run_check(host, current_manifest(), side_effect=answer_then_lose_the_tunnel)

        self.assertIn("could not be verified", reported(host)[1])
        self.assertNotIn(f"reports {DESKTOP_VERSION}", reported(host)[1])

    def test_neither_lock_is_held_while_the_probe_waits_on_the_network(self) -> None:
        """The snapshot nests the lifecycle lock inside the authority guard.

        Both are released before the read, so an unrelated thread can take them
        in that same order while the probe is blocked on the endpoint.
        """

        host = build_host(remote=True)
        acquired: list[str] = []

        def answer_after_a_foreign_acquisition(*args: object, **kwargs: object):
            def take_both_locks() -> None:
                with host._remote_authority_guard():
                    with HOST.resource_lock_for(host):
                        acquired.append("both")

            thread = HOST.threading.Thread(target=take_both_locks)
            thread.start()
            thread.join(timeout=5.0)
            return storage_response(DESKTOP_VERSION)

        run_check(host, current_manifest(), side_effect=answer_after_a_foreign_acquisition)

        self.assertEqual(["both"], acquired)
        self.assertIn(f"connected remote server reports {DESKTOP_VERSION}", reported(host)[1])

    def test_a_new_attempt_started_during_the_probe_discards_the_answer(self) -> None:
        host = build_host(remote=True)
        installed = host.remote_attempt_gate.current_locked()

        def answer_then_begin_a_new_attempt(*args: object, **kwargs: object):
            host._begin_remote_attempt()
            return storage_response(DESKTOP_VERSION)

        run_check(host, current_manifest(), side_effect=answer_then_begin_a_new_attempt)

        self.assertIn("could not be verified", reported(host)[1])
        self.assertIs(installed, host.remote_attempt_gate.current_locked())


if __name__ == "__main__":
    unittest.main()

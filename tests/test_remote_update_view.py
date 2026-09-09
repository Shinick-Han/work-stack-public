"""Remote update view: R6 snapshot in, one mountable page out.

The page has to be something the existing desktop shell can load, not a pile of
unmounted strings: same native HTML pattern as startup recovery, English copy,
one next action, honest Unknown, and no raw extras. These tests drive the
normalizer, the presentation, the HTML, and the request admission with
synthetic snapshots only.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_update_view as VIEW


CAPABILITY = "0123456789abcdef" * 4
REQUEST_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def quiescent_owner() -> dict[str, str]:
    return {"state": "dead", "process_exit": "verified",
            "listener_release": "verified", "lease_release": "verified"}


def snapshot(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": VIEW.SCHEMA_VERSION,
        "stage": "idle",
        "code": "unknown",
        "versions": {
            "desktop": "1.0.13",
            "remote": None,
            "served_ui": None,
            "protocol": None,
            "schema": None,
        },
        "owner": {
            "state": "unknown",
            "token_available": None,
            "pidfd_available": None,
            "process_exit": "unknown",
            "listener_release": "unknown",
            "lease_release": "unknown",
        },
        "install": {"capability": "unknown", "method": "unknown"},
        "backup": {"status": "unknown", "migration_required": None},
        "actions": [],
    }
    for key, value in overrides.items():
        if key in {"versions", "owner", "install", "backup"} and isinstance(value, dict):
            merged = dict(document[key])
            merged.update(value)
            document[key] = merged
        else:
            document[key] = value
    return document


def present(**overrides: object) -> VIEW.RemoteUpdatePresentation:
    return VIEW.present_remote_update(snapshot(**overrides))


def page(**overrides: object) -> str:
    return VIEW.build_remote_update_html(
        snapshot(**overrides), capability=CAPABILITY, theme="dark"
    )


def request(operation: str, capability: str = CAPABILITY, **extra: object) -> str:
    value: dict[str, object] = {
        "type": VIEW.REMOTE_UPDATE_REQUEST_TYPE,
        "schema_version": 1,
        "request_id": REQUEST_ID,
        "capability": capability,
        "operation": operation,
    }
    value.update(extra)
    return json.dumps(value)


class NormalizeTests(unittest.TestCase):
    def test_absent_and_invalid_snapshots_become_unknown(self) -> None:
        for raw in (None, "", 1, [], {}, {"schema_version": "nope"}):
            with self.subTest(raw=raw):
                snap = VIEW.normalize_remote_update_snapshot(raw)
                self.assertEqual("unknown", snap.stage)
                self.assertEqual("unknown", snap.owner.state)
                self.assertIsNone(snap.versions.desktop)
                self.assertEqual((), snap.actions)

    def test_clarification_accepts_stop_and_backup_as_separate_stages(self) -> None:
        for stage in ("stop", "backup"):
            snap = VIEW.normalize_remote_update_snapshot(snapshot(stage=stage))
            self.assertEqual(stage, snap.stage)

    def test_legacy_slash_stage_is_unknown_not_a_new_literal(self) -> None:
        snap = VIEW.normalize_remote_update_snapshot(snapshot(stage="backup/stop"))
        self.assertEqual("unknown", snap.stage)

    def test_malformed_versions_and_bools_do_not_become_facts(self) -> None:
        snap = VIEW.normalize_remote_update_snapshot(
            snapshot(
                versions={
                    "desktop": "1.0.13<script>",
                    "remote": "v1.0.13",
                    "served_ui": 1,
                    "protocol": "1<script>",
                    "schema": "../etc",
                },
                owner={"token_available": 1, "pidfd_available": "yes", "state": "LIVE"},
                backup={"migration_required": 0, "status": "done"},
                actions=["stop_owner", "rm -rf", "stop_owner", "<script>"],
            )
        )
        self.assertIsNone(snap.versions.desktop)
        self.assertIsNone(snap.versions.remote)
        self.assertIsNone(snap.versions.served_ui)
        self.assertIsNone(snap.versions.protocol)
        self.assertIsNone(snap.versions.schema)
        self.assertEqual("unknown", snap.owner.state)
        self.assertIsNone(snap.owner.token_available)
        self.assertIsNone(snap.backup.migration_required)
        self.assertEqual(("stop_owner",), snap.actions)

    def test_extra_raw_keys_are_dropped(self) -> None:
        raw = snapshot(
            token="secret-token",
            argv=["python", "-c", "print(1)"],
            path="/var/lib/workstack/ssot",
            pid=4242,
        )
        snap = VIEW.normalize_remote_update_snapshot(raw)
        self.assertFalse(hasattr(snap, "token"))
        self.assertNotIn("secret-token", VIEW.build_remote_update_html(raw, capability=CAPABILITY))


class PresentationVersionTests(unittest.TestCase):
    def test_local_only_desktop_does_not_invent_a_connected_server(self) -> None:
        view = present(code="local_only", versions={"desktop": "1.0.13"})
        self.assertEqual("This PC is current.", view.headline)
        self.assertEqual("Not connected", view.server_version)
        self.assertEqual("No connected server", view.server_status)
        self.assertEqual("Not connected", view.served_ui_version)
        self.assertFalse(view.owner_visible)
        self.assertFalse(view.desktop_independent)
        self.assertIsNone(view.next_action)

    def test_unknown_remote_is_not_current_and_does_not_block_this_pc(self) -> None:
        view = present(
            code="remote_offline",
            versions={"desktop": "1.0.13", "remote": None},
            actions=["download_this_pc"],
        )
        self.assertIn("unknown", view.headline.lower())
        self.assertEqual("Unknown", view.server_version)
        self.assertEqual("Unknown", view.server_status)
        self.assertNotIn("up to date", view.headline.lower())
        self.assertNotIn("up to date", view.detail.lower())
        self.assertEqual("download_this_pc", view.next_action)
        self.assertTrue(view.desktop_independent)
        self.assertTrue(any("does not block" in note for note in view.notes))

    def test_matching_remote_and_desktop_numbers_are_not_a_rebuild(self) -> None:
        view = present(
            versions={"desktop": "1.0.13", "remote": "1.0.13", "served_ui": "1.0.13"}
        )
        self.assertIn("not a proven rebuild", view.server_status)
        self.assertIn("not a proven rebuild", view.served_ui_status)

    def test_mismatch_keeps_desktop_remote_and_served_ui_distinct(self) -> None:
        view = present(
            code="remote_mismatch",
            versions={"desktop": "1.0.13", "remote": "1.0.5", "served_ui": "1.0.4"},
        )
        self.assertEqual("1.0.13", view.pc_version)
        self.assertEqual("1.0.5", view.server_version)
        self.assertEqual("1.0.4", view.served_ui_version)
        self.assertIn("Different from this PC", view.server_status)
        self.assertIn("Different from the remote product", view.served_ui_status)
        self.assertIn("different builds", view.headline)

    def test_served_ui_unknown_is_not_copied_from_the_remote_product(self) -> None:
        view = present(versions={"desktop": "1.0.13", "remote": "1.0.13", "served_ui": None})
        self.assertEqual("Unknown", view.served_ui_version)
        self.assertEqual("Unknown", view.served_ui_status)
        self.assertNotEqual(view.pc_version, view.served_ui_version)

    def test_served_ui_digest_is_not_treated_as_a_product_version(self) -> None:
        digest = "sha256:" + "ab" * 32
        view = present(
            versions={"desktop": "1.0.13", "remote": "1.0.13", "served_ui": digest}
        )
        self.assertEqual("Observed digest", view.served_ui_version)
        self.assertIn("not inferred", view.served_ui_status)
        # The observer digests the served HTML entrypoint, so the row may not
        # claim the packaged frontend it never measured.
        self.assertIn("Observed HTML entrypoint identity", view.served_ui_status)
        self.assertNotIn("Packaged", view.served_ui_status)
        self.assertEqual(digest, dict(view.diagnostics)["Served UI identity"])
        self.assertNotIn(digest, VIEW.build_remote_update_html(
            snapshot(versions={"desktop": "1.0.13", "remote": "1.0.13", "served_ui": digest}),
            capability=CAPABILITY,
        ).partition("<details")[0])


class PresentationOwnerTests(unittest.TestCase):
    def test_live_owner_and_token_are_separate_facts_with_a_stop_action(self) -> None:
        view = present(
            code="remote_lock_owned",
            owner={"state": "live", "token_available": True},
            actions=["stop_owner"],
        )
        self.assertEqual("Live", view.owner_state)
        self.assertEqual("Available", view.token_availability)
        self.assertEqual("stop_owner", view.next_action)
        self.assertIn("live session", view.headline)
        self.assertIn("separately", view.detail)

    def test_missing_token_is_not_legacy_and_does_not_offer_stop(self) -> None:
        view = present(
            code="token_missing",
            owner={"state": "live", "token_available": False},
            actions=["stop_owner", "check_this_pc"],
        )
        self.assertEqual("Live", view.owner_state)
        self.assertEqual("Not available", view.token_availability)
        self.assertNotEqual("stop_owner", view.next_action)
        self.assertIn("not a legacy unfenced receipt", view.detail)
        self.assertEqual("check_this_pc", view.next_action)

    def test_foreign_owner_never_offers_stop_or_force_recovery(self) -> None:
        view = present(
            code="owner_foreign",
            owner={"state": "foreign", "token_available": True},
            actions=["stop_owner", "update_connected_server"],
        )
        self.assertEqual("Foreign", view.owner_state)
        self.assertNotEqual("stop_owner", view.next_action)
        self.assertNotEqual("update_connected_server", view.next_action)
        self.assertIn("foreign", view.headline.lower())
        self.assertIn("will not force recovery", view.detail)

    def test_unfenced_is_not_missing_token_and_not_foreign(self) -> None:
        view = present(
            code="owner_unfenced",
            owner={"state": "unfenced", "token_available": False},
        )
        self.assertEqual("Unfenced", view.owner_state)
        self.assertEqual("Not available", view.token_availability)
        self.assertIn("unfenced owner receipt", view.headline)
        self.assertNotIn("foreign", view.headline.lower())
        self.assertNotEqual("stop_owner", view.next_action)

    def test_stopping_is_not_a_completed_shutdown(self) -> None:
        view = present(
            stage="stop",
            code="owner_stopping",
            owner={
                "state": "stopping",
                "token_available": True,
                "process_exit": "unknown",
                "listener_release": "unknown",
                "lease_release": "unknown",
            },
            actions=["stop_owner"],
        )
        self.assertIsNone(view.next_action)
        self.assertIn("not a completed shutdown", view.headline)
        self.assertIn("spawned stop command is not success", view.detail)

    def test_dead_owner_can_preview_a_server_update(self) -> None:
        view = present(
            code="owner_dead",
            owner=dict(quiescent_owner(), token_available=False),
            actions=["update_connected_server"],
        )
        self.assertEqual("Dead", view.owner_state)
        self.assertEqual("update_connected_server", view.next_action)


class PresentationFlowTests(unittest.TestCase):
    def test_failed_with_verified_backup_offers_explicit_restore(self) -> None:
        view = present(
            stage="failed",
            code="failed",
            owner=quiescent_owner(),
            backup={"status": "verified", "migration_required": True},
            actions=["restore_backup"],
        )
        self.assertEqual("restore_backup", view.next_action)
        self.assertIn("did not finish", view.headline)
        self.assertTrue(any("migration is required" in note for note in view.notes))
        self.assertTrue(any("Backup is verified" in note for note in view.notes))

    def test_cancelled_and_unknown_do_not_invent_a_mutating_action(self) -> None:
        cancelled = present(stage="cancelled", code="cancelled")
        unknown = present(stage="unknown", code="remote_unknown")
        self.assertIsNone(cancelled.next_action)
        self.assertIsNone(unknown.next_action)
        self.assertIn("cancelled", cancelled.headline)
        self.assertIn("unknown", unknown.headline.lower())

    def test_backup_not_run_is_visible_before_prepare(self) -> None:
        view = present(
            stage="backup",
            code="backup_required",
            owner=quiescent_owner(),
            backup={"status": "not_run"},
            actions=["continue_flow"],
        )
        self.assertTrue(any("verified backup has not been run" in note for note in view.notes))
        self.assertEqual("continue_flow", view.next_action)

    def test_nfs_alternative_does_not_claim_all_atomic_operations_fail(self) -> None:
        view = present(
            code="nfs_alternative",
            owner={"state": "dead"},
            install={"capability": "unavailable", "method": "verified_unpack"},
        )
        joined = " ".join(view.notes)
        self.assertIn("Verified unpack", joined)
        self.assertIn("does not mean every NFS atomic operation is unsupported", joined)

    def test_unknown_install_capability_stays_unknown(self) -> None:
        view = present(
            stage="preview",
            owner={"state": "dead"},
            install={"capability": "unknown", "method": "unknown"},
        )
        self.assertTrue(any(note == "Install capability is unknown." for note in view.notes))

    def test_protocol_below_does_not_block_this_pc_update(self) -> None:
        view = present(
            code="protocol_below",
            versions={"desktop": "1.0.13", "remote": "1.0.5", "protocol": "1", "schema": "5"},
            actions=["update_this_pc"],
        )
        self.assertEqual("update_this_pc", view.next_action)
        self.assertTrue(any("does not block" in note for note in view.notes))
        labels = dict(view.diagnostics)
        self.assertEqual("1", labels["Protocol"])
        self.assertEqual("5", labels["Schema"])

    def test_activate_stage_offers_restart_not_a_new_mutation(self) -> None:
        view = present(
            stage="activate",
            owner=quiescent_owner(),
            backup={"status": "verified"},
            actions=["continue_flow", "restart"],
        )
        self.assertEqual("restart", view.next_action)
        self.assertIn("Restart is required", view.headline)
        self.assertIn("same pending activation receipt", view.detail)
        html = page(
            stage="activate",
            owner=quiescent_owner(),
            backup={"status": "verified"},
            actions=["continue_flow", "restart"],
        )
        self.assertIn('data-operation="restart"', html)
        self.assertNotIn('data-operation="continue_flow"', html)

    def test_snapshot_action_order_wins_when_admissible(self) -> None:
        view = present(
            code="desktop_available",
            owner={"state": "live", "token_available": True},
            actions=["update_this_pc", "stop_owner"],
        )
        self.assertEqual("update_this_pc", view.next_action)


class HtmlMountTests(unittest.TestCase):
    def test_page_is_a_real_shell_document_with_one_primary_action(self) -> None:
        html = page(
            code="remote_lock_owned",
            owner={"state": "live", "token_available": True},
            actions=["stop_owner"],
        )
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn('lang="en"', html)
        self.assertIn('id="primary"', html)
        self.assertIn('data-operation="stop_owner"', html)
        self.assertIn("Stop this session's owner", html)
        self.assertIn("Update this PC", html)
        self.assertIn("Update connected server", html)
        self.assertIn("Served UI", html)
        self.assertIn("window.chrome.webview.postMessage", html)
        self.assertIn(VIEW.REMOTE_UPDATE_REQUEST_TYPE, html)
        self.assertIn(CAPABILITY, html)
        self.assertIn('id="close"', html)
        self.assertIn("<details>", html)
        self.assertIn("Diagnostics", html)
        self.assertIn("min-height:44px", html)
        self.assertIn("focus-visible", html)

    def test_default_screen_hides_protocol_schema_and_shutdown_evidence(self) -> None:
        html = page(
            versions={"desktop": "1.0.13", "remote": "1.0.5", "protocol": "2", "schema": "6"},
            owner={
                "state": "dead",
                "pidfd_available": False,
                "process_exit": "verified",
                "listener_release": "failed",
                "lease_release": "unknown",
            },
        )
        pre, _sep, details = html.partition("<details")
        self.assertNotIn("Protocol", pre)
        self.assertNotIn("Schema", pre)
        self.assertNotIn("pidfd", pre)
        self.assertNotIn("Process exit", pre)
        self.assertNotIn("Listener release", pre)
        self.assertNotIn("Lease release", pre)
        self.assertIn("Protocol", details)
        self.assertIn(">2<", details)
        self.assertIn("Schema", details)
        self.assertIn("pidfd", details)

    def test_theme_tokens_follow_the_native_shell(self) -> None:
        light = VIEW.build_remote_update_html(snapshot(code="local_only"), capability=CAPABILITY, theme="light")
        dark = VIEW.build_remote_update_html(snapshot(code="local_only"), capability=CAPABILITY, theme="dark")
        unknown = VIEW.build_remote_update_html(
            snapshot(code="local_only"), capability=CAPABILITY, theme="system"
        )
        self.assertIn('content="light"', light)
        self.assertIn('content="dark"', dark)
        self.assertEqual(unknown, dark)
        self.assertNotEqual(light, dark)
        self.assertIn(VIEW.theme_color("light", "bg.app"), light)
        self.assertIn(VIEW.theme_color("dark", "brand.accent"), dark)

    def test_untrusted_strings_cannot_become_html_or_commands(self) -> None:
        probe = '<img src=x onerror="alert(1)">'
        html = VIEW.build_remote_update_html(
            snapshot(
                versions={"desktop": probe, "remote": probe, "served_ui": probe, "protocol": probe},
                token=probe,
                path="C:/secret",
                argv=["cmd.exe", "/c", probe],
            ),
            capability=CAPABILITY,
        )
        self.assertNotIn(probe, html)
        self.assertNotIn("<img", html)
        self.assertNotIn("onerror", html)
        self.assertNotIn("C:/secret", html)
        self.assertNotIn("cmd.exe", html)
        self.assertNotIn("alert(1)", html)

    def test_invalid_capability_refuses_to_render(self) -> None:
        with self.assertRaises(ValueError):
            VIEW.build_remote_update_html(snapshot(), capability="nope")

    def test_wait_states_have_close_but_no_primary_mutation(self) -> None:
        html = page(owner={"state": "stopping", "token_available": True})
        self.assertIn('id="close"', html)
        self.assertNotIn('id="primary"', html)
        self.assertNotIn("data-operation=\"stop_owner\"", html)


class RequestAndSessionTests(unittest.TestCase):
    def test_parser_accepts_only_the_exact_request_shape(self) -> None:
        parsed = VIEW.parse_remote_update_request(request("update_this_pc"))
        self.assertIsNotNone(parsed)
        self.assertEqual("update_this_pc", parsed.operation)
        self.assertEqual(CAPABILITY, parsed.capability)

        for rejected in (
            request("update_this_pc", path="C:/secret"),
            request("rm"),
            request("update_this_pc", capability="00"),
            request("update_this_pc", request_id="latest"),
            "x" * 3000,
            "{",
            json.dumps({"type": VIEW.REMOTE_UPDATE_REQUEST_TYPE}),
        ):
            with self.subTest(rejected=rejected[:40] if isinstance(rejected, str) else rejected):
                self.assertIsNone(VIEW.parse_remote_update_request(rejected))

    def test_session_admits_only_the_offered_action_and_close(self) -> None:
        rendered: list[str] = []
        session = VIEW.RemoteUpdateViewSession()
        self.assertTrue(
            session.show(
                snapshot(
                    code="remote_lock_owned",
                    owner={"state": "live", "token_available": True},
                    actions=["stop_owner"],
                ),
                rendered.append,
                capability=CAPABILITY,
            )
        )
        self.assertTrue(session.admits_navigation(VIEW.NAVIGATE_TO_STRING_PREFIX + "other") is False)
        inlined = VIEW.remote_update_navigation_targets(rendered[0])
        data_target = next(target for target in inlined if target.startswith("data:"))
        self.assertTrue(session.admits_navigation(data_target))
        self.assertFalse(session.admits_navigation(data_target))

        admitted = session.admit(request("stop_owner"))
        self.assertEqual("action", admitted.outcome)
        ignored = session.admit(request("stop_owner"))
        self.assertEqual("ignored", ignored.outcome)
        unbound = session.admit(request("update_this_pc"))
        self.assertEqual("ignored", unbound.outcome)

        session = VIEW.RemoteUpdateViewSession()
        session.show(snapshot(code="local_only"), rendered.append, capability=CAPABILITY)
        foreign = session.admit(request("stop_owner", capability="ab" * 32))
        self.assertEqual("unbound", foreign.outcome)
        closed = session.admit(request("close"))
        self.assertEqual("close", closed.outcome)
        self.assertEqual("unbound", session.admit(request("close")).outcome)

    def test_brand_mark_is_inlined_on_the_page(self) -> None:
        from brand_assets import read_mark_svg

        html = page(code="local_only")
        self.assertIn(read_mark_svg(), html)
        self.assertIn('aria-hidden="true"', html)


def _admit(operation: str, **overrides: object) -> VIEW.RemoteUpdateAdmission:
    rendered: list[str] = []
    session = VIEW.RemoteUpdateViewSession()
    session.show(snapshot(**overrides), rendered.append, capability=CAPABILITY)
    return session.admit(request(operation))


class AdmissionAllowlistTests(unittest.TestCase):
    def test_empty_actions_do_not_synthesize_restore_or_retry(self) -> None:
        verified = present(
            stage="failed",
            code="failed",
            owner={"state": "dead"},
            backup={"status": "verified"},
            actions=[],
        )
        unknown = present(
            stage="failed",
            code="failed",
            owner={"state": "dead"},
            backup={"status": "unknown"},
            actions=[],
        )
        self.assertIsNone(verified.next_action)
        self.assertIsNone(unknown.next_action)
        self.assertEqual(
            "ignored",
            _admit(
                "restore_backup",
                stage="failed",
                code="failed",
                owner={"state": "dead"},
                backup={"status": "verified"},
                actions=[],
            ).outcome,
        )
        self.assertEqual(
            "ignored",
            _admit(
                "retry",
                stage="failed",
                code="failed",
                owner={"state": "dead"},
                backup={"status": "unknown"},
                actions=[],
            ).outcome,
        )

    def test_empty_actions_do_not_synthesize_restart_or_stop(self) -> None:
        activate = present(
            stage="activate",
            owner={"state": "dead"},
            backup={"status": "verified"},
            actions=[],
        )
        live = present(
            code="remote_lock_owned",
            owner={"state": "live", "token_available": True},
            actions=[],
        )
        self.assertIsNone(activate.next_action)
        self.assertIsNone(live.next_action)
        html = page(
            stage="activate",
            owner={"state": "dead"},
            backup={"status": "verified"},
            actions=[],
        )
        self.assertNotIn('id="primary"', html)
        self.assertNotIn('data-operation="restart"', html)
        self.assertEqual(
            "ignored",
            _admit(
                "restart",
                stage="activate",
                owner={"state": "dead"},
                backup={"status": "verified"},
                actions=[],
            ).outcome,
        )
        self.assertEqual(
            "ignored",
            _admit(
                "stop_owner",
                code="remote_lock_owned",
                owner={"state": "live", "token_available": True},
                actions=[],
            ).outcome,
        )

    def test_foreign_preview_refuses_mutation_and_restore(self) -> None:
        update_view = present(
            stage="preview",
            owner={"state": "foreign", "token_available": True},
            actions=["update_connected_server"],
        )
        restore_view = present(
            stage="failed",
            code="failed",
            owner={"state": "foreign"},
            backup={"status": "verified"},
            actions=["restore_backup"],
        )
        self.assertIsNone(update_view.next_action)
        self.assertIsNone(restore_view.next_action)
        self.assertEqual(
            "ignored",
            _admit(
                "update_connected_server",
                stage="preview",
                owner={"state": "foreign", "token_available": True},
                actions=["update_connected_server"],
            ).outcome,
        )
        self.assertEqual(
            "ignored",
            _admit(
                "restore_backup",
                stage="failed",
                code="failed",
                owner={"state": "foreign"},
                backup={"status": "verified"},
                actions=["restore_backup"],
            ).outcome,
        )

    def test_foreign_preview_still_admits_read_only_preview(self) -> None:
        view = present(
            stage="preview",
            owner={"state": "foreign", "token_available": True},
            actions=["preview_server_update", "update_connected_server"],
        )
        self.assertEqual("preview_server_update", view.next_action)
        self.assertEqual(
            "action",
            _admit(
                "preview_server_update",
                stage="preview",
                owner={"state": "foreign", "token_available": True},
                actions=["preview_server_update", "update_connected_server"],
            ).outcome,
        )
        self.assertEqual(
            "ignored",
            _admit(
                "update_connected_server",
                stage="preview",
                owner={"state": "foreign", "token_available": True},
                actions=["preview_server_update", "update_connected_server"],
            ).outcome,
        )

    def test_live_stop_refuses_continue_before_quiescence(self) -> None:
        owner = {
            "state": "live",
            "token_available": True,
            "process_exit": "unknown",
            "listener_release": "unknown",
            "lease_release": "unknown",
        }
        view = present(stage="stop", owner=owner, actions=["continue_flow", "stop_owner"])
        self.assertEqual("stop_owner", view.next_action)
        self.assertEqual(
            "ignored",
            _admit(
                "continue_flow",
                stage="stop",
                owner=owner,
                actions=["continue_flow", "stop_owner"],
            ).outcome,
        )
        self.assertEqual(
            "action",
            _admit(
                "stop_owner",
                stage="stop",
                owner=owner,
                actions=["continue_flow", "stop_owner"],
            ).outcome,
        )

    def test_dead_stop_continue_requires_verified_quiescence(self) -> None:
        unknown = {
            "state": "dead",
            "token_available": False,
            "process_exit": "unknown",
            "listener_release": "unknown",
            "lease_release": "unknown",
        }
        verified = {
            "state": "dead",
            "token_available": False,
            "process_exit": "verified",
            "listener_release": "verified",
            "lease_release": "verified",
        }
        self.assertIsNone(
            present(stage="stop", owner=unknown, actions=["continue_flow"]).next_action
        )
        self.assertEqual(
            "ignored",
            _admit(
                "continue_flow",
                stage="stop",
                owner=unknown,
                actions=["continue_flow"],
            ).outcome,
        )
        self.assertEqual(
            "continue_flow",
            present(stage="stop", owner=verified, actions=["continue_flow"]).next_action,
        )
        self.assertEqual(
            "action",
            _admit(
                "continue_flow",
                stage="stop",
                owner=verified,
                actions=["continue_flow"],
            ).outcome,
        )

    def test_contradictory_local_only_does_not_hide_remote_facts(self) -> None:
        digest = "sha256:" + "cd" * 32
        view = present(
            code="local_only",
            versions={"desktop": "1.0.13", "remote": None, "served_ui": digest},
            owner={"state": "live", "token_available": True},
            actions=[],
        )
        self.assertTrue(view.owner_visible)
        self.assertEqual("Live", view.owner_state)
        self.assertEqual("Available", view.token_availability)
        self.assertEqual("Unknown", view.server_version)
        self.assertNotEqual("Not connected", view.server_version)
        self.assertEqual("Observed digest", view.served_ui_version)
        self.assertNotEqual("Not connected", view.served_ui_version)
        self.assertIsNone(view.next_action)
        self.assertEqual(
            "ignored",
            _admit(
                "stop_owner",
                code="local_only",
                versions={"desktop": "1.0.13", "remote": None, "served_ui": digest},
                owner={"state": "live", "token_available": True},
                actions=[],
            ).outcome,
        )

    def test_foreign_owner_still_admits_independent_this_pc_action(self) -> None:
        view = present(
            owner={"state": "foreign", "token_available": True},
            actions=["update_this_pc", "update_connected_server"],
        )
        self.assertEqual("update_this_pc", view.next_action)
        self.assertEqual(
            "action",
            _admit(
                "update_this_pc",
                owner={"state": "foreign", "token_available": True},
                actions=["update_this_pc", "update_connected_server"],
            ).outcome,
        )
        self.assertEqual(
            "ignored",
            _admit(
                "update_connected_server",
                owner={"state": "foreign", "token_available": True},
                actions=["update_this_pc", "update_connected_server"],
            ).outcome,
        )

    def test_explicit_dead_owner_mutations_remain_admissible(self) -> None:
        self.assertEqual(
            "action",
            _admit(
                "update_connected_server",
                owner=quiescent_owner(),
                actions=["update_connected_server"],
            ).outcome,
        )
        self.assertEqual(
            "action",
            _admit(
                "restore_backup",
                stage="failed",
                code="failed",
                owner=quiescent_owner(),
                backup={"status": "verified"},
                actions=["restore_backup"],
            ).outcome,
        )
        self.assertEqual(
            "action",
            _admit(
                "restart",
                stage="activate",
                owner=quiescent_owner(),
                backup={"status": "verified"},
                actions=["restart"],
            ).outcome,
        )


class AllMutationQuiescenceTests(unittest.TestCase):
    def test_each_mutation_refuses_each_missing_or_failed_observation(self):
        cases = [("update_connected_server", "preview"), ("retry", "failed"),
                 ("restore_backup", "failed"),
                 ("continue_flow", "prepare"), ("continue_flow", "backup")]
        for operation, stage in cases:
            for field in ("process_exit", "listener_release", "lease_release"):
                for value in ("unknown", "failed"):
                    owner = dict(quiescent_owner(), **{field: value})
                    with self.subTest(operation=operation, stage=stage, field=field, value=value):
                        result = _admit(operation, stage=stage, owner=owner,
                                        backup={"status": "verified"}, actions=[operation])
                        self.assertEqual(result.outcome, "ignored")

    def test_each_safe_mutation_keeps_its_explicit_positive_path(self):
        for operation, stage in [("update_connected_server", "preview"),
                                 ("retry", "failed"), ("restore_backup", "failed"),
                                 ("restart", "activate"), ("continue_flow", "prepare")]:
            with self.subTest(operation=operation):
                self.assertEqual(_admit(operation, stage=stage, owner=quiescent_owner(),
                    backup={"status": "verified"}, actions=[operation]).outcome, "action")

    def test_restart_is_stage_gated_and_not_a_new_remote_mutation(self):
        incomplete = dict(quiescent_owner(), process_exit="unknown")
        self.assertEqual(
            "action",
            _admit(
                "restart",
                stage="activate",
                owner=incomplete,
                actions=["restart"],
            ).outcome,
        )
        self.assertEqual(
            "ignored",
            _admit(
                "restart",
                stage="prepare",
                owner=quiescent_owner(),
                actions=["restart"],
            ).outcome,
        )


class Integration4ActionTests(unittest.TestCase):
    def test_reconcile_pending_is_admitted_while_owner_is_unknown(self) -> None:
        owner = {
            "state": "unknown",
            "token_available": None,
            "process_exit": "unknown",
            "listener_release": "unknown",
            "lease_release": "unknown",
        }
        view = present(stage="unknown", code="unknown", owner=owner, actions=["reconcile_pending"])
        self.assertEqual("reconcile_pending", view.next_action)
        self.assertEqual("Check pending outcome", view.next_action_label)
        self.assertIn("same retained operation", view.detail)
        self.assertEqual(
            "action",
            _admit(
                "reconcile_pending",
                stage="unknown",
                code="unknown",
                owner=owner,
                actions=["reconcile_pending"],
            ).outcome,
        )

    def test_verify_connection_is_read_only_with_a_live_owner(self) -> None:
        owner = {
            "state": "live",
            "token_available": True,
            "process_exit": "unknown",
            "listener_release": "unknown",
            "lease_release": "unknown",
        }
        view = present(stage="verify", owner=owner, actions=["verify_connection", "continue_flow"])
        self.assertEqual("verify_connection", view.next_action)
        self.assertEqual("Verify the connected server", view.next_action_label)
        self.assertEqual(
            "action",
            _admit(
                "verify_connection",
                stage="verify",
                owner=owner,
                actions=["verify_connection", "continue_flow"],
            ).outcome,
        )
        self.assertEqual(
            "ignored",
            _admit(
                "continue_flow",
                stage="verify",
                owner=owner,
                actions=["verify_connection", "continue_flow"],
            ).outcome,
        )

    def test_rollback_follows_pairing_policy_not_unknown(self) -> None:
        live = {
            "state": "live",
            "token_available": True,
            "process_exit": "unknown",
            "listener_release": "unknown",
            "lease_release": "unknown",
        }
        self.assertIsNone(
            present(
                stage="failed",
                code="unknown",
                owner=live,
                actions=["rollback_activation"],
            ).next_action
        )
        self.assertEqual(
            "ignored",
            _admit(
                "rollback_activation",
                stage="failed",
                code="unknown",
                owner=live,
                actions=["rollback_activation"],
            ).outcome,
        )
        self.assertEqual(
            "rollback_activation",
            present(
                stage="failed",
                code="failed",
                owner=live,
                actions=["rollback_activation"],
            ).next_action,
        )
        self.assertEqual(
            "action",
            _admit(
                "rollback_activation",
                stage="failed",
                code="failed",
                owner=live,
                actions=["rollback_activation"],
            ).outcome,
        )

    def test_empty_actions_do_not_synthesize_integration_actions(self) -> None:
        for operation in ("reconcile_pending", "rollback_activation", "verify_connection"):
            with self.subTest(operation=operation):
                self.assertEqual(
                    "ignored",
                    _admit(
                        operation,
                        stage="failed",
                        code="failed",
                        owner=quiescent_owner(),
                        backup={"status": "verified"},
                        actions=[],
                    ).outcome,
                )

    def test_parser_accepts_the_integration_action_names(self) -> None:
        for operation in ("reconcile_pending", "rollback_activation", "verify_connection"):
            parsed = VIEW.parse_remote_update_request(request(operation))
            self.assertIsNotNone(parsed)
            self.assertEqual(operation, parsed.operation)


class OfferedStopRuleTests(unittest.TestCase):
    """The offered stop is itself the authority check.

    The owner facts are what an authenticated stop returns, so the page may
    not demand them before the call that obtains them.  It may also not claim
    them: an unmeasured owner still reads Unknown on the page.
    """

    def test_unmeasured_owner_admits_the_offered_stop_without_claiming_facts(self) -> None:
        view = present(stage="preview", actions=["stop_owner", "review", "cancel"])
        self.assertEqual("stop_owner", view.next_action)
        self.assertEqual("Unknown", view.owner_state)
        self.assertEqual("Unknown", view.token_availability)
        self.assertIn("confirmed separately", view.detail)
        self.assertEqual(
            "action",
            _admit("stop_owner", stage="preview", actions=["stop_owner", "review", "cancel"]).outcome,
        )
        html = page(stage="preview", actions=["stop_owner", "review", "cancel"])
        self.assertIn('data-operation="stop_owner"', html)

    def test_an_unoffered_stop_is_never_admitted_from_an_unmeasured_owner(self) -> None:
        view = present(stage="preview", actions=["review", "cancel"])
        self.assertNotEqual("stop_owner", view.next_action)
        self.assertEqual(
            "ignored",
            _admit("stop_owner", stage="preview", actions=["review", "cancel"]).outcome,
        )
        self.assertEqual(
            "ignored", _admit("stop_owner", stage="preview", actions=[]).outcome
        )

    def test_measured_evidence_rules_once_the_stop_has_answered(self) -> None:
        refusing = {
            "missing_token": {"state": "live", "token_available": False},
            "foreign": {"state": "foreign", "token_available": True},
            "unfenced": {"state": "unfenced", "token_available": False},
            "stopping": {"state": "stopping", "token_available": True},
            "dead": {"state": "dead", "token_available": True},
            # State alone is still unknown, but a release was observed, so the
            # stop has answered and its evidence -- not the offer -- decides.
            "partial_release": {"process_exit": "verified"},
            "token_only": {"token_available": False},
        }
        for name, owner in refusing.items():
            with self.subTest(owner=name):
                self.assertNotEqual(
                    "stop_owner",
                    present(stage="stop", owner=owner, actions=["stop_owner"]).next_action,
                )
                self.assertEqual(
                    "ignored",
                    _admit("stop_owner", stage="stop", owner=owner, actions=["stop_owner"]).outcome,
                )
        live = {"state": "live", "token_available": True}
        self.assertEqual(
            "stop_owner",
            present(stage="stop", owner=live, actions=["stop_owner"]).next_action,
        )
        self.assertEqual(
            "action",
            _admit("stop_owner", stage="stop", owner=live, actions=["stop_owner"]).outcome,
        )

    def test_the_offered_stop_never_relaxes_the_later_mutations(self) -> None:
        offered = ["stop_owner", "continue_flow", "restore_backup", "update_connected_server"]
        view = present(stage="preview", backup={"status": "verified"}, actions=offered)
        self.assertEqual("stop_owner", view.next_action)
        for blocked in ("continue_flow", "restore_backup", "update_connected_server"):
            with self.subTest(action=blocked):
                self.assertEqual(
                    "ignored",
                    _admit(
                        blocked,
                        stage="preview",
                        backup={"status": "verified"},
                        actions=offered,
                    ).outcome,
                )


class EarlyPrepareRuleTests(unittest.TestCase):
    """Staging the new application files is admitted by its offer alone.

    Preparation puts a new application beside the running one.  It reads and
    writes no SSOT, starts no server and selects no activation, so the
    quiescence gate that protects the live data is not the gate for it; the
    gate for it is that the flow is offering it right now.
    """

    def test_the_offered_staging_is_clickable_with_no_owner_and_no_backup(self) -> None:
        offered = ["prepare_app_files", "review", "cancel"]
        view = present(stage="preview", actions=offered)
        self.assertEqual("prepare_app_files", view.next_action)
        self.assertEqual("Unknown", view.owner_state)
        self.assertIn("activates nothing", view.detail)
        self.assertEqual(
            "action", _admit("prepare_app_files", stage="preview", actions=offered).outcome
        )
        self.assertIn(
            'data-operation="prepare_app_files"', page(stage="preview", actions=offered)
        )

    def test_unoffered_staging_is_never_admitted(self) -> None:
        self.assertEqual(
            "ignored",
            _admit("prepare_app_files", stage="preview", actions=["review"]).outcome,
        )
        self.assertEqual(
            "ignored", _admit("prepare_app_files", stage="preview", actions=[]).outcome
        )

    def test_offered_staging_relaxes_no_live_data_mutation(self) -> None:
        offered = [
            "prepare_app_files", "continue_flow", "restore_backup",
            "update_connected_server",
        ]
        live = {"state": "live", "token_available": True}
        self.assertEqual(
            "prepare_app_files",
            present(
                stage="preview", owner=live, backup={"status": "verified"}, actions=offered
            ).next_action,
        )
        for blocked in ("continue_flow", "restore_backup", "update_connected_server"):
            with self.subTest(action=blocked):
                self.assertEqual(
                    "ignored",
                    _admit(
                        blocked,
                        stage="preview",
                        owner=live,
                        backup={"status": "verified"},
                        actions=offered,
                    ).outcome,
                )


class StopRetryRuleTests(unittest.TestCase):
    """The stop retry needs the live-owner answer that only a stop produces."""

    def test_a_measured_live_owner_with_a_token_admits_the_offered_stop_retry(self) -> None:
        live = {"state": "live", "token_available": True, "pidfd_available": True}
        offered = ["retry_stop", "review", "cancel"]
        view = present(stage="failed", code="owner_live", owner=live, actions=offered)
        self.assertEqual("retry_stop", view.next_action)
        self.assertIn("still running", view.detail)
        self.assertEqual(
            "action",
            _admit(
                "retry_stop", stage="failed", code="owner_live", owner=live, actions=offered
            ).outcome,
        )

    def test_the_stop_retry_is_refused_without_that_exact_evidence(self) -> None:
        live = {"state": "live", "token_available": True}
        refusing = {
            # Offered, but no authenticated stop answered "still live" here.
            "other_code": ("failed", "failed", live, ["retry_stop"]),
            # The owner-live code without an owner-live reading is not evidence.
            "dead_owner": (
                "failed", "owner_live", {"state": "dead", "token_available": True},
                ["retry_stop"],
            ),
            "missing_token": (
                "failed", "owner_live", {"state": "live", "token_available": False},
                ["retry_stop"],
            ),
            "unknown_token": (
                "failed", "owner_live", {"state": "live", "token_available": None},
                ["retry_stop"],
            ),
            # Never synthesized from an offer the flow is not making.
            "not_offered": ("failed", "owner_live", live, ["review", "cancel"]),
        }
        for name, (stage, code, owner, actions) in refusing.items():
            with self.subTest(case=name):
                self.assertNotEqual(
                    "retry_stop",
                    present(stage=stage, code=code, owner=owner, actions=actions).next_action,
                )
                self.assertEqual(
                    "ignored",
                    _admit(
                        "retry_stop", stage=stage, code=code, owner=owner, actions=actions
                    ).outcome,
                )

    def test_the_stop_retry_relaxes_no_other_mutation_on_the_same_page(self) -> None:
        live = {"state": "live", "token_available": True}
        offered = ["retry_stop", "retry", "continue_flow", "restore_backup"]
        for blocked in ("retry", "continue_flow", "restore_backup"):
            with self.subTest(action=blocked):
                self.assertEqual(
                    "ignored",
                    _admit(
                        blocked,
                        stage="failed",
                        code="owner_live",
                        owner=live,
                        backup={"status": "verified"},
                        actions=offered,
                    ).outcome,
                )


if __name__ == "__main__":
    unittest.main()

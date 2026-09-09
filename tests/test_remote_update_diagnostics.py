"""Remote update diagnostics: redaction, honesty and bounded output.

Three things are worth failing a build over here.  The report must never carry
a secret, a command line, a host name, a path or Task/Context text, so the
redaction tests feed the formatter a payload stuffed with canaries and assert
that not one of them survives anywhere in the rendered block.  The report must
never overstate what was observed, so an issued stop, an absent fact and a
backup that never ran each have to read as their own honest answer rather than
as success.  And an untrusted remote-reported string must not become markup or
a shell word when the summary is pasted somewhere else.

The canaries are checked against the whole report rather than field by field
on purpose: a field-by-field assertion only proves the fields we thought of are
clean, while a whole-text search also catches a leak through a section added
later.
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

import remote_update_diagnostics as DIAG  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "remote_recovery"

# Values that must never reach the report. Each is deliberately shaped like the
# real thing it stands for.
SESSION_TOKEN = "r5pending-token-not-enforced-01"
TOKEN_HASH = "a" * 64
FULL_ARGV = "ssh -i C:/Users/analyst/.ssh/id_ed25519 build01.corp.example.com"
INTERNAL_HOST = "build01.corp.example.com"
INTERNAL_PATH = "/nfs/home/analyst/.workstack/data"
TASK_BODY = "Q3 pricing rework for the Northwind account"
CANARIES = (
    SESSION_TOKEN,
    TOKEN_HASH,
    FULL_ARGV,
    INTERNAL_HOST,
    INTERNAL_PATH,
    TASK_BODY,
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class NormalizationContract(unittest.TestCase):
    """The snapshot always has the R6 shape, whatever arrives."""

    def test_empty_payload_is_a_complete_unknown_snapshot(self) -> None:
        view = DIAG.normalize_view({})

        self.assertEqual(view["schema_version"], DIAG.SCHEMA_VERSION)
        self.assertEqual(view["stage"], "unknown")
        self.assertIsNone(view["code"])
        self.assertEqual(
            set(view["versions"]), set(DIAG.VERSION_FIELDS)
        )
        self.assertTrue(all(value is None for value in view["versions"].values()))
        self.assertEqual(view["owner"]["state"], "unknown")
        self.assertIsNone(view["owner"]["token_available"])
        self.assertIsNone(view["owner"]["pidfd_available"])
        for field in DIAG.OWNER_OBSERVATION_FIELDS:
            self.assertEqual(view["owner"][field], "unknown")
        self.assertEqual(view["install"], {"capability": "unknown", "method": "unknown"})
        self.assertEqual(
            view["backup"], {"status": "unknown", "migration_required": None}
        )
        self.assertEqual(view["actions"], [])

    def test_non_mapping_payloads_do_not_raise(self) -> None:
        for payload in (None, "unknown", 7, [], object()):
            with self.subTest(payload=type(payload).__name__):
                view = DIAG.normalize_view(payload)
                self.assertEqual(view["stage"], "unknown")
                self.assertEqual(view["schema_version"], DIAG.SCHEMA_VERSION)

    def test_unrecognized_keys_are_dropped_entirely(self) -> None:
        view = DIAG.normalize_view(
            {"stage": "ready", "session_token": SESSION_TOKEN, "argv": [FULL_ARGV]}
        )

        self.assertEqual(set(view), {
            "schema_version",
            "stage",
            "code",
            "versions",
            "owner",
            "install",
            "backup",
            "actions",
        })

    def test_stop_and_backup_are_separate_stages_in_that_order(self) -> None:
        """R1 clarification 1 resolved `backup/stop` into two ordered literals."""

        self.assertEqual(DIAG.normalize_view({"stage": "stop"})["stage"], "stop")
        self.assertEqual(DIAG.normalize_view({"stage": "backup"})["stage"], "backup")
        self.assertLess(
            DIAG.FLOW_STAGES.index("stop"), DIAG.FLOW_STAGES.index("backup")
        )
        # The slash spelling itself is not a stage.
        self.assertEqual(DIAG.normalize_view({"stage": "backup_stop"})["stage"], "unknown")

    def test_the_settled_stage_enum_is_exactly_the_agreed_vocabulary(self) -> None:
        self.assertEqual(
            DIAG.STAGES,
            (
                "idle",
                "preview",
                "stop",
                "backup",
                "prepare",
                "probe",
                "activate",
                "verify",
                "ready",
                "failed",
                "cancelled",
                "unknown",
            ),
        )

    def test_out_of_vocabulary_enum_values_become_unknown(self) -> None:
        view = DIAG.normalize_view(
            {
                "stage": "almost_ready",
                "owner": {"state": "probably_live", "process_exit": "likely"},
                "install": {"capability": "maybe", "method": "rsync"},
                "backup": {"status": "skipped"},
            }
        )

        self.assertEqual(view["stage"], "unknown")
        self.assertEqual(view["owner"]["state"], "unknown")
        self.assertEqual(view["owner"]["process_exit"], "unknown")
        self.assertEqual(view["install"], {"capability": "unknown", "method": "unknown"})
        self.assertEqual(view["backup"]["status"], "unknown")

    def test_only_a_real_bool_answers_a_tristate(self) -> None:
        for supplied in ("true", 1, 0, "", None, "yes"):
            with self.subTest(supplied=repr(supplied)):
                view = DIAG.normalize_view(
                    {
                        "owner": {"token_available": supplied, "pidfd_available": supplied},
                        "backup": {"migration_required": supplied},
                    }
                )
                self.assertIsNone(view["owner"]["token_available"])
                self.assertIsNone(view["owner"]["pidfd_available"])
                self.assertIsNone(view["backup"]["migration_required"])

        answered = DIAG.normalize_view(
            {"owner": {"token_available": False, "pidfd_available": True}}
        )
        self.assertIs(answered["owner"]["token_available"], False)
        self.assertIs(answered["owner"]["pidfd_available"], True)

    def test_protocol_and_schema_accept_numbers_within_bounds(self) -> None:
        view = DIAG.normalize_view({"versions": {"protocol": 6, "schema": 6}})
        self.assertEqual(view["versions"]["protocol"], "6")
        self.assertEqual(view["versions"]["schema"], "6")

        refused = DIAG.normalize_view(
            {"versions": {"protocol": -1, "schema": DIAG.MAX_PROTOCOL_NUMBER + 1}}
        )
        self.assertIsNone(refused["versions"]["protocol"])
        self.assertIsNone(refused["versions"]["schema"])

    def test_actions_are_deduplicated_and_bounded(self) -> None:
        supplied = ["retry"] * 3 + [f"action_{index}" for index in range(20)]
        view, notes = DIAG.normalize_view_with_notes({"actions": supplied})

        self.assertLessEqual(len(view["actions"]), DIAG.MAX_ACTIONS)
        self.assertEqual(len(set(view["actions"])), len(view["actions"]))
        self.assertIn("actions.overflow", notes)


class RedactionCanaries(unittest.TestCase):
    """Nothing secret, addressable or user-authored may reach the report."""

    def test_canary_payload_leaks_nothing(self) -> None:
        payload = load_fixture("redaction_canary_payload.json")
        # The fixture is only useful if it really carries the canaries.
        serialized = json.dumps(payload)
        for canary in CANARIES:
            self.assertIn(canary, serialized)

        report = DIAG.render_report(payload)

        for canary in CANARIES:
            with self.subTest(canary=canary[:24]):
                self.assertNotIn(canary, report)

    def test_canaries_lodged_in_allowlisted_fields_are_refused(self) -> None:
        """A leak through a legitimate field is the interesting failure.

        Dropping unknown keys is easy; the real risk is a caller putting a
        token where a version belongs, so every admitted field is loaded with a
        canary here rather than relying on the key allowlist alone.
        """

        report = DIAG.render_report(
            {
                "stage": TASK_BODY,
                "code": FULL_ARGV,
                "versions": {
                    "desktop": INTERNAL_PATH,
                    "remote": INTERNAL_HOST,
                    "served_ui": TOKEN_HASH,
                    "protocol": SESSION_TOKEN,
                    "schema": TASK_BODY,
                },
                "owner": {"state": SESSION_TOKEN, "process_exit": FULL_ARGV},
                "install": {"capability": INTERNAL_PATH},
                "backup": {"status": TASK_BODY},
                "actions": [INTERNAL_PATH, FULL_ARGV, TOKEN_HASH],
            }
        )

        for canary in CANARIES:
            with self.subTest(canary=canary[:24]):
                self.assertNotIn(canary, report)
        self.assertIn("Stage: Unknown", report)
        self.assertIn("Next action: Unknown", report)

    def test_a_sha256_shaped_value_is_refused_everywhere(self) -> None:
        """A token hash is indistinguishable from any other sha256 digest."""

        view = DIAG.normalize_view(
            {
                "versions": {"served_ui": TOKEN_HASH},
                "code": TOKEN_HASH,
                "actions": [TOKEN_HASH],
            }
        )

        self.assertIsNone(view["versions"]["served_ui"])
        self.assertIsNone(view["code"])
        self.assertEqual(view["actions"], [])

    def test_a_host_name_in_the_version_row_is_refused(self) -> None:
        """Regression: a host name is a fine symbolic string and a bad version.

        An earlier draft admitted one shared symbolic shape for every field,
        which printed `build01.corp.example.com` as the remote version. Each
        field now carries its own grammar, and a version must start with a
        digit.
        """

        for hostish in (INTERNAL_HOST, "corp-nfs01", "localhost", "example.com"):
            with self.subTest(value=hostish):
                view = DIAG.normalize_view({"versions": {"remote": hostish}})
                self.assertIsNone(view["versions"]["remote"])

    def test_a_token_shaped_value_is_not_an_action_or_a_code(self) -> None:
        for field, payload in (
            ("code", {"code": SESSION_TOKEN}),
            ("actions", {"actions": [SESSION_TOKEN, INTERNAL_HOST]}),
        ):
            with self.subTest(field=field):
                view = DIAG.normalize_view(payload)
                self.assertIsNone(view["code"])
                self.assertEqual(view["actions"], [])

    def test_an_abbreviated_build_id_still_passes(self) -> None:
        """Refusing digests must not cost the build identity we do want."""

        view = DIAG.normalize_view({"versions": {"served_ui": "1.0.13+6a2aa78"}})
        self.assertEqual(view["versions"]["served_ui"], "1.0.13+6a2aa78")

    def test_refusal_notes_name_fields_and_never_values(self) -> None:
        view, notes = DIAG.normalize_view_with_notes(
            {"versions": {"remote": INTERNAL_HOST}, "code": FULL_ARGV}
        )

        self.assertIn("versions.remote", notes)
        self.assertIn("code", notes)
        for note in notes:
            for canary in CANARIES:
                self.assertNotIn(canary, note)

        report = DIAG.format_report(view, refused=notes)
        self.assertIn("Refused as malformed or unsafe:", report)
        self.assertIn("versions.remote", report)

    def test_a_forged_refusal_list_cannot_inject_text(self) -> None:
        """The refusal list is field names, so a caller cannot write into it."""

        report = DIAG.format_report(
            DIAG.normalize_view({}), refused=[TASK_BODY, INTERNAL_PATH, "versions.remote"]
        )

        self.assertNotIn(TASK_BODY, report)
        self.assertNotIn(INTERNAL_PATH, report)
        self.assertIn("versions.remote", report)


class UntrustedStringsStayInert(unittest.TestCase):
    """A remote-reported string may not become markup or a shell word."""

    HOSTILE = (
        "1.0.13<script>alert(1)</script>",
        "1.0.13; rm -rf ~",
        "1.0.13 && curl example.com",
        "$(whoami)",
        "`id`",
        "1.0.13\nStage: Ready",
        "1.0.13\r\nCode: None",
        '1.0.13"',
        "1.0.13'",
        "../../etc/passwd",
        "C:\\Users\\analyst",
        "\u202eesrever",
        "1.0.13\x00",
    )

    def test_hostile_versions_are_refused_not_escaped(self) -> None:
        for hostile in self.HOSTILE:
            with self.subTest(hostile=repr(hostile)):
                view = DIAG.normalize_view({"versions": {"remote": hostile}})
                self.assertIsNone(view["versions"]["remote"])

    def test_a_multiline_value_cannot_forge_a_report_line(self) -> None:
        report = DIAG.render_report({"versions": {"remote": "1.0.13\nStage: Ready"}})

        self.assertIn("Remote server: Unknown", report)
        self.assertIn("Stage: Unknown", report)
        self.assertEqual(report.count("Stage:"), 1)

    def test_a_hostile_action_cannot_forge_a_next_action(self) -> None:
        report = DIAG.render_report({"actions": ["run: rm -rf /", "restart_desktop"]})

        self.assertIn("Next action: restart_desktop", report)
        self.assertNotIn("rm -rf", report)


class OversizedInput(unittest.TestCase):
    """Oversized facts are refused, and the report stays one pasteable block."""

    def test_an_oversized_symbol_is_refused(self) -> None:
        oversized = "a" * (DIAG.MAX_SYMBOL_LENGTH + 1)
        view = DIAG.normalize_view({"code": oversized, "actions": [oversized]})

        self.assertIsNone(view["code"])
        self.assertEqual(view["actions"], [])

    def test_an_oversized_version_is_refused(self) -> None:
        oversized = "1." + "0" * DIAG.MAX_VERSION_LENGTH
        self.assertIsNone(
            DIAG.normalize_view({"versions": {"desktop": oversized}})["versions"]["desktop"]
        )

    def test_a_maximal_payload_stays_within_the_report_ceiling(self) -> None:
        maximal = {
            "stage": "activate",
            "code": "R" * DIAG.MAX_SYMBOL_LENGTH,
            "versions": {field: "9" * DIAG.MAX_VERSION_LENGTH for field in DIAG.VERSION_FIELDS},
            "owner": {
                "state": "unfenced",
                "token_available": False,
                "pidfd_available": False,
                "process_exit": "failed",
                "listener_release": "failed",
                "lease_release": "failed",
            },
            "install": {"capability": "unavailable", "method": "verified_unpack"},
            "backup": {"status": "not_run", "migration_required": True},
            "actions": ["a" * DIAG.MAX_SYMBOL_LENGTH] * 2
            + [f"action_{index}" for index in range(DIAG.MAX_ACTIONS)],
        }

        report = DIAG.render_report(maximal)

        self.assertLessEqual(len(report), DIAG.MAX_REPORT_CHARACTERS)

    def test_a_giant_action_list_is_scanned_bounded(self) -> None:
        view, notes = DIAG.normalize_view_with_notes({"actions": ["!"] * 5000})

        self.assertEqual(view["actions"], [])
        self.assertLessEqual(len(notes), DIAG.MAX_ACTIONS + 2)


class HonestPartialResults(unittest.TestCase):
    """Absent, ambiguous and merely-attempted must not read as success."""

    def test_an_issued_stop_is_not_a_verified_exit(self) -> None:
        for confirmed in (None, "true", "done", 1):
            with self.subTest(confirmed=repr(confirmed)):
                self.assertEqual(
                    DIAG.project_observation(attempted=True, confirmed=confirmed),
                    "unknown",
                )

    def test_the_three_releases_are_observed_independently(self) -> None:
        view = DIAG.normalize_view(
            {
                "owner": {
                    "process_exit": "verified",
                    "listener_release": "unknown",
                    "lease_release": "failed",
                }
            }
        )

        self.assertEqual(view["owner"]["process_exit"], "verified")
        self.assertEqual(view["owner"]["listener_release"], "unknown")
        self.assertEqual(view["owner"]["lease_release"], "failed")

        report = DIAG.format_report(view)
        self.assertIn("Process exit: Verified", report)
        self.assertIn("Listener release: Unknown", report)
        self.assertIn("Lease release: Failed", report)

    def test_an_unattempted_release_is_unknown_not_failed(self) -> None:
        self.assertEqual(
            DIAG.project_observation(attempted=False, confirmed=False), "unknown"
        )

    def test_a_negative_observation_is_a_failure(self) -> None:
        self.assertEqual(
            DIAG.project_observation(attempted=True, confirmed=False), "failed"
        )
        self.assertEqual(
            DIAG.project_observation(attempted=True, confirmed=True), "verified"
        )

    def test_a_backup_that_never_ran_is_not_a_failed_backup(self) -> None:
        report = DIAG.render_report({"backup": {"status": "not_run"}})

        self.assertIn("Status: Not run", report)
        self.assertNotIn("Status: Failed", report)
        self.assertNotIn("Status: Verified", report)

    def test_a_missing_token_does_not_change_the_owner_state(self) -> None:
        """Missing token and owner liveness are separate conditions."""

        view = DIAG.normalize_view(
            {"owner": {"state": "live", "token_available": False}}
        )

        self.assertEqual(view["owner"]["state"], "live")
        self.assertIs(view["owner"]["token_available"], False)
        report = DIAG.format_report(view)
        self.assertIn("State: Live", report)
        self.assertIn("Session token: Missing", report)
        self.assertNotIn("Legacy", report)

    def test_no_pidfd_does_not_imply_a_verified_exit(self) -> None:
        report = DIAG.render_report(
            {"owner": {"state": "live", "pidfd_available": False}}
        )

        self.assertIn("pidfd support: Unavailable", report)
        self.assertIn("Process exit: Unknown", report)

    def test_the_report_never_claims_an_unobserved_success(self) -> None:
        report = DIAG.render_report({})

        for claim in ("Verified", "Available", "Live", "Ready"):
            self.assertNotIn(claim, report)


class ProductionProjections(unittest.TestCase):
    """The projections map the shell's real vocabularies, and lose nothing up."""

    def test_owner_state_projection_covers_every_production_state(self) -> None:
        import typing

        import remote_owner as OWNER

        production_states = set(typing.get_args(OWNER.OwnerState))
        self.assertIn("ambiguous", production_states)
        for state in production_states:
            with self.subTest(state=state):
                projected = DIAG.project_owner_state(state)
                self.assertIn(projected, DIAG.OWNER_STATES)

    def test_absent_replaced_and_ambiguous_all_project_to_unknown(self) -> None:
        """R1 clarification 5 settles the lossy direction for all three.

        None of them may become a confident verdict on this projection alone;
        only independent evidence could support another R6 state.
        """

        for classification in ("absent", "replaced", "ambiguous"):
            with self.subTest(classification=classification):
                self.assertEqual(DIAG.project_owner_state(classification), "unknown")

    def test_a_definite_classification_survives_the_projection(self) -> None:
        for classification in ("live", "dead", "foreign", "unfenced"):
            with self.subTest(classification=classification):
                self.assertEqual(
                    DIAG.project_owner_state(classification), classification
                )

    def test_foreign_and_unfenced_stay_distinct(self) -> None:
        self.assertEqual(DIAG.project_owner_state("foreign"), "foreign")
        self.assertEqual(DIAG.project_owner_state("unfenced"), "unfenced")

    def test_an_unrecognized_classification_is_unknown(self) -> None:
        for value in (None, "", "LIVE", 5, ["live"]):
            with self.subTest(value=repr(value)):
                self.assertEqual(DIAG.project_owner_state(value), "unknown")

    def test_stage_projection_covers_every_startup_state(self) -> None:
        import remote_startup_state as STARTUP

        for state in STARTUP.RemoteStartupState:
            with self.subTest(state=state.value):
                self.assertIn(DIAG.project_stage(state.value), DIAG.STAGES)

    def test_a_monitoring_attempt_reads_as_ready(self) -> None:
        self.assertEqual(DIAG.project_stage("MONITORING"), "ready")
        self.assertEqual(DIAG.project_stage("FAILED"), "failed")


class FormatterGuards(unittest.TestCase):
    """The formatter refuses anything that did not go through the allowlist."""

    def test_raw_caller_state_is_refused(self) -> None:
        for payload in ({}, {"schema_version": "remote-update-view/2"}, None, "x"):
            with self.subTest(payload=repr(payload)):
                with self.assertRaises(DIAG.DiagnosticsError):
                    DIAG.format_report(payload)

    def test_every_report_carries_the_redaction_notice(self) -> None:
        self.assertIn(DIAG.REDACTION_NOTICE, DIAG.render_report({}))

    def test_one_next_action_is_named_first(self) -> None:
        report = DIAG.render_report({"actions": ["stop_remote_owner", "retry_probe"]})

        self.assertIn("Next action: stop_remote_owner", report)
        self.assertIn("Other actions: retry_probe", report)


if __name__ == "__main__":
    unittest.main()

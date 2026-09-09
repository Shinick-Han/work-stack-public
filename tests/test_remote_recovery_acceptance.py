"""Remote update and close-reopen recovery: what is actually covered, and what is not.

The company reports a 1.0.13 connection success, and the confirmation everyone
still wants -- close, reopen, close again, with the process, the original
listener and the lease released each time -- is exactly the thing no local test
here can produce.  So this file is deliberately two things at once: a set of
real checks against production seams, and a machine-enforced statement of what
those checks do *not* prove.

``tests/fixtures/remote_recovery/acceptance_matrix.json`` lists every criterion
with a status.  A criterion marked ``covered`` must have a check registered in
``BOUND_CHECKS`` below, and every registered check must have a criterion marked
``covered``.  Both directions are asserted, so the matrix cannot be edited into
a PASS that no code backs, and a check cannot quietly stop being claimed.  A
criterion marked ``not_covered`` has to name the lane or gate that owns it and
say what is missing, and the test asserts that the diagnostic report for such a
condition reads Unknown rather than reporting success.

Everything here is synthetic and local, on Windows, against a Linux-targeted
product.  No corporate share, NFS mount, csh-family shell or company machine
was touched, and the matrix is checked for any row that would imply otherwise.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_owner as OWNER  # noqa: E402
import remote_process_handle as HANDLE  # noqa: E402
import remote_update_diagnostics as DIAG  # noqa: E402
import workstack_update_status as STATUS  # noqa: E402
from remote_command_contract import token_hash  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "remote_recovery"
MATRIX = json.loads((FIXTURES / "acceptance_matrix.json").read_text(encoding="utf-8"))

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
THIS_HOST = "b" * 64
THIS_BOOT = "c" * 64
OTHER_HOST = "d" * 64
OTHER_BOOT = "e" * 64
OWN_TOKEN = "synthetic-session-token-not-a-secret"

HONEST_STATUSES = frozenset({"covered", "not_covered", "not_run"})

# Words that would only appear in a row claiming a real corporate result. The
# matrix is synthetic, so any of them in a criterion is a fabricated receipt.
CORPORATE_CLAIM_WORDS = ("passed on the company", "company confirmed", "corporate pass")


class StubController:
    """A process controller that answers exactly what a scenario specifies.

    ``observe`` records its calls so a test can prove a pid was never inspected,
    which is the whole point of the foreign-host and prior-boot cases.
    """

    def __init__(
        self,
        *,
        observed: str = "live",
        host: str | None = THIS_HOST,
        boot: str | None = THIS_BOOT,
    ) -> None:
        self._observed = observed
        self._host = host
        self._boot = boot
        self.observed_pids: list[int] = []

    def current_pid(self) -> int:
        return 4242

    def start_identity(self, pid: int) -> str | None:
        return "start-1"

    def observe(self, pid: int, start_identity: str) -> str:
        self.observed_pids.append(pid)
        return self._observed

    def open_owned_process(self, pid: int, start_identity: str):
        raise AssertionError("acceptance scenarios never signal a process")

    def host_identity(self) -> str | None:
        return self._host

    def boot_identity(self) -> str | None:
        return self._boot


def receipt(
    *,
    host: str | None = THIS_HOST,
    boot: str | None = THIS_BOOT,
    token: str = OWN_TOKEN,
) -> OWNER.OwnerReceipt:
    return OWNER.OwnerReceipt(
        workspace_id=WORKSPACE_ID,
        data_dir_digest="a" * 64,
        app_dir_digest="f" * 64,
        pid=4242,
        start_identity="start-1",
        release_id="1.0.13",
        token_hash=token_hash(token),
        host_identity=host,
        boot_identity=boot,
    )


def owner_view(state: str, **owner_facts: object) -> dict:
    return DIAG.normalize_view({"owner": {"state": state, **owner_facts}})


class MatrixIntegrity(unittest.TestCase):
    """The matrix may not claim more than the registered checks execute."""

    def test_every_criterion_is_well_formed(self) -> None:
        self.assertEqual(MATRIX["schema"], "remote-recovery-acceptance/1")
        seen: set[str] = set()
        for criterion in MATRIX["criteria"]:
            with self.subTest(criterion=criterion.get("id")):
                identifier = criterion["id"]
                self.assertNotIn(identifier, seen)
                seen.add(identifier)
                self.assertTrue(criterion["title"].strip())
                self.assertIn(criterion["status"], HONEST_STATUSES)
                if criterion["status"] == "covered":
                    self.assertTrue(criterion["seam"].strip())
                else:
                    self.assertTrue(criterion["owner"].strip())
                    self.assertGreater(len(criterion["reason"].strip()), 40)

    def test_covered_criteria_and_registered_checks_agree(self) -> None:
        """Neither side may drift: no unbacked PASS, no unclaimed check."""

        claimed = {
            criterion["id"]
            for criterion in MATRIX["criteria"]
            if criterion["status"] == "covered"
        }

        self.assertEqual(
            claimed,
            set(BOUND_CHECKS),
            "matrix 'covered' rows and registered acceptance checks must match exactly",
        )

    def test_the_matrix_declares_a_synthetic_local_environment(self) -> None:
        self.assertEqual(MATRIX["environment"]["kind"], "synthetic_local")
        self.assertIn("No corporate result", MATRIX["environment"]["note"])

    def test_no_criterion_claims_a_corporate_result(self) -> None:
        serialized = json.dumps(MATRIX).lower()
        for phrase in CORPORATE_CLAIM_WORDS:
            self.assertNotIn(phrase, serialized)

        company = [
            criterion
            for criterion in MATRIX["criteria"]
            if criterion["id"] == "COMPANY-01"
        ]
        self.assertEqual(len(company), 1)
        self.assertEqual(company[0]["status"], "not_covered")

    def test_the_environment_matrix_criteria_are_all_present(self) -> None:
        """The uncovered half of the task must stay visible, not vanish."""

        required = {
            "NFS-01",
            "CSH-01",
            "PIDFD-02",
            "UNPACK-01",
            "DIST-01",
            "SCHEMA-01",
            "CLOSE-01",
            "BUILD-01",
            "COMPANY-01",
        }
        present = {criterion["id"] for criterion in MATRIX["criteria"]}
        self.assertTrue(required.issubset(present), required - present)


class VersionCriteria(unittest.TestCase):
    """RV-01..RV-04, against the shipped update-status wording."""

    DESKTOP = "1.0.13"

    def check_rv_01(self) -> None:
        clause = STATUS.remote_clause(
            desktop_version=self.DESKTOP, remote_version="1.0.12"
        )

        self.assertIn("1.0.12", clause)
        self.assertIn(f"does not match desktop {self.DESKTOP}", clause)
        self.assertIn(STATUS.DESKTOP_ONLY_SCOPE, clause)
        self.assertNotIn("up to date", clause)

    def check_rv_02(self) -> None:
        clause = STATUS.remote_clause(
            desktop_version=self.DESKTOP, remote_version=self.DESKTOP
        )

        self.assertIn("do not prove the remote interface files were", clause)

    def check_rv_03(self) -> None:
        for reported in (None, "", "unknown"):
            with self.subTest(reported=repr(reported)):
                clause = STATUS.remote_clause(
                    desktop_version=self.DESKTOP, remote_version=reported
                )
                self.assertIn("could not be verified", clause)

    def check_rv_04(self) -> None:
        for alias in ("1.0.13<script>alert(1)</script>", "1.0.13 (build 9)", " 1.0.13"):
            with self.subTest(alias=alias):
                self.assertEqual(STATUS.canonical_version(alias), "")
                clause = STATUS.remote_clause(
                    desktop_version=self.DESKTOP, remote_version=alias
                )
                self.assertIn("could not be verified", clause)
                # And the same alias cannot reach the diagnostic report either.
                view = DIAG.normalize_view({"versions": {"remote": alias}})
                self.assertIsNone(view["versions"]["remote"])

    test_rv_01_older_remote_is_a_mismatch = check_rv_01
    test_rv_02_equal_versions_prove_nothing = check_rv_02
    test_rv_03_unreported_remote_stays_unverified = check_rv_03
    test_rv_04_canonical_alias_is_refused_whole = check_rv_04


class OwnerCriteria(unittest.TestCase):
    """OWN-01..OWN-05 and TOK-01, against the shipped owner classifier."""

    def check_own_01(self) -> None:
        controller = StubController(observed="live")

        state = OWNER.classify_owner(receipt(), controller)

        self.assertEqual(state, "live")
        self.assertEqual(DIAG.project_owner_state(state), "live")
        self.assertIn("State: Live", DIAG.format_report(owner_view("live")))

    def check_own_02(self) -> None:
        """A prior boot is provably dead without reading any pid."""

        controller = StubController(observed="live", boot=OTHER_BOOT)

        state = OWNER.classify_owner(receipt(boot=THIS_BOOT), controller)

        self.assertEqual(state, "dead")
        self.assertEqual(controller.observed_pids, [])
        self.assertEqual(DIAG.project_owner_state(state), "dead")

    def check_own_03(self) -> None:
        controller = StubController(observed="live", host=OTHER_HOST)

        state = OWNER.classify_owner(receipt(host=THIS_HOST), controller)

        self.assertEqual(state, "foreign")
        self.assertEqual(controller.observed_pids, [])
        self.assertEqual(DIAG.project_owner_state(state), "foreign")
        report = DIAG.format_report(owner_view("foreign"))
        self.assertIn("Owned by another host", report)
        # The other host is named nowhere in the report.
        self.assertNotIn(OTHER_HOST, report)

    def check_own_04(self) -> None:
        """A pre-fencing receipt is unfenced, which is not the same as legacy-ok."""

        controller = StubController(observed="live")

        state = OWNER.classify_owner(receipt(host=None, boot=None), controller)

        self.assertEqual(state, "unfenced")
        self.assertEqual(controller.observed_pids, [])
        self.assertEqual(DIAG.project_owner_state(state), "unfenced")
        report = DIAG.format_report(owner_view("unfenced"))
        self.assertIn("Legacy receipt without host or boot identity", report)
        self.assertNotIn("State: Dead", report)

    def check_own_05(self) -> None:
        controller = StubController(observed="unknown")

        state = OWNER.classify_owner(receipt(), controller)

        self.assertEqual(state, "ambiguous")
        self.assertEqual(DIAG.project_owner_state(state), "unknown")
        report = DIAG.format_report(owner_view("unknown"))
        self.assertIn("State: Unknown", report)
        self.assertNotIn("State: Dead", report)
        self.assertNotIn("State: Live", report)

    def check_own_06(self) -> None:
        """A reused pid is the case where a confident verdict is worth least.

        Production distinguishes ``replaced`` from ``exited``: the recorded
        process is gone either way, but only ``exited`` is evidence this
        session's own owner ended. R1 clarification 5 settles the projection at
        ``unknown`` rather than ``dead``.
        """

        controller = StubController(observed="replaced")

        state = OWNER.classify_owner(receipt(), controller)

        self.assertEqual(state, "replaced")
        self.assertEqual(DIAG.project_owner_state(state), "unknown")
        report = DIAG.format_report(owner_view("unknown"))
        self.assertIn("State: Unknown", report)
        self.assertNotIn("State: Dead", report)

    def check_tok_01(self) -> None:
        """Missing token and owner liveness are separate conditions."""

        current = receipt()

        self.assertFalse(OWNER.owner_recognizes_caller(current, None))
        self.assertFalse(OWNER.owner_recognizes_caller(current, ""))
        self.assertFalse(OWNER.owner_recognizes_caller(current, "another-token"))
        self.assertTrue(OWNER.owner_recognizes_caller(current, OWN_TOKEN))

        # A live owner stays live when the caller has no token: the missing
        # token is reported on its own line and never downgrades the state.
        view = owner_view("live", token_available=False)
        self.assertEqual(view["owner"]["state"], "live")
        report = DIAG.format_report(view)
        self.assertIn("State: Live", report)
        self.assertIn("Session token: Missing", report)
        self.assertNotIn("Legacy receipt", report)
        self.assertNotIn(OWN_TOKEN, report)
        self.assertNotIn(token_hash(OWN_TOKEN), report)

    test_own_01_live_owner = check_own_01
    test_own_02_prior_boot_is_dead_unread = check_own_02
    test_own_03_foreign_host_is_never_inspected = check_own_03
    test_own_04_unfenced_legacy_receipt = check_own_04
    test_own_05_ambiguous_stays_unknown = check_own_05
    test_own_06_replaced_pid_stays_unknown = check_own_06
    test_tok_01_missing_token_is_independent = check_tok_01


class ShutdownCriteria(unittest.TestCase):
    """PIDFD-01 and STOP-01: what a stop does and does not prove."""

    def check_pidfd_01(self) -> None:
        measured = HANDLE.pidfd_signalling_available()
        self.assertIsInstance(measured, bool)

        view = DIAG.normalize_view(
            {"owner": {"state": "live", "pidfd_available": measured}}
        )
        self.assertIs(view["owner"]["pidfd_available"], measured)
        # Whatever the answer, it is not evidence about the process.
        self.assertEqual(view["owner"]["process_exit"], "unknown")

        report = DIAG.format_report(view)
        expected = "Available" if measured else "Unavailable"
        self.assertIn(f"pidfd support: {expected}", report)
        self.assertIn("Process exit: Unknown", report)

    def check_stop_01(self) -> None:
        """A spawned stop command is not a successful shutdown."""

        issued = {
            field: DIAG.project_observation(attempted=True, confirmed=None)
            for field in DIAG.OWNER_OBSERVATION_FIELDS
        }
        self.assertEqual(set(issued.values()), {"unknown"})

        report = DIAG.format_report(
            DIAG.normalize_view({"stage": "stop", "owner": {"state": "stopping", **issued}})
        )
        self.assertIn("Process exit: Unknown", report)
        self.assertIn("Listener release: Unknown", report)
        self.assertIn("Lease release: Unknown", report)
        self.assertNotIn("Verified", report)

    test_pidfd_01_availability_is_measured_not_assumed = check_pidfd_01
    test_stop_01_an_issued_stop_proves_nothing = check_stop_01


class ReportCriteria(unittest.TestCase):
    """RED-01, PART-01 and CAP-01: the report itself, end to end."""

    def check_red_01(self) -> None:
        payload = json.loads(
            (FIXTURES / "redaction_canary_payload.json").read_text(encoding="utf-8")
        )
        report = DIAG.render_report(payload)

        for canary in (
            "r5pending-token-not-enforced-01",
            "a" * 64,
            "ssh -i C:/Users/analyst/.ssh/id_ed25519 build01.corp.example.com",
            "build01.corp.example.com",
            "/nfs/home/analyst/.workstack",
            "Q3 pricing rework for the Northwind account",
        ):
            with self.subTest(canary=canary[:24]):
                self.assertNotIn(canary, report)

        # The useful facts still survive the redaction.
        self.assertIn("Stage: Stopping the current server", report)
        self.assertIn("Code: REMOTE_LOCK_OWNED", report)
        self.assertIn("Desktop: 1.0.13", report)
        self.assertIn("Next action: confirm_no_remote_owner_then_remove_receipt", report)

    def check_part_01(self) -> None:
        for payload in ({}, None, "", {"versions": None}, {"owner": "live"}):
            with self.subTest(payload=repr(payload)):
                report = DIAG.render_report(payload)
                self.assertIn("Stage: Unknown", report)
                self.assertIn("State: Unknown", report)
                self.assertIn("Status: Unknown", report)
                self.assertIn("Next action: Unknown", report)
                self.assertLessEqual(len(report), DIAG.MAX_REPORT_CHARACTERS)

    def check_cap_01(self) -> None:
        """An unmeasured filesystem never reads as a supported one.

        Not every NFS refuses every atomic operation, so an unmeasured share
        may not be reported either way.
        """

        report = DIAG.render_report({"install": {}})

        self.assertIn("Install capability: Unknown", report)
        self.assertIn("Install method: Unknown", report)
        self.assertNotIn("Install capability: Available", report)
        self.assertNotIn("Install capability: Unavailable", report)

    test_red_01_no_canary_survives = check_red_01
    test_part_01_partial_input_is_honest = check_part_01
    test_cap_01_unmeasured_capability_stays_unknown = check_cap_01


class BindingCriteria(unittest.TestCase):
    """BIND-01: the matrix names a commit this checkout actually contains."""

    def check_bind_01(self) -> None:
        base = MATRIX["base_commit"]
        self.assertRegex(base, r"\A[0-9a-f]{40}\Z")

        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        # A missing git is a harness failure, not a product failure, and the
        # message says so rather than letting the criterion silently pass.
        self.assertEqual(
            completed.returncode,
            0,
            f"HARNESS or BINDING failure: base commit {base} is not present in "
            f"{ROOT}; git said {completed.stderr.strip()!r}",
        )

    test_bind_01_base_commit_exists = check_bind_01


class UncoveredCriteriaStayUncovered(unittest.TestCase):
    """A not-covered criterion must read Unknown, never as a pass."""

    def uncovered(self) -> list[dict]:
        return [
            criterion
            for criterion in MATRIX["criteria"]
            if criterion["status"] != "covered"
        ]

    def test_each_uncovered_criterion_names_an_owner_and_a_reason(self) -> None:
        uncovered = self.uncovered()
        self.assertTrue(uncovered, "the matrix must not pretend everything is covered")
        for criterion in uncovered:
            with self.subTest(criterion=criterion["id"]):
                self.assertTrue(criterion["owner"].strip())
                self.assertTrue(criterion["reason"].strip())
                self.assertNotIn(criterion["id"], BOUND_CHECKS)

    def test_the_close_reopen_close_confirmation_is_not_claimed(self) -> None:
        """The one confirmation the company is still missing is not faked here.

        Nothing populates the three release observations yet, so a report built
        from a real close would say Unknown three times. That is the honest
        answer, and it is asserted so no later change can turn silence into a
        pass.
        """

        close = next(c for c in MATRIX["criteria"] if c["id"] == "CLOSE-01")
        self.assertEqual(close["status"], "not_covered")

        report = DIAG.render_report({"stage": "idle", "owner": {"state": "dead"}})
        self.assertIn("Process exit: Unknown", report)
        self.assertIn("Listener release: Unknown", report)
        self.assertIn("Lease release: Unknown", report)

    def test_the_schema_migration_criterion_reports_no_verdict(self) -> None:
        schema = next(c for c in MATRIX["criteria"] if c["id"] == "SCHEMA-01")
        self.assertEqual(schema["status"], "not_covered")

        # migration_required is a fact this lane relays, never one it verifies,
        # and a backup that never ran is not a backup that succeeded.
        report = DIAG.render_report({"backup": {"migration_required": True}})
        self.assertIn("Migration required: Yes", report)
        self.assertIn("Status: Unknown", report)
        self.assertNotIn("Status: Verified", report)


# Registered checks, keyed by criterion id. ``MatrixIntegrity`` asserts this
# mapping matches the matrix's 'covered' rows exactly in both directions.
BOUND_CHECKS: dict[str, tuple[type[unittest.TestCase], str]] = {
    "RV-01": (VersionCriteria, "check_rv_01"),
    "RV-02": (VersionCriteria, "check_rv_02"),
    "RV-03": (VersionCriteria, "check_rv_03"),
    "RV-04": (VersionCriteria, "check_rv_04"),
    "OWN-01": (OwnerCriteria, "check_own_01"),
    "OWN-02": (OwnerCriteria, "check_own_02"),
    "OWN-03": (OwnerCriteria, "check_own_03"),
    "OWN-04": (OwnerCriteria, "check_own_04"),
    "OWN-05": (OwnerCriteria, "check_own_05"),
    "OWN-06": (OwnerCriteria, "check_own_06"),
    "TOK-01": (OwnerCriteria, "check_tok_01"),
    "PIDFD-01": (ShutdownCriteria, "check_pidfd_01"),
    "STOP-01": (ShutdownCriteria, "check_stop_01"),
    "RED-01": (ReportCriteria, "check_red_01"),
    "PART-01": (ReportCriteria, "check_part_01"),
    "CAP-01": (ReportCriteria, "check_cap_01"),
    "BIND-01": (BindingCriteria, "check_bind_01"),
}


class BoundChecksAreRunnable(unittest.TestCase):
    """Every registered check must exist and be collected as a real test."""

    def test_each_bound_check_exists_and_is_exposed_as_a_test(self) -> None:
        loader = unittest.TestLoader()
        for identifier, (case, method) in BOUND_CHECKS.items():
            with self.subTest(criterion=identifier):
                self.assertTrue(
                    callable(getattr(case, method, None)),
                    f"{case.__name__}.{method} is missing",
                )
                exposed = [
                    name
                    for name in loader.getTestCaseNames(case)
                    if getattr(case, name) is getattr(case, method)
                ]
                self.assertTrue(
                    exposed,
                    f"{case.__name__}.{method} is registered for {identifier} but "
                    "is not exposed under a test_ name, so it would never run",
                )


if __name__ == "__main__":
    unittest.main()

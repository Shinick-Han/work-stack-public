"""The durable journal, exercised through the actual remote update flow.

Every recovery case here rebuilds both objects -- a new ``RemoteUpdateJournal``
over the same directory and a new ``RemoteUpdateFlow`` over that journal --
because that is what a desktop restart is.  Nothing is handed from one process
to the next except the bytes on disk.

The fake ports drive the flow only.  The journal under test is the real one,
writing real files into a real temporary directory, so the atomic replace, the
stale-writer gate and every fail-closed refusal are exercised as written.
The concurrency cases are real too: real threads, and a real second process.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
if str(SHELL) not in sys.path:
    sys.path.insert(0, str(SHELL))

import remote_update_journal as JOURNAL
from remote_update_flow import RemoteUpdateFlow
from remote_update_flow_contract import (
    ActivationOutcome,
    BackupOutcome,
    LostResponse,
    OwnerStopFacts,
    PendingOperation,
    PrepareOutcome,
    PreviewFacts,
    ProbeOutcome,
    RemoteUpdatePorts,
    RemoteUpdateRefused,
    RestoreOutcome,
    RollbackOutcome,
    StartupEvidence,
    VerifyOutcome,
)
from remote_update_journal import (
    JournalBinding,
    JournalBindingMismatch,
    JournalBindingRefused,
    JournalUnavailable,
    JournalUnreadable,
    JournalWriteRefused,
    JournalWriteUncertain,
    RemoteUpdateJournal,
)


BINDING = JournalBinding(
    workspace_id="ws-4a1c9f", profile_id="prof-7b2e", update_attempt_id="att-0031"
)
OTHER_PROFILE = JournalBinding(
    workspace_id="ws-4a1c9f", profile_id="prof-9d40", update_attempt_id="att-0031"
)

#: The state root once a writer has run: a leaked temp is a third name.
SETTLED_FILES = sorted([JOURNAL.JOURNAL_FILE, JOURNAL.JOURNAL_LOCK_FILE])

PREVIEW_OK = PreviewFacts(
    desktop_version="1.0.14",
    remote_version="1.0.13",
    served_ui_version="1.0.13",
    protocol_version="7",
    schema_version_before="v5",
    schema_version_target="v6",
    migration_required=True,
    install_capability="available",
    install_method="verified_unpack",
)
STOP_OK = OwnerStopFacts(
    state="dead",
    token_available=True,
    pidfd_available=True,
    process_exit="verified",
    listener_release="verified",
    lease_release="verified",
)
BACKUP_OK = BackupOutcome(status="verified", migration_required=True)
PREPARE_OK = PrepareOutcome(
    status="verified",
    capability="available",
    method="verified_unpack",
    previous_app_retained=True,
    previous_profile_retained=True,
)
PROBE_OK = ProbeOutcome(
    status="verified",
    workspace_match="verified",
    served_ui_version="1.0.14",
    protocol_version="7",
)
ACTIVATE_OK = ActivationOutcome(
    status="verified", committed=True, state="pending", restart_required=True
)
CONFIRM_OK = ActivationOutcome(
    status="verified",
    committed=True,
    state="confirmed",
    remote_version="1.0.14",
    schema_version="v6",
)
RESTORE_OK = RestoreOutcome(status="verified", pre_migration_data=True)
ROLLBACK_OK = RollbackOutcome(status="verified", previous_activation_selected=True)
VERIFY_OK = VerifyOutcome(
    status="verified",
    desktop_version="1.0.14",
    remote_version="1.0.14",
    served_ui_version="1.0.14",
    protocol_version="7",
    schema_version="v6",
)


class Port:
    """One effect port: replays scripted answers and records every call."""

    def __init__(self, name: str, calls: list[str], **scripts: object) -> None:
        self._name = name
        self._calls = calls
        self._scripts = {method: list(results) for method, results in scripts.items()}

    def __getattr__(self, method: str):
        if method.startswith("_") or method not in self._scripts:
            raise AttributeError(method)

        def call(operation_id: str = "") -> object:
            self._calls.append(f"{self._name}.{method}")
            results = self._scripts[method]
            if not results:
                raise AssertionError(f"unscripted call to {self._name}.{method}")
            answer = results.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return call


@contextmanager
def _staging_refused():
    """Refuse only the exclusive creation of a staging file.

    Reading has to keep working: the refusal being proved is a write that
    never reached the destination, not an unreadable journal.
    """

    real_open = Path.open

    def guarded(self, mode="r", *args, **kwargs):
        if "x" in mode:
            raise OSError("read-only")
        return real_open(self, mode, *args, **kwargs)

    with mock.patch.object(Path, "open", guarded):
        yield


class Desktop:
    """One desktop process: its own journal object and its own flow."""

    def __init__(self, state_root: Path, binding: JournalBinding = BINDING, **scripts):
        self.calls: list[str] = []
        self.issued: list[str] = []
        self.journal = RemoteUpdateJournal(state_root, binding)
        self.flow = RemoteUpdateFlow(
            RemoteUpdatePorts(
                preview=Port("preview", self.calls, describe=scripts.get("preview", (PREVIEW_OK,))),
                owner=Port(
                    "owner", self.calls,
                    stop=scripts.get("stop", (STOP_OK,)),
                    observe=scripts.get("stop_observe", ()),
                ),
                backup=Port(
                    "backup", self.calls,
                    create_verified=scripts.get("backup", (BACKUP_OK,)),
                    observe=scripts.get("backup_observe", ()),
                    restore=scripts.get("restore", ()),
                    observe_restore=scripts.get("restore_observe", ()),
                ),
                prepare=Port(
                    "prepare", self.calls,
                    prepare=scripts.get("prepare", (PREPARE_OK,)),
                    observe=scripts.get("prepare_observe", ()),
                ),
                probe=Port("probe", self.calls, probe=scripts.get("probe", (PROBE_OK,))),
                activation=Port(
                    "activation", self.calls,
                    activate=scripts.get("activate", (ACTIVATE_OK,)),
                    observe=scripts.get("activate_observe", ()),
                    confirm=scripts.get("confirm", (CONFIRM_OK,)),
                    observe_confirm=scripts.get("confirm_observe", ()),
                    rollback=scripts.get("rollback", ()),
                    observe_rollback=scripts.get("rollback_observe", ()),
                ),
                verification=Port(
                    "verification", self.calls, verify=scripts.get("verify", (VERIFY_OK,))
                ),
            ),
            operation_ids=self._operation_id,
            journal=self.journal,
        )

    def _operation_id(self, stage: str) -> str:
        issue = f"op-{stage}-{1 + sum(1 for name in self.issued if name == stage)}"
        self.issued.append(stage)
        return issue

    def run(self, steps: int) -> list[str]:
        return [self.flow.advance().code for _ in range(steps)]

    def resume(self, **fields: object) -> object:
        evidence = {
            "selected_activation_id": "op-activate-1",
            "authority_selected": "verified",
            "workspace_match": "verified",
        }
        evidence.update(fields)
        return self.flow.resume_after_restart(StartupEvidence(**evidence))


class JournalCase(unittest.TestCase):
    """A real directory beside which the journal file lives."""

    def setUp(self) -> None:
        self.state_root = Path(tempfile.mkdtemp(prefix="remote-update-journal-"))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        self.path = JOURNAL.journal_path(self.state_root)

    def desktop(self, binding: JournalBinding = BINDING, **scripts) -> Desktop:
        return Desktop(self.state_root, binding, **scripts)

    def reader(self, binding: JournalBinding = BINDING) -> RemoteUpdateJournal:
        return RemoteUpdateJournal(self.state_root, binding)

    def state_files(self) -> list[str]:
        return sorted(path.name for path in self.state_root.iterdir())

    def document(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write_document(self, document: object) -> None:
        self.path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    def valid_document(self, records: list, sequence: int = 1) -> dict:
        return {
            "schema_version": JOURNAL.JOURNAL_SCHEMA_VERSION,
            "binding": BINDING.document(),
            "binding_digest": BINDING.digest,
            "sequence": sequence,
            "writer": "writer-01",
            "records": records,
        }

    def confirmed_anchor(self) -> dict:
        return {
            "stage": "activate",
            "operation_id": "op-activate-1",
            "kind": "activation_confirmed",
            "previous_app_retained": "verified",
        }


class RestartRecoveryTests(JournalCase):
    """The settled restart cases, each across a genuine object recreation."""

    def test_a_lost_restore_and_its_confirmed_anchor_survive_the_process(self) -> None:
        first = self.desktop(
            verify=(VerifyOutcome(status="failed"),),
            restore=(LostResponse("op-backup_restore-1"),),
        )
        first.run(6)
        first.resume()
        self.assertEqual(first.flow.advance().code, "verify_failed")
        self.assertEqual(first.flow.restore().code, "restore_unknown")

        # The process is gone.  Only the file crosses the boundary.
        second = self.desktop(restore_observe=(RESTORE_OK,))
        loaded = second.journal.load()
        self.assertEqual(
            loaded,
            (
                PendingOperation("activate", "op-activate-1", "activation_confirmed", "verified"),
                PendingOperation(
                    "backup", "op-backup_restore-1", "backup_restore", "verified"
                ),
            ),
        )
        reloaded = second.flow.snapshot()
        self.assertEqual((reloaded.stage, reloaded.code), ("unknown", "restore_unknown"))
        self.assertEqual(second.flow.retained_activation(), loaded[0])

        settled = second.flow.reconcile()
        self.assertEqual(second.calls, ["backup.observe_restore"])
        self.assertEqual(
            (settled.stage, settled.code),
            ("failed", "restore_verified_activation_selected"),
        )
        self.assertEqual(settled.actions[0], "rollback_activation")
        self.assertNotIn("dismiss", settled.actions)
        self.assertNotIn("finish", settled.actions)

    def test_a_pending_activation_still_requires_its_restart_after_recreation(self) -> None:
        first = self.desktop()
        self.assertEqual(first.run(6)[-1], "activate_pending_restart")

        second = self.desktop()
        self.assertEqual(
            second.journal.load(),
            (PendingOperation("activate", "op-activate-1", "activation_pending", "verified"),),
        )
        reloaded = second.flow.snapshot()
        self.assertEqual((reloaded.stage, reloaded.code), ("activate", "activate_pending_restart"))
        with self.assertRaises(RemoteUpdateRefused) as refused:
            second.flow.advance()
        self.assertEqual(str(refused.exception), "advance_refused_restart_required")
        self.assertEqual(second.calls, [])

        # The pending anchor still demands matching selection evidence.
        self.assertEqual(second.resume(workspace_match="unknown").code, "activate_workspace_unknown")
        self.assertEqual(second.calls, [])
        self.assertEqual(second.journal.load()[0].kind, "activation_pending")
        self.assertEqual(second.resume().code, "activate_committed")
        self.assertEqual(second.journal.load()[0].kind, "activation_confirmed")

    def test_a_completed_update_clears_the_record_without_removing_the_file(self) -> None:
        first = self.desktop()
        first.run(6)
        first.resume()
        self.assertEqual(first.flow.advance().code, "update_ready")

        self.assertTrue(self.path.is_file())
        self.assertEqual(self.document()["records"], [])
        second = self.desktop()
        self.assertEqual(second.journal.load(), ())
        self.assertEqual(second.flow.snapshot().stage, "idle")
        # A cleared record is a legitimate fresh start, and the next process
        # continues the same sequence rather than restarting the counter.
        self.assertEqual(second.run(1), ["preview_ready"])
        self.assertGreater(self.document()["sequence"], 1)

    def test_a_verified_rollback_retires_the_anchor_it_no_longer_needs(self) -> None:
        first = self.desktop(
            verify=(VerifyOutcome(status="failed"),),
            restore=(RESTORE_OK,),
            rollback=(ROLLBACK_OK,),
        )
        first.run(6)
        first.resume()
        first.flow.advance()
        self.assertEqual(first.flow.restore().code, "restore_verified_activation_selected")
        self.assertEqual(first.flow.rollback().code, "rollback_verified")

        second = self.desktop()
        self.assertEqual(second.journal.load(), ())
        self.assertIsNone(second.flow.retained_activation())


class FailClosedLoadTests(JournalCase):
    """No parse, identity or I/O failure may ever read as an empty journal."""

    def test_only_a_missing_file_is_a_fresh_start(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertEqual(self.reader().load(), ())
        self.assertFalse(self.path.exists())

    def test_a_corrupt_journal_refuses_to_start_a_fresh_mutation(self) -> None:
        first = self.desktop()
        first.run(6)
        whole = self.path.read_bytes()
        truncated = whole[: len(whole) // 2]
        self.path.write_bytes(truncated)

        with self.assertRaises(JournalUnreadable):
            self.reader().load()
        # The flow loads in its constructor, so the refusal reaches the host
        # before any port can be called a second time.
        with self.assertRaises(JournalUnreadable):
            self.desktop()
        # Read-only means read-only: the only remaining evidence is untouched.
        self.assertEqual(self.path.read_bytes(), truncated)

    def test_an_unreadable_file_is_not_an_absent_one(self) -> None:
        self.write_document(self.valid_document([self.confirmed_anchor()]))
        journal = self.reader()
        with mock.patch.object(
            Path, "read_bytes", side_effect=PermissionError("locked")
        ):
            with self.assertRaises(JournalUnreadable):
                journal.load()

    def test_an_oversized_journal_is_refused(self) -> None:
        self.path.write_text("0" * (JOURNAL.MAX_JOURNAL_BYTES + 1), encoding="utf-8")
        with self.assertRaises(JournalUnreadable):
            self.reader().load()

    def test_an_unknown_document_field_is_refused(self) -> None:
        document = self.valid_document([])
        document["operator_note"] = "resumed by hand"
        self.write_document(document)
        with self.assertRaises(JournalUnreadable):
            self.reader().load()

    def test_a_foreign_schema_version_is_refused(self) -> None:
        document = self.valid_document([])
        document["schema_version"] = 2
        self.write_document(document)
        with self.assertRaises(JournalUnreadable):
            self.reader().load()

    def test_a_record_pairing_a_kind_with_a_foreign_stage_is_refused(self) -> None:
        anchor = self.confirmed_anchor()
        anchor["stage"] = "backup"
        self.write_document(self.valid_document([anchor]))
        with self.assertRaises(JournalUnreadable):
            self.reader().load()

    def test_an_unknown_kind_is_refused(self) -> None:
        anchor = self.confirmed_anchor()
        anchor["kind"] = "activation_forced"
        self.write_document(self.valid_document([anchor]))
        with self.assertRaises(JournalUnreadable):
            self.reader().load()

    def test_two_anchors_or_two_mutations_in_flight_are_refused(self) -> None:
        pending = dict(self.confirmed_anchor(), kind="activation_pending")
        self.write_document(self.valid_document([self.confirmed_anchor(), pending]))
        with self.assertRaises(JournalUnreadable):
            self.reader().load()

        restore = {
            "stage": "backup",
            "operation_id": "op-backup_restore-1",
            "kind": "backup_restore",
            "previous_app_retained": "verified",
        }
        create = dict(restore, kind="backup_create", operation_id="op-backup-1")
        self.write_document(self.valid_document([restore, create]))
        with self.assertRaises(JournalUnreadable):
            self.reader().load()

    def test_one_identity_recorded_twice_is_refused(self) -> None:
        anchor = self.confirmed_anchor()
        self.write_document(self.valid_document([anchor, dict(anchor)]))
        with self.assertRaises(JournalUnreadable):
            self.reader().load()

    def test_a_rollback_may_share_the_retained_receipt_identity(self) -> None:
        rollback = {
            "stage": "activate",
            "operation_id": "op-activate-1",
            "kind": "activation_rollback",
            "previous_app_retained": "verified",
        }
        self.write_document(self.valid_document([self.confirmed_anchor(), rollback]))
        loaded = self.reader().load()
        self.assertEqual([record.kind for record in loaded],
                         ["activation_confirmed", "activation_rollback"])

    def test_a_legacy_record_without_the_retained_field_reads_as_unknown(self) -> None:
        legacy_anchor = self.confirmed_anchor()
        del legacy_anchor["previous_app_retained"]
        legacy_restore = {
            "stage": "backup",
            "operation_id": "op-backup_restore-1",
            "kind": "backup_restore",
        }
        self.write_document(self.valid_document([legacy_anchor, legacy_restore]))
        loaded = self.reader().load()
        self.assertEqual(
            [record.previous_app_retained for record in loaded], ["unknown", "unknown"]
        )

        # And the flow's conservative end follows from it: the open pairing is
        # named, never a rollback offer and never a guessed pairing.
        restarted = self.desktop(restore_observe=(RESTORE_OK,))
        settled = restarted.flow.reconcile()
        self.assertEqual(settled.code, "restore_verified_activation_selected")
        self.assertEqual(
            list(settled.actions), ["resolve_activation_pairing", "inspect_diagnostics"]
        )
        with self.assertRaises(RemoteUpdateRefused) as refused:
            restarted.flow.rollback()
        self.assertEqual(refused.exception.code, "rollback_refused_no_previous_app")
        self.assertEqual([name for name in restarted.calls if name.startswith("activation")], [])

    def test_a_foreign_retention_answer_is_refused(self) -> None:
        anchor = dict(self.confirmed_anchor(), previous_app_retained="probably")
        self.write_document(self.valid_document([anchor]))
        with self.assertRaises(JournalUnreadable):
            self.reader().load()


class BindingTests(JournalCase):
    """One journal speaks for exactly one admitted update attempt."""

    def test_a_wildcard_or_absent_binding_value_is_refused(self) -> None:
        for value in ("*", "any", "", "unknown", "prof 7b2e", "profiles/prof-7b2e", None):
            with self.subTest(profile_id=value):
                with self.assertRaises(JournalBindingRefused):
                    JournalBinding(
                        workspace_id="ws-4a1c9f",
                        profile_id=value,
                        update_attempt_id="att-0031",
                    )
        with self.assertRaises(JournalBindingRefused):
            RemoteUpdateJournal(self.state_root, "ws-4a1c9f")

    def test_a_record_for_another_profile_is_refused_not_reported_empty(self) -> None:
        first = self.desktop()
        first.run(6)
        with self.assertRaises(JournalBindingMismatch):
            self.reader(OTHER_PROFILE).load()
        # A selection under a different admitted binding may not start fresh
        # over a record it is not allowed to read either.
        with self.assertRaises(JournalBindingMismatch):
            self.desktop(OTHER_PROFILE)
        self.assertEqual(self.reader().load()[0].operation_id, "op-activate-1")

    def test_a_tampered_binding_digest_is_a_mismatch(self) -> None:
        document = self.valid_document([self.confirmed_anchor()])
        document["binding_digest"] = "0" * 64
        self.write_document(document)
        with self.assertRaises(JournalBindingMismatch):
            self.reader().load()

    def test_the_body_carries_no_path_and_no_secret(self) -> None:
        first = self.desktop()
        first.run(6)
        body = self.path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.state_root), body)
        self.assertNotIn("\\\\", body)
        self.assertEqual(
            set(self.document()),
            {"schema_version", "binding", "binding_digest", "sequence", "writer", "records"},
        )
        self.assertEqual(self.document()["binding"], BINDING.document())


class WriteFailureTests(JournalCase):
    """A write either happens or leaves the previous record exactly as it was."""

    def test_a_failed_replace_preserves_the_record_and_claims_nothing(self) -> None:
        first = self.desktop()
        first.run(6)
        before = self.path.read_bytes()

        with mock.patch.object(JOURNAL.os, "replace", side_effect=OSError("no space")):
            with self.assertRaises(JournalWriteUncertain):
                first.journal.record(
                    (PendingOperation("activate", "op-activate-1", "activation_confirmed",
                                      "verified"),)
                )
        self.assertEqual(self.path.read_bytes(), before)
        # Nothing of this call's is left behind, and the outcome stays unknown:
        # this instance refuses further writes rather than guessing.
        self.assertEqual(self.state_files(), SETTLED_FILES)
        with self.assertRaises(JournalWriteUncertain):
            first.journal.record(())
        self.assertEqual(self.path.read_bytes(), before)
        # The host can still reload and see exactly what was on record.
        self.assertEqual(self.reader().load()[0].kind, "activation_pending")

    def test_a_staging_failure_is_a_refusal_that_wrote_nothing(self) -> None:
        first = self.desktop()
        first.run(6)
        before = self.path.read_bytes()

        with _staging_refused():
            with self.assertRaises(JournalWriteRefused):
                first.journal.record(())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.state_files(), SETTLED_FILES)
        # A refusal is not an uncertainty: this instance may still write.
        first.journal.record(())
        self.assertEqual(self.reader().load(), ())

    def test_an_inadmissible_record_never_reaches_the_file(self) -> None:
        journal = self.reader()
        self.assertEqual(journal.load(), ())
        for entries in (
            (PendingOperation("backup", "op-activate-1", "activation_confirmed", "verified"),),
            (PendingOperation("activate", "op-activate-1", "activation_forced", "verified"),),
            (PendingOperation("activate", "op activate 1", "activation_confirmed", "verified"),),
            (
                PendingOperation("activate", "op-activate-1", "activation_confirmed", "verified"),
                PendingOperation("activate", "op-activate-2", "activation_pending", "verified"),
            ),
        ):
            with self.subTest(entries=entries):
                with self.assertRaises(JournalUnreadable):
                    journal.record(entries)
        self.assertFalse(self.path.exists())


class StaleWriterTests(JournalCase):
    """A writer whose observation was overtaken writes nothing."""

    def test_a_stale_instance_refuses_to_overwrite_a_newer_record(self) -> None:
        first = self.desktop()
        first.run(6)
        stale = self.reader()
        self.assertEqual(stale.load()[0].kind, "activation_pending")

        # A competing instance advances the record.
        current = self.reader()
        current.load()
        current.record(())
        newer = self.path.read_bytes()

        with self.assertRaises(JournalWriteRefused):
            stale.record(())
        self.assertEqual(self.path.read_bytes(), newer)
        # The flow that owns the stale object is refused too, rather than
        # silently continuing over another instance's record.
        with self.assertRaises(JournalWriteRefused):
            first.resume()
        self.assertEqual(self.path.read_bytes(), newer)

    def test_a_writer_that_never_read_the_record_is_stale_by_definition(self) -> None:
        first = self.desktop()
        first.run(6)
        blind = self.reader()
        with self.assertRaises(JournalWriteRefused):
            blind.record(())
        self.assertEqual(self.reader().load()[0].kind, "activation_pending")

    def test_the_remembered_sequence_under_another_writer_is_refused(self) -> None:
        """A sequence is not an identity: the same number from another writer
        is a different record, and must not admit this one."""

        self.write_document(self.valid_document([self.confirmed_anchor()], sequence=5))
        journal = self.reader()
        self.assertEqual(journal.load()[0].kind, "activation_confirmed")
        overtaken = self.valid_document([], sequence=5)
        overtaken["writer"] = "writer-02"
        self.write_document(overtaken)
        with self.assertRaises(JournalWriteRefused):
            journal.record(())
        self.assertEqual(self.document()["writer"], "writer-02")

    def test_every_refusal_is_one_kind_a_host_can_catch(self) -> None:
        for failure in (
            JournalUnreadable,
            JournalBindingMismatch,
            JournalBindingRefused,
            JournalWriteRefused,
            JournalWriteUncertain,
        ):
            with self.subTest(failure=failure.__name__):
                self.assertTrue(issubclass(failure, JournalUnavailable))


#: A second real process that holds the real journal lock until told to stop.
_HOLDER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from remote_update_journal import remote_update_journal_lock
signals = Path(sys.argv[3])
with remote_update_journal_lock(Path(sys.argv[2])):
    (signals / "held").write_text("1", encoding="utf-8")
    deadline = time.monotonic() + 60.0
    while not (signals / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
"""


class SharedLockTests(JournalCase):
    """Read, admission and replace are one critical section for everybody."""

    def seed(self) -> None:
        seeded = self.reader()
        seeded.load()
        entry = PendingOperation("activate", "op-seed", "activation_pending", "verified")
        seeded.record((entry,))

    def race(self) -> tuple[dict, dict]:
        """Put two loaded instances through ``record`` simultaneously: the
        barrier sits in front of the lock rather than inside it, so both
        threads are past their own load and contend for one gate at once."""

        journals = {"first": self.reader(), "second": self.reader()}
        for journal in journals.values():
            self.assertEqual(journal.load()[0].operation_id, "op-seed")
        barrier = threading.Barrier(len(journals), timeout=30)
        real_lock = JOURNAL.remote_update_journal_lock
        outcomes: dict[str, JournalUnavailable | None] = {}

        @contextmanager
        def barriered(state_root):
            barrier.wait()
            with real_lock(state_root):
                yield

        def attempt(name: str) -> None:
            entry = PendingOperation("activate", f"op-{name}", "activation_confirmed", "verified")
            try:
                journals[name].record((entry,))
                outcomes[name] = None
            except JournalUnavailable as error:
                outcomes[name] = error

        with mock.patch.object(JOURNAL, "remote_update_journal_lock", barriered):
            threads = [threading.Thread(target=attempt, args=(n,)) for n in journals]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        self.assertEqual(sorted(outcomes), sorted(journals), "a racer never finished")
        return outcomes, journals

    def test_two_simultaneous_instances_commit_exactly_once(self) -> None:
        self.seed()
        outcomes, journals = self.race()
        committed = [name for name, error in outcomes.items() if error is None]
        refused = [name for name, error in outcomes.items() if error is not None]

        self.assertEqual(len(committed), 1, outcomes)
        self.assertIsInstance(outcomes[refused[0]], JournalWriteRefused)
        # Advanced once, not twice, and holding the winner's record alone.
        document = self.document()
        self.assertEqual(document["sequence"], 2)
        recorded = [entry["operation_id"] for entry in document["records"]]
        self.assertEqual(recorded, [f"op-{committed[0]}"])
        # The refused write staged nothing, so it left nothing behind.
        self.assertEqual(self.state_files(), SETTLED_FILES)

        # The loser stays refused: its observation was never the one on disk.
        loser = journals[refused[0]]
        with self.assertRaises(JournalWriteRefused):
            loser.record(())
        self.assertEqual(self.document()["sequence"], 2)
        # Only an admitted reload readmits it: a refusal is not a poisoning.
        self.assertEqual(loser.load()[0].kind, "activation_confirmed")
        loser.record(())
        self.assertEqual(self.reader().load(), ())
        self.assertEqual(self.document()["sequence"], 3)

    @contextmanager
    def holder(self):
        signals = Path(tempfile.mkdtemp(prefix="remote-update-journal-signal-"))
        self.addCleanup(shutil.rmtree, signals, True)
        argv = [sys.executable, "-c", _HOLDER, str(SHELL), str(self.state_root)]
        child = subprocess.Popen(argv + [str(signals)])
        try:
            deadline = time.monotonic() + 60.0
            while not (signals / "held").exists():
                if child.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError("the holder never took the journal lock")
                time.sleep(0.01)
            yield
        finally:
            (signals / "release").write_text("1", encoding="utf-8")
            child.wait(timeout=60)

    def test_a_lock_held_by_another_process_refuses_within_its_bound(self) -> None:
        self.seed()
        writer = self.reader()
        self.assertEqual(writer.load()[0].operation_id, "op-seed")
        before = self.path.read_bytes()
        lock_file = JOURNAL.journal_lock_path(self.state_root).stat()

        with self.holder():
            with mock.patch.object(JOURNAL, "LOCK_CONTENTION_SECONDS", 0.2):
                started = time.monotonic()
                with self.assertRaises(JournalWriteRefused):
                    writer.record(())
            # A refusal at the deadline, before any journal effect at all.
            self.assertLess(time.monotonic() - started, 30.0)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.state_files(), SETTLED_FILES)

        self.assertGreater(JOURNAL.LOCK_CONTENTION_SECONDS, 0.0)
        # The holder gone, it writes; the shared lock file is still one file.
        writer.record(())
        self.assertEqual(self.reader().load(), ())
        after = JOURNAL.journal_lock_path(self.state_root).stat()
        self.assertEqual((lock_file.st_dev, lock_file.st_ino), (after.st_dev, after.st_ino))
        self.assertEqual(self.state_files(), SETTLED_FILES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

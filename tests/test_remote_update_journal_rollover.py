"""The second legitimate update on one desktop, and everything it may not do.

One completed remote update used to end remote update for that desktop state
root forever: the journal file survives with its binding, the next attempt
arrives under a new ``update_attempt_id``, and the first read of the record
refused it.  The rule under test here is the narrow one that fixes it without
giving a fresh attempt anything else.

``load()`` never substitutes an answer.  It returns the records the file
itself carries, or it raises.  Exactly two files carry no records: the absent
file, and a *settled* record of another attempt -- admitted whole, agreeing
with its own binding digest, ``records`` empty.  That record names no mutation
in flight and no activation anchor, which is the same state its own next
process would find, so it is superseded rather than adopted.  Anything else
about another attempt is refused exactly as before.

Nothing here is a mock of the subject.  The journal is the real one writing
real files; the flow that fills it is the real ``RemoteUpdateFlow`` (its effect
ports scripted, as the journal lane already does); and both the refused and the
admitted composition go through the real ``build_remote_update_flow``, which is
where the defect actually surfaced.  No SSH, no socket, no live store.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
TESTS = ROOT / "tests"
for _entry in (str(ROOT), str(SHELL)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import remote_update_host_factory as FACTORY  # noqa: E402
import remote_update_journal as JOURNAL  # noqa: E402
from remote_update_flow_contract import LostResponse, PendingOperation  # noqa: E402
from remote_update_journal import (  # noqa: E402
    JournalBinding,
    JournalBindingMismatch,
    JournalUnavailable,
    JournalUnreadable,
    JournalWriteRefused,
    RemoteUpdateJournal,
)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The two existing lanes, reused as fixture libraries rather than copied.
#: ``Desktop`` is the real flow over the real journal with scripted effect
#: ports; ``host_inputs`` is the real host input shape the factory admits.
JOURNAL_TESTS = load_module(TESTS / "test_remote_update_journal.py", "rollover_journal_lane")
FACTORY_TESTS = load_module(
    TESTS / "test_remote_update_host_factory.py", "rollover_factory_lane"
)

ATTEMPT_A = "attempt-a-0001"
ATTEMPT_B = "attempt-b-0002"

MAX_FILE_LINES = 800
MAX_FUNCTION_LINES = 100
MAX_CCN = 15


def binding_for(attempt: str, profile: str = FACTORY_TESTS.PROFILE_ID) -> JournalBinding:
    """Exactly the binding ``_journal`` mints for these host inputs."""

    return JournalBinding(
        workspace_id=FACTORY_TESTS.WORKSPACE,
        profile_id=profile,
        update_attempt_id=attempt,
    )


class RolloverCase(unittest.TestCase):
    """One disposable desktop state root, shared by both attempts."""

    def setUp(self) -> None:
        self.state_root = Path(tempfile.mkdtemp(prefix="remote-update-rollover-"))
        self.addCleanup(shutil.rmtree, self.state_root, True)
        self.path = JOURNAL.journal_path(self.state_root)

    # -- the two attempts -------------------------------------------------

    def compose(self, attempt: str, **overrides: object):
        """Attempt ``attempt``, composed by the production factory itself."""

        return FACTORY.build_remote_update_flow(
            FACTORY_TESTS.host_inputs(
                self.state_root, update_attempt_id=attempt, **overrides
            )
        )

    def refused_code(self, attempt: str, **overrides: object) -> str:
        with self.assertRaises(FACTORY.RemoteUpdateFactoryRefused) as refused:
            self.compose(attempt, **overrides)
        return refused.exception.code

    def desktop(self, attempt: str, profile: str = FACTORY_TESTS.PROFILE_ID, **scripts):
        """The real flow and the real journal for one attempt's process."""

        return JOURNAL_TESTS.Desktop(self.state_root, binding_for(attempt, profile), **scripts)

    def complete(self, attempt: str = ATTEMPT_A) -> None:
        """Run one attempt all the way to ``update_ready``, as a desktop does."""

        first = self.desktop(attempt)
        first.run(6)
        first.resume()
        self.assertEqual(first.flow.advance().code, "update_ready")
        self.assertEqual(self.document()["records"], [])

    def leave_pending(self, attempt: str = ATTEMPT_A) -> None:
        """Stop one attempt where its activation is still unreconciled."""

        first = self.desktop(attempt)
        first.run(6)
        self.assertEqual(self.document()["records"][0]["kind"], "activation_pending")

    # -- the file ---------------------------------------------------------

    def document(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write_document(self, document: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(document) + "\n", encoding="utf-8")

    def settled_document(self, attempt: str, sequence: int = 4) -> dict:
        """What a completed attempt really leaves: a record with no records."""

        binding = binding_for(attempt)
        return {
            "schema_version": JOURNAL.JOURNAL_SCHEMA_VERSION,
            "binding": binding.document(),
            "binding_digest": binding.digest,
            "sequence": sequence,
            "writer": "writer-a1",
            "records": [],
        }

    def anchor(self) -> dict:
        return {
            "stage": "activate",
            "operation_id": "op-activate-1",
            "kind": "activation_confirmed",
            "previous_app_retained": "verified",
        }


class SupersedeSettledTests(RolloverCase):
    """A completed attempt frees the record it has nothing left to say about."""

    def test_a_completed_attempt_does_not_end_remote_update_for_the_desktop(self) -> None:
        # Attempt A is the real composition first, so the binding the journal
        # ends up holding is the one the production factory actually mints.
        composed = self.compose(ATTEMPT_A)
        self.assertEqual(composed.journal.binding, binding_for(ATTEMPT_A))
        self.assertIsNone(composed.journal.superseded)
        self.complete(ATTEMPT_A)
        settled = self.path.read_bytes()

        # This is the exact call that used to raise REFUSED_JOURNAL forever.
        second = self.compose(ATTEMPT_B)
        self.assertEqual(second.update_attempt_id, ATTEMPT_B)
        self.assertEqual(second.flow.snapshot().stage, "idle")
        self.assertEqual(second.flow.snapshot().code, "idle")
        self.assertIsNone(second.flow.retained_activation())
        # It is superseded, not adopted: B knows whose settled record it stands
        # on, and reading it changed not one byte.
        self.assertEqual(second.journal.superseded, binding_for(ATTEMPT_A))
        self.assertEqual(second.journal.load(), ())
        self.assertEqual(self.path.read_bytes(), settled)

    def test_the_second_attempt_takes_the_record_over_only_by_writing(self) -> None:
        self.complete(ATTEMPT_A)
        before = self.document()

        self.compose(ATTEMPT_B)
        self.assertEqual(self.document(), before)

        # The takeover is an ordinary write -- the first mutation B issues,
        # through the same lock and the same gate -- and the sequence keeps
        # counting rather than restarting under the new binding.
        second = self.desktop(ATTEMPT_B)
        self.assertEqual(second.run(1), ["preview_ready"])
        self.assertEqual(self.document(), before, "a read-only preview wrote nothing")
        self.assertEqual(second.run(1), ["stop_verified"])
        after = self.document()
        self.assertEqual(after["binding"], binding_for(ATTEMPT_B).document())
        self.assertEqual(after["binding_digest"], binding_for(ATTEMPT_B).digest)
        self.assertGreater(after["sequence"], before["sequence"])
        self.assertNotEqual(after["writer"], before["writer"])
        # One journal and one lock file: no per-attempt file was minted.
        self.assertEqual(
            sorted(path.name for path in self.state_root.iterdir()),
            sorted([JOURNAL.JOURNAL_FILE, JOURNAL.JOURNAL_LOCK_FILE]),
        )

    def test_a_profile_switch_after_a_completed_attempt_is_admitted(self) -> None:
        """The proof is the empty record, not which binding field moved."""

        self.complete(ATTEMPT_A)
        switched = self.compose(
            ATTEMPT_B,
            profile=FACTORY_TESTS.ssh_profile(profile_id=FACTORY_TESTS.OTHER_PROFILE_ID),
        )
        self.assertEqual(switched.profile_id, FACTORY_TESTS.OTHER_PROFILE_ID)
        self.assertEqual(switched.journal.superseded, binding_for(ATTEMPT_A))
        self.assertEqual(switched.flow.snapshot().stage, "idle")

    def test_a_settled_record_of_this_very_attempt_is_not_superseded(self) -> None:
        """Its own cleared record is adopted, exactly as it always was."""

        self.complete(ATTEMPT_A)
        again = self.compose(ATTEMPT_A)
        self.assertIsNone(again.journal.superseded)
        self.assertEqual(again.flow.snapshot().stage, "idle")

    def test_an_absent_record_supersedes_nothing(self) -> None:
        self.assertFalse(self.path.exists())
        first = self.compose(ATTEMPT_A)
        self.assertIsNone(first.journal.superseded)
        self.assertFalse(self.path.exists())


class PendingIsUntouchableTests(RolloverCase):
    """An unreconciled attempt still refuses every other attempt, as before."""

    def test_a_pending_activation_refuses_a_fresh_attempt_and_keeps_its_record(self) -> None:
        self.leave_pending(ATTEMPT_A)
        recorded = self.path.read_bytes()

        self.assertEqual(self.refused_code(ATTEMPT_B), FACTORY.REFUSED_JOURNAL)
        self.assertEqual(self.path.read_bytes(), recorded)

        reader = RemoteUpdateJournal(self.state_root, binding_for(ATTEMPT_B))
        with self.assertRaises(JournalBindingMismatch):
            reader.load()
        self.assertIsNone(reader.superseded)
        # And it may not erase what it may not read, either.
        with self.assertRaises(JournalUnavailable):
            reader.record(())
        self.assertEqual(self.path.read_bytes(), recorded)
        # A's own identity is still exactly where A left it.
        own = RemoteUpdateJournal(self.state_root, binding_for(ATTEMPT_A)).load()
        self.assertEqual([record.kind for record in own], ["activation_pending"])

    def test_a_mutation_in_flight_refuses_a_fresh_attempt(self) -> None:
        """An unknown stop is the case a second issue would be worst in."""

        first = self.desktop(ATTEMPT_A, stop=(LostResponse("op-stop-1"),))
        self.assertEqual(first.run(2), ["preview_ready", "stop_incomplete_unknown"])
        recorded = self.path.read_bytes()
        self.assertEqual(self.document()["records"][0]["kind"], "owner_stop")

        self.assertEqual(self.refused_code(ATTEMPT_B), FACTORY.REFUSED_JOURNAL)
        self.assertEqual(self.path.read_bytes(), recorded)

    def test_a_profile_switch_over_a_pending_attempt_is_refused(self) -> None:
        self.leave_pending(ATTEMPT_A)
        recorded = self.path.read_bytes()
        code = self.refused_code(
            ATTEMPT_A,
            profile=FACTORY_TESTS.ssh_profile(profile_id=FACTORY_TESTS.OTHER_PROFILE_ID),
        )
        self.assertEqual(code, FACTORY.REFUSED_JOURNAL)
        self.assertEqual(self.path.read_bytes(), recorded)

    def test_the_superseded_attempt_may_not_come_back_over_the_new_one(self) -> None:
        """Isolation runs both ways: A gets no more claim than B ever had."""

        self.complete(ATTEMPT_A)
        self.desktop(ATTEMPT_B).run(6)
        held = self.path.read_bytes()
        self.assertEqual(self.document()["records"][0]["kind"], "activation_pending")

        self.assertEqual(self.refused_code(ATTEMPT_A), FACTORY.REFUSED_JOURNAL)
        self.assertEqual(self.path.read_bytes(), held)


class AmbiguousEvidenceTests(RolloverCase):
    """Empty records retire a record only when the document proves it is one."""

    def assert_untouched_refusal(self, expected: type[JournalUnavailable]) -> None:
        recorded = self.path.read_bytes()
        reader = RemoteUpdateJournal(self.state_root, binding_for(ATTEMPT_B))
        with self.assertRaises(expected):
            reader.load()
        self.assertIsNone(reader.superseded)
        self.assertEqual(self.refused_code(ATTEMPT_B), FACTORY.REFUSED_JOURNAL)
        self.assertEqual(self.path.read_bytes(), recorded)

    def test_a_foreign_schema_version_is_never_retired(self) -> None:
        document = self.settled_document(ATTEMPT_A)
        document["schema_version"] = 2
        self.write_document(document)
        self.assert_untouched_refusal(JournalUnreadable)

    def test_an_unknown_document_field_is_never_retired(self) -> None:
        document = self.settled_document(ATTEMPT_A)
        document["retired_by"] = "attempt-b-0002"
        self.write_document(document)
        self.assert_untouched_refusal(JournalUnreadable)

    def test_a_document_disagreeing_with_its_own_digest_is_never_retired(self) -> None:
        """The one hand-editable field that could have forged a settled record."""

        document = self.settled_document(ATTEMPT_A)
        document["binding_digest"] = binding_for(ATTEMPT_B).digest
        self.write_document(document)
        self.assert_untouched_refusal(JournalBindingMismatch)

        document = self.settled_document(ATTEMPT_A)
        document["binding_digest"] = "0" * 64
        self.write_document(document)
        self.assert_untouched_refusal(JournalBindingMismatch)

    def test_a_truncated_document_is_never_retired(self) -> None:
        self.complete(ATTEMPT_A)
        whole = self.path.read_bytes()
        self.path.write_bytes(whole[: len(whole) // 2])
        self.assert_untouched_refusal(JournalUnreadable)

    def test_an_oversized_document_is_never_retired(self) -> None:
        self.path.write_text("0" * (JOURNAL.MAX_JOURNAL_BYTES + 1), encoding="utf-8")
        self.assert_untouched_refusal(JournalUnreadable)

    def test_a_binding_value_that_names_a_location_is_never_retired(self) -> None:
        document = self.settled_document(ATTEMPT_A)
        document["binding"]["update_attempt_id"] = "../../attempts/a"
        self.write_document(document)
        recorded = self.path.read_bytes()
        with self.assertRaises(JournalUnavailable):
            RemoteUpdateJournal(self.state_root, binding_for(ATTEMPT_B)).load()
        self.assertEqual(self.path.read_bytes(), recorded)

    def test_a_foreign_record_a_fresh_attempt_cannot_parse_is_left_alone(self) -> None:
        document = self.settled_document(ATTEMPT_A)
        document["records"] = [dict(self.anchor(), kind="activation_forced")]
        self.write_document(document)
        self.assert_untouched_refusal(JournalUnavailable)

    def test_a_foreign_record_this_attempt_could_parse_is_still_refused(self) -> None:
        document = self.settled_document(ATTEMPT_A)
        document["records"] = [self.anchor()]
        self.write_document(document)
        self.assert_untouched_refusal(JournalBindingMismatch)


class SupersedingWriterTests(RolloverCase):
    """Taking a settled record over does not weaken the write gate at all."""

    def test_a_stale_attempt_cannot_overwrite_the_attempt_that_took_over(self) -> None:
        self.complete(ATTEMPT_A)
        stale = RemoteUpdateJournal(self.state_root, binding_for(ATTEMPT_A))
        self.assertEqual(stale.load(), ())

        self.desktop(ATTEMPT_B).run(6)
        held = self.path.read_bytes()
        self.assertEqual(self.document()["binding"], binding_for(ATTEMPT_B).document())

        with self.assertRaises(JournalUnavailable):
            stale.record(())
        self.assertEqual(self.path.read_bytes(), held)
        with self.assertRaises(JournalUnavailable):
            stale.record(
                (PendingOperation("backup", "op-backup-9", "backup_create", "unknown"),)
            )
        self.assertEqual(self.path.read_bytes(), held)

    def test_a_stale_attempt_is_refused_even_when_the_taker_left_it_settled(self) -> None:
        """The sequence-and-writer gate answers this one, not the binding."""

        self.complete(ATTEMPT_A)
        stale = RemoteUpdateJournal(self.state_root, binding_for(ATTEMPT_A))
        self.assertEqual(stale.load(), ())

        taker = RemoteUpdateJournal(self.state_root, binding_for(ATTEMPT_B))
        self.assertEqual(taker.load(), ())
        taker.record(())
        newer = self.path.read_bytes()
        self.assertEqual(self.document()["binding"], binding_for(ATTEMPT_B).document())

        with self.assertRaises(JournalWriteRefused):
            stale.record(())
        self.assertEqual(self.path.read_bytes(), newer)

    def test_two_fresh_attempts_over_one_settled_record_commit_once(self) -> None:
        self.complete(ATTEMPT_A)
        first = RemoteUpdateJournal(self.state_root, binding_for(ATTEMPT_B))
        second = RemoteUpdateJournal(self.state_root, binding_for("attempt-c-0003"))
        self.assertEqual(first.load(), ())
        self.assertEqual(second.load(), ())

        first.record((PendingOperation("stop", "op-stop-1", "owner_stop", "unknown"),))
        # The loser is refused by the record it re-reads under the lock: the
        # settled record it was admitted over is gone, and what stands in its
        # place is another attempt's identity in flight, which is nobody
        # else's to touch.
        with self.assertRaises(JournalBindingMismatch):
            second.record((PendingOperation("stop", "op-stop-2", "owner_stop", "unknown"),))
        self.assertEqual(
            [entry["operation_id"] for entry in self.document()["records"]], ["op-stop-1"]
        )

    def test_a_blind_superseding_writer_is_still_stale_by_definition(self) -> None:
        self.complete(ATTEMPT_A)
        settled = self.path.read_bytes()
        blind = RemoteUpdateJournal(self.state_root, binding_for(ATTEMPT_B))
        with self.assertRaises(JournalWriteRefused):
            blind.record(())
        self.assertEqual(self.path.read_bytes(), settled)


class SameAttemptRecoveryTests(RolloverCase):
    """Nothing here weakens the recovery the journal exists for."""

    def test_the_same_attempt_still_resumes_its_pending_activation(self) -> None:
        self.leave_pending(ATTEMPT_A)
        recorded = self.path.read_bytes()

        resumed = self.compose(ATTEMPT_A)
        self.assertIsNone(resumed.journal.superseded)
        snapshot = resumed.flow.snapshot()
        self.assertEqual(snapshot.stage, "activate")
        self.assertEqual(snapshot.code, "activate_pending_restart")
        retained = resumed.flow.retained_activation()
        self.assertIsNotNone(retained)
        self.assertEqual(retained.operation_id, "op-activate-1")
        self.assertEqual(self.path.read_bytes(), recorded)

    def test_the_same_attempt_still_resumes_a_lost_mutation(self) -> None:
        first = self.desktop(ATTEMPT_A, stop=(LostResponse("op-stop-1"),))
        first.run(2)
        recorded = self.path.read_bytes()

        resumed = self.compose(ATTEMPT_A)
        snapshot = resumed.flow.snapshot()
        self.assertEqual(snapshot.stage, "unknown")
        self.assertEqual(snapshot.code, "stop_incomplete_unknown")
        self.assertEqual(self.path.read_bytes(), recorded)


class StructuralTests(unittest.TestCase):
    """The changed module stays inside the repository's own metrics."""

    def test_the_journal_stays_inside_repo_metrics(self) -> None:
        path = SHELL / "remote_update_journal.py"
        source = path.read_text(encoding="utf-8")
        self.assertLessEqual(len(source.splitlines()), MAX_FILE_LINES)
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            with self.subTest(function=node.name):
                self.assertLessEqual(length, MAX_FUNCTION_LINES)
                self.assertLessEqual(FACTORY_TESTS.complexity(node), MAX_CCN)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

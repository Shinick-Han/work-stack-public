"""Compatibility contracts the service extraction must keep satisfying.

``workstack/service.py`` was one 4963-line module. Its domain now lives in
``workstack/service_*`` modules that the facade composes. Everything asserted
here is a property a consumer already depends on and that a file split is
uniquely able to break: the import path, the error identities, the clock
substitution seam, the method resolution order, and the absence of a cycle
between the facade and the modules it composes.

These are narrow extraction regressions only. Domain behaviour keeps being
covered by the existing contract suites.
"""

from __future__ import annotations

import ast
import datetime
import importlib
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from workstack import service
from workstack.service import DomainError, WorkStack
from workstack.store import Store

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "workstack"

# Every name a consumer imports from ``workstack.service`` today. server.py,
# cli.py, cli_read_transport.py, agent_local_backend.py, the scripts and the
# test suite between them import all of these from this ONE path.
PUBLIC_NAMES = (
    "CheckpointTransitionConflictError",
    "DomainError",
    "IdempotencyConflictError",
    "NotFoundError",
    "ReplyReceiptConflictError",
    "RevisionConflictError",
    "RevisionExhaustedError",
    "SnapshotDisclosureRequiredError",
    "SnapshotExportConflictError",
    "SnapshotExportRefusedError",
    "SnapshotStoreNotReadyError",
    "SourceRevisionConflictError",
    "StaleCaptureError",
    "TaskDeletionTransactionError",
    "WorkSessionConflictError",
    "WorkStack",
    "current_quarter",
    "today",
    "utc_now",
)

# One representative public operation per extracted domain module, so a module
# that stopped being composed into the facade cannot pass unnoticed.
FACADE_OPERATIONS = (
    "add_task",
    "add_task_cli",
    "add_worklog_v1",
    "apply_checkpoint_transition_v1",
    "apply_reply_receipt",
    "approve_reply",
    "create_objective_v1",
    "create_task_v1",
    "ingest_capture",
    "list_checkpoint_audit",
    "patch_task",
    "search_projection",
    "snapshot",
    "start_work_session_v1",
    "storage_status",
    "task_detail",
    "weekly_report",
)


def service_modules() -> list[str]:
    return sorted(path.stem for path in PACKAGE.glob("service_*.py"))


class ImportSurface(unittest.TestCase):
    def test_every_consumed_name_is_still_importable_from_workstack_service(self) -> None:
        for name in PUBLIC_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hasattr(service, name), name)

    def test_a_refusal_class_is_one_object_however_it_is_reached(self) -> None:
        """A caller catching ``service.DomainError`` must catch the raised one."""

        from workstack import service_errors

        self.assertIs(service.DomainError, service_errors.DomainError)
        self.assertIs(service.NotFoundError, service_errors.NotFoundError)
        with tempfile.TemporaryDirectory() as base:
            stack = WorkStack(Store(Path(base)))
            with self.assertRaises(DomainError):
                stack.patch_task("T-1", {"revision": 0, "status": "nonsense"})

    def test_the_facade_still_answers_every_extracted_domain(self) -> None:
        for name in FACADE_OPERATIONS:
            with self.subTest(operation=name):
                self.assertTrue(callable(getattr(WorkStack, name, None)), name)


class ClockSubstitutionSeam(unittest.TestCase):
    """``workstack.service.today``/``utc_now`` must still reach the domain."""

    def test_a_patched_clock_reaches_a_write_that_now_lives_in_a_mixin(self) -> None:
        day = "2031-04-05"
        moment = day + "T06:07:08Z"
        with tempfile.TemporaryDirectory() as base:
            stack = WorkStack(Store(Path(base)))
            with mock.patch("workstack.service.today", return_value=day), mock.patch(
                "workstack.service.utc_now", return_value=moment
            ):
                task = stack.add_task("Clock seam")
                objective = stack.add_objective("Clock seam objective", quarter="2031-Q2")
                note = stack.add_note("Clock seam note")
        self.assertEqual(task["created"], day)
        self.assertEqual(task["updated_at"], day)
        self.assertEqual(objective["created"], day)
        self.assertEqual(note["created"], day)

    def test_created_and_updated_at_stay_two_independent_clock_reads(self) -> None:
        """A create spanning midnight keeps both reads, as the owner contract proves."""

        with tempfile.TemporaryDirectory() as base:
            stack = WorkStack(Store(Path(base)))
            with mock.patch(
                "workstack.service.today", side_effect=["2031-04-05", "2031-04-06"]
            ), mock.patch("workstack.service.utc_now", return_value="2031-04-05T00:00:00Z"):
                task = stack.add_task("Two reads")
        self.assertEqual((task["created"], task["updated_at"]), ("2031-04-05", "2031-04-06"))

    def test_a_patched_quarter_still_defaults_a_new_objective(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            stack = WorkStack(Store(Path(base)))
            with mock.patch("workstack.service.current_quarter", return_value="2099-Q4"):
                objective = stack.add_objective("Quarter seam")
        self.assertEqual(objective["quarter"], "2099-Q4")


class FrozenDatetimeModule(datetime.datetime):
    """A ``datetime`` whose ``now`` is frozen, mirroring the existing CLI fixtures."""

    FROZEN = datetime.datetime(2031, 4, 5, 9, 42, 17, tzinfo=datetime.timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.FROZEN if tz else cls.FROZEN.replace(tzinfo=None)


class FrozenDate(datetime.date):
    @classmethod
    def today(cls) -> datetime.date:
        return datetime.date(2031, 4, 5)


class DatetimeModuleSeam(unittest.TestCase):
    """``mock.patch.object(service, "dt", ...)`` must still reach the domain.

    ``tests/test_cli_worklog_checkin_writer_contract`` substitutes the datetime
    module on ``workstack.service`` to freeze the check-in default clock. The
    three clock READS the extracted modules perform must keep resolving through
    that name.
    """

    def frozen(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            date=FrozenDate,
            datetime=FrozenDatetimeModule,
            timezone=datetime.timezone,
            timedelta=datetime.timedelta,
        )

    def test_a_substituted_datetime_module_still_drives_every_clock_read(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            stack = WorkStack(Store(Path(base)))
            with mock.patch.object(service, "dt", self.frozen()):
                checked_in = stack.checkin(date="2031-04-05")
                weekly = stack.weekly_report()
                snapshot = stack.snapshot()
        self.assertEqual(checked_in["start_time"], "09:42")
        self.assertEqual(weekly["range"]["end"], "2031-04-05")
        self.assertEqual(weekly["range"]["start"], "2031-03-30")
        self.assertTrue(snapshot["generated_at"].startswith("2031-04-05T09:42:17"))


class CompositionOrder(unittest.TestCase):
    def test_domain_mixins_resolve_before_the_report_and_mutation_mixins(self) -> None:
        """The single-class facade won over both; the split must not flip that."""

        from workstack.mutation_service import MutationNoticeMixin
        from workstack.report_repository_service import ReportRepositoryServiceMixin
        from workstack.service_task_commands import TaskCommandsMixin

        order = WorkStack.__mro__
        self.assertLess(order.index(TaskCommandsMixin), order.index(ReportRepositoryServiceMixin))
        self.assertLess(order.index(TaskCommandsMixin), order.index(MutationNoticeMixin))

    def test_the_mutation_mixin_still_finds_the_helpers_it_calls_on_self(self) -> None:
        for name in (
            "_idempotency_replay",
            "_project_task",
            "_raw_request_digest",
            "_record_idempotency",
            "_validate_idempotency_key",
        ):
            with self.subTest(helper=name):
                self.assertTrue(callable(getattr(WorkStack, name, None)), name)


class NoCycleBackToTheFacade(unittest.TestCase):
    def test_no_extracted_module_imports_the_facade(self) -> None:
        """The facade composes the modules; a back edge would be a cycle."""

        for module in service_modules():
            with self.subTest(module=module):
                tree = ast.parse((PACKAGE / (module + ".py")).read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotIn(
                            (node.module or "").rsplit(".", 1)[-1],
                            {"service"},
                            module,
                        )
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            self.assertNotEqual(alias.name, "workstack.service", module)

    def test_each_extracted_module_imports_on_its_own(self) -> None:
        for module in service_modules():
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.import_module("workstack." + module))


if __name__ == "__main__":
    unittest.main()

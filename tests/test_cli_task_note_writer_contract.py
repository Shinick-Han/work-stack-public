"""Public wire contract for ``work-stack backlog note`` owner forwarding.

Everything here drives the public ``cli.main`` entry point and a real ephemeral
loopback wire. No private helper is called and no helper call sequence is
asserted: the subject is the observable CLI result and the bytes that reach the
server.

Two owners appear. ``create_server`` from :mod:`workstack.server` is the real
product owner, used for the success, parity and mutation-scope cases so the
response shapes are genuine rather than hand-invented. The real-owner proxy
relays complete responses and can drop them after the owner commits, which is
how the ambiguous replay is exercised against a real ledger. A separate
scripted owner uses partial, synthetic preflight payloads for focused protocol
cases, including stale-revision 409 and malformed bodies; these are not
captures.

Owner-aware routing is expected to be RED against the pre-implementation
baseline: ``backlog note`` still takes the exclusive-local Store path, so it
fails closed on the writer lease while an owner holds it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SESSION_PATH = "/api/v1/session"
STORAGE_PATH = "/api/v1/storage"
SYNC_PATH = "/api/v1/sync/status"
TASKS_PATH = "/api/v1/tasks"
NOTE_KEYS = ("date", "text")
MAX_REVISION = 2**53 - 1
KEY_PREFIX = "cli-note-"


def _result_root() -> Path | None:
    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "task-note-fixtures"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


# Strong, module-level references to children that were still running at an
# observation boundary. Nothing removes a live entry: the whole point is that
# the child, its fixture and its pending cleanup stay reachable.
class _OwnedChildCleanup:
    """Bounded cleanup of the ONE CLI child this fixture itself just spawned.

    The scope is the retained Popen OBJECT and nothing else. There is no pid or
    name lookup, no process tree, no signal sweep, no replacement child and no
    attaching to anything this fixture did not create: an existing review child,
    an app, a server or any other session is out of scope entirely.

    At most ONE ``terminate()`` call is made on that object, and every wait
    afterwards draws on a single cleanup deadline measured once. The deadline is
    never reset and there is no retry loop. Exit counts as verified only when the
    same object reports it; an exception on its own proves nothing.
    """

    def __init__(
        self, process: Any, command: list[str], receipt_path: Path, console: Any
    ) -> None:
        self.process = process
        self.command = list(command)
        self.receipt_path = receipt_path
        self.console = console
        self.record: dict[str, Any] = {}
        self.terminate_calls = 0
        self.already_exited = False
        self.reaped = False
        self.verified_exit = False
        self.returncode: int | None = None
        self.stdout: str | None = None
        self.stderr: str | None = None
        self.cleanup_deadline: float | None = None
        self.cleanup_errors: list[str] = []
        self.report_errors: list[str] = []
        self.sink_records: list[str] = []

    # -- normal path -------------------------------------------------------
    def normal_reap(self, out: str, err: str) -> None:
        """A child that finished inside the observation window, refusal included."""

        self.stdout = out
        self.stderr = err
        self.returncode = self.process.returncode
        self.reaped = True
        self.verified_exit = True

    # -- bounded fault path ------------------------------------------------
    def remaining(self) -> float:
        return self.cleanup_deadline - time.monotonic()

    def cleanup(self) -> None:
        if self.reaped:
            return
        # Measured once, here, and never reset.
        self.cleanup_deadline = time.monotonic() + CLEANUP_DEADLINE_SECONDS
        try:
            exited = self.process.poll()
        except BaseException as failure:  # noqa: BLE001 - retained, not proof
            self.cleanup_errors.append(f"poll: {failure!r}")
            exited = None
        if exited is not None:
            # The same object says it is already gone; its pipes are still reaped.
            self.already_exited = True
        else:
            # Counted before the call, so "at most one" holds even if it raises.
            self.terminate_calls += 1
            try:
                self.process.terminate()
            except BaseException as failure:  # noqa: BLE001
                self.cleanup_errors.append(f"terminate: {failure!r}")
        remaining = self.remaining()
        if remaining <= 0:
            self.cleanup_errors.append("the cleanup deadline expired before the reap")
            return
        try:
            out, err = self.process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as expiry:
            self.cleanup_errors.append(f"bounded reap timed out: {expiry!r}")
            return
        except BaseException as failure:  # noqa: BLE001
            self.cleanup_errors.append(f"bounded reap: {failure!r}")
            return
        self.stdout = out
        self.stderr = err
        try:
            code = self.process.poll()
        except BaseException as failure:  # noqa: BLE001
            self.cleanup_errors.append(f"exit check: {failure!r}")
            return
        if code is None:
            self.cleanup_errors.append("the child never confirmed its exit")
            return
        self.returncode = code
        self.reaped = True
        self.verified_exit = True

    # -- reporting ---------------------------------------------------------
    def describe(self, stage: str) -> str:
        visible = dict(self.record)
        visible.pop("process", None)
        visible.update({
            "stage": stage,
            "command": self.command,
            "terminate_calls": self.terminate_calls,
            "already_exited": self.already_exited,
            "reaped": self.reaped,
            "verified_exit": self.verified_exit,
            "returncode": self.returncode,
            "cleanup_errors": list(self.cleanup_errors),
            "report_errors": list(self.report_errors),
        })
        return json.dumps(visible, default=str, sort_keys=True)

    def report(self, stage: str) -> None:
        """Both sinks attempted independently; neither may gate cleanup."""

        try:
            payload = self.describe(stage)
        except BaseException as failure:  # noqa: BLE001
            self.report_errors.append(f"describe {stage}: {failure!r}")
            payload = json.dumps({"stage": stage, "command": self.command})
        try:
            self.console.write("OWNED CHILD " + payload + NEWLINE)
            self.console.flush()
            self.sink_records.append("console:" + stage)
        except BaseException as failure:  # noqa: BLE001
            self.report_errors.append(f"console {stage}: {failure!r}")
        try:
            self.receipt_path.write_text(payload, encoding="utf-8")
            self.sink_records.append("receipt:" + stage)
        except BaseException as failure:  # noqa: BLE001
            self.report_errors.append(f"receipt {stage}: {failure!r}")


class _OwnedChildTimeoutMixin:
    """Spawn one public CLI child, observe it, and clean up only that object."""

    def spawn_child(self, command: list[str], environment: dict[str, str]) -> Any:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.home),
            env=environment,
            encoding="utf-8",
        )

    def child_environment(self) -> dict[str, str]:
        """A complete environment whose every storage root is inside the fixture."""

        environment = dict(os.environ)
        environment.update({
            "WORK_STACK_HOME": str(self.home),
            "WORK_STACK_RUNTIME": str(self.runtime),
            "LOCALAPPDATA": str(self.home),
            "APPDATA": str(self.home),
            "TEMP": str(self.scratch),
            "TMP": str(self.scratch),
            "TMPDIR": str(self.scratch),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        return environment

    def child_record(
        self, process: Any, command: list[str], *, timeout_seconds: int
    ) -> dict[str, Any]:
        started = time.monotonic()
        record = {
            "command": list(command),
            "pid": process.pid,
            "fixture_root": str(self.home),
            "data_root": str(self.root),
            "runtime_root": str(self.runtime),
            "temp_root": str(self.scratch),
            "started_monotonic": started,
            "deadline_monotonic": started + timeout_seconds,
            "timeout_seconds": timeout_seconds,
        }
        return record

    def active_result(self) -> Any:
        outcome = getattr(self, "_outcome", None)
        return getattr(outcome, "result", None) if outcome is not None else None

    def preserve_holder(self) -> None:
        """Detach only THIS holder's automatic finalizer, before anything can raise.

        Detaching stops interpreter-exit removal of a fixture that is still
        unsafe. It changes no other fixture and creates no owner framework.
        """

        with contextlib.suppress(Exception):
            self.temporary._finalizer.detach()
        self.holder_preserved = True

    def stop_active_result(self, reason: str) -> None:
        result = self.active_result()
        if result is None:
            return
        try:
            result.stop()
        except BaseException as failure:  # noqa: BLE001
            self.release_failures.append(f"stop after {reason}: {failure!r}")

    def preserve_unverified_child(self, owned: _OwnedChildCleanup) -> None:
        """The child's exit could not be verified: release nothing beneath it."""

        self.preserve_fixture = True
        self.unverified_owner = owned
        self.preserve_holder()

    def add_release(self, name: str, function: Any, *arguments: Any) -> None:
        """Register one release callback whose failure stays visible and stops the run."""

        def release() -> None:
            self.release_order.append(name)
            try:
                function(*arguments)
            except BaseException as failure:  # noqa: BLE001 - recorded then re-raised
                # Preserve the now-unsafe fixture BEFORE the error propagates,
                # and stop the run: a following case must not start on top of a
                # resource this one could not release.
                self.preserve_holder()
                self.release_failures.append(f"{name}: {failure!r}")
                self.stop_active_result(name)
                raise

        self.addCleanup(release)

    def doCleanups(self) -> bool:  # noqa: N802 - unittest lifecycle name
        """Ordinary cleanup, except beneath a child whose exit is unverified.

        Preserving the directory is not enough: the servers, lease, environment
        and root that the child may still be using must not be released either.
        The actual pending callback stack is retained for root instead of run.
        """

        if getattr(self, "preserve_fixture", False):
            self.retained_cleanups = list(self._cleanups)
            self._cleanups = []
            return True
        return super().doCleanups()

    def handle_child_fault(self, owned: _OwnedChildCleanup, failure: BaseException) -> str:
        """Stop the run and clean up the exact child, whatever else fails."""

        try:
            boundary = (
                "OWNED CHILD FAULT: " + repr(failure) + NEWLINE + owned.describe("fault")
            )
        except BaseException as inner:  # noqa: BLE001
            boundary = f"OWNED CHILD FAULT: {failure!r} (description failed: {inner!r})"
        try:
            result = self.active_result()
            if result is not None:
                result.stop()
        except BaseException as inner:  # noqa: BLE001
            owned.report_errors.append(f"result.stop: {inner!r}")
        try:
            owned.report("fault")
        finally:
            # Cleanup is never gated on a report, a receipt or a result callback.
            owned.cleanup()
            if not owned.verified_exit:
                # Established immediately, before any report or callback can
                # raise: nothing is disposed of underneath an unverified child.
                self.preserve_unverified_child(owned)
        owned.report("cleanup")
        return boundary + NEWLINE + owned.describe("cleanup")

    def run(self, result: Any = None) -> Any:
        """Ordinary run, except that cleanup cannot be skipped.

        A unittest result callback such as addFailure may itself raise, which
        would otherwise return from TestCase.run before doCleanups and leave the
        owned servers, environment and fixture root untouched.
        """

        try:
            return super().run(result)
        finally:
            if self._cleanups:
                self.doCleanups()

    def run_child(self, *arguments: str) -> tuple[int, str, str]:
        """Run the public entry point once, bounded, owning exactly that child."""

        command = [
            sys.executable,
            "-B",
            str(ENTRY_POINT),
            "--data-dir",
            str(self.root),
            "backlog",
            "note",
            *arguments,
        ]
        environment = self.child_environment()
        # Everything fallible that can be checked before a process exists is
        # checked before one exists.
        self.assertIsNotNone(
            self.active_result(),
            "an owned child may only be spawned inside an active unittest result",
        )
        self.assertEqual(command[2], str(ENTRY_POINT))
        self.assertEqual(command[3], "--data-dir")
        for name in ("WORK_STACK_HOME", "WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR"):
            self.assertTrue(
                Path(environment[name]).resolve().is_relative_to(self.home.resolve()),
                f"{name} escapes the fixture root",
            )
        self.assertTrue(self.root.resolve().is_relative_to(self.home.resolve()))
        receipt_path = getattr(self, "receipt_override", None) or (
            self.home.parent / (self.home.name + "-owned-child.json")
        )

        process = self.spawn_child(command, environment)
        # Retained immediately, before any fallible post-spawn work such as
        # reading the pid or building a record. A Popen that never returned
        # grants no authority to go looking for some other process.
        owned = _OwnedChildCleanup(process, command, receipt_path, self.console)
        self.owned_child = owned
        self.owned_children.append(owned)
        try:
            owned.record = self.child_record(
                process, command, timeout_seconds=CHILD_TIMEOUT_SECONDS
            )
            out, err = process.communicate(timeout=CHILD_TIMEOUT_SECONDS)
        except BaseException as failure:  # noqa: BLE001
            self.fail(self.handle_child_fault(owned, failure))
        owned.normal_reap(out, err)
        return process.returncode, out, err


class _IsolatedRuntimeCase(unittest.TestCase):
    """Redirect runtime and temporary storage BEFORE constructing any Store."""

    def setUp(self) -> None:
        # unittest runs tearDown BEFORE doCleanups, so removing the root in
        # tearDown would delete it while an owner is still running and holding
        # the Store lease. Removal is therefore a cleanup callback registered
        # HERE, before any subclass registers its server callbacks: LIFO then
        # runs every proxy close, shutdown, server_close and join first, and
        # this one last. server_close() is the supported release path; it exits
        # the Store server lease, which closes the lock file handle.
        self.console = sys.__stderr__
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
        self.addCleanup(self._remove_fixture_root)
        self._owned_threads: list[threading.Thread] = []
        # Fixture servers whose handler failures must be empty at the end,
        # and owned children that never finished (which preserve the root).
        self._error_sinks: list[tuple[str, _RecordingHTTPServer]] = []
        self.unfinished_children: list[dict[str, Any]] = []
        self.preserve_fixture = False
        # Release callbacks that failed, and the order releases ran in.
        self.release_failures: list[str] = []
        self.release_order: list[str] = []
        self.owned_children: list[Any] = []
        self.owned_child: Any = None
        self.unverified_owner: Any = None
        self.retained_cleanups: list[Any] = []
        self.holder_preserved = False
        self.home = Path(self.temporary.name)
        self.root = self.home / "data"
        self.runtime = self.home / "runtime"
        self.scratch = self.home / "tmp"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._saved_environment = {
            name: os.environ.get(name)
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        for name in ("TEMP", "TMP", "TMPDIR"):
            os.environ[name] = str(self.scratch)
        self.addCleanup(self._restore_environment)

        from workstack.service import WorkStack
        from workstack.store import Store

        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.workspace_uid = self.store.load("workspace.json")["id"]

    def _restore_environment(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def preserve_holder(self) -> None:
        """Detach only this holder's automatic finalizer; nothing else changes."""

        with contextlib.suppress(Exception):
            self.temporary._finalizer.detach()
        self.holder_preserved = True

    def stop_active_result(self, reason: str) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome is not None else None
        if result is None:
            return
        with contextlib.suppress(Exception):
            result.stop()

    def _remove_fixture_root(self) -> None:
        """The last cleanup to run: every owner has already been closed.

        Nothing is suppressed and nothing is retried. If a lock survived its
        owner this raises, which is the point.
        """

        for thread in self._owned_threads:
            if thread.is_alive():
                # Verification failed: preserve and stop before reporting it.
                self.preserve_holder()
                self.stop_active_result("a live owned thread")
                self.fail("an owned server thread outlived its shutdown and join")
        # A fixture handler that failed unexpectedly is a defect even when every
        # assertion passed, because the traceback would land in redirected
        # stderr and disappear.
        for label, server in self._error_sinks:
            if server.handler_errors:
                self.preserve_holder()
                self.stop_active_result("an unexpected handler error")
                self.fail(f"{label} raised an unexpected handler error")
        if self.preserve_fixture or self.release_failures:
            # Either an owned child's exit could not be verified, or a release
            # failed. Nothing is disposed of underneath that.
            self.preserve_holder()
            self.stop_active_result("a preserved fixture")
            self.fail(
                "the fixture root is preserved: "
                f"owned={self.owned_children} releases={self.release_failures}"
            )
        # The final removal and its verification are held to the same rule as
        # every earlier release: if this cannot be completed, preserve what is
        # left and stop the run before the failure propagates, so no following
        # case starts on top of an unresolved fixture.
        try:
            self.temporary.cleanup()
        except BaseException:
            self.preserve_holder()
            self.stop_active_result("a failed fixture-root removal")
            raise
        if Path(self.temporary.name).exists():
            self.preserve_holder()
            self.stop_active_result("a surviving fixture root")
            self.fail(
                "the fixture root must be removed once every owner released its lease"
            )

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        from workstack import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data-dir", str(self.root), "backlog", "note", *arguments])
        return code, out.getvalue(), err.getvalue()

    def tasks_on_disk(self) -> list[dict[str, Any]]:
        return json.loads(self.store.path("backlog.json").read_text(encoding="utf-8"))[
            "tasks"
        ]

    def task_on_disk(self, identifier: str) -> dict[str, Any]:
        matches = [t for t in self.tasks_on_disk() if t["id"] == identifier]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def seed_task(self, title: str, *, notes: tuple[str, ...] = ()) -> str:
        """Seed a Task and, when asked, REAL Task notes.

        ``WorkStack.add_note(text, links)`` creates a standalone Note document,
        not a Task note: calling it with a task id seeds nothing on the Task and
        leaves every "existing baseline" assertion vacuous. The Task-note API is
        ``add_task_note(task_id, text)``.
        """

        task = self.stack.add_task(title)
        for entry in notes:
            self.stack.add_task_note(task["id"], entry)
        if notes:
            seeded = self.task_on_disk(task["id"])["notes"]
            self.assertEqual([record["text"] for record in seeded], list(notes))
        return task["id"]

    def start_idle_endpoint(self) -> "_IdleEndpoint":
        """Bind a second endpoint, registering cleanup before exposing it."""

        endpoint = _IdleEndpoint()
        self._error_sinks.append(("the idle endpoint", endpoint.server))
        self._owned_threads.append(endpoint.thread)
        self.addCleanup(endpoint.thread.join, 10)
        self.addCleanup(endpoint.close)
        endpoint.thread.start()
        return endpoint

    def write_advertisement(self, port: int, **overrides: Any) -> Path:
        document = {"version": 1, "host": "127.0.0.1", "port": port}
        document.update(overrides)
        path = self.store.server_info_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path


# ---------------------------------------------------------------------------
# The real product owner holds the writer lease.
# ---------------------------------------------------------------------------


class TaskNoteRealOwnerContract(_IsolatedRuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        from workstack.server import create_server

        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # Registered first so LIFO runs shutdown, then server_close, then this
        # join: the owner thread has fully stopped and released its handles
        # before tearDown removes the fixture root.
        self._owned_threads.append(self.thread)
        self.addCleanup(self.thread.join, 10)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.write_advertisement(self.port)

    def test_appends_one_note_and_prints_only_the_legacy_record(self) -> None:
        task_id = self.seed_task("Owner routed task")
        sibling = self.seed_task("Sibling planning task")
        before = self.task_on_disk(task_id)["revision"]

        code, out, err = self.run_cli(task_id, "  routed through the owner  ")

        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(tuple(record), NOTE_KEYS)
        self.assertEqual(record["text"], "routed through the owner")
        self.assertIsInstance(record["date"], str)
        stored = self.task_on_disk(task_id)
        self.assertEqual(stored["revision"], before + 1)
        self.assertEqual(stored["notes"], [record])
        # Sibling planning is untouched by a scoped note write.
        self.assertEqual(self.task_on_disk(sibling)["notes"], [])

    def test_preserves_an_existing_ordered_baseline_as_a_prefix(self) -> None:
        task_id = self.seed_task("Task with history", notes=("first", "second"))
        baseline = self.task_on_disk(task_id)["notes"]
        self.assertEqual([record["text"] for record in baseline], ["first", "second"])

        code, out, _err = self.run_cli(task_id.lower(), "third")

        self.assertEqual(code, 0)
        stored = self.task_on_disk(task_id)["notes"]
        self.assertEqual(stored[: len(baseline)], baseline)
        self.assertEqual(len(stored), len(baseline) + 1)
        self.assertEqual(stored[-1], json.loads(out))

    def test_duplicate_text_is_a_distinct_intent_and_appends_twice(self) -> None:
        task_id = self.seed_task("Repeatable")

        first_code, first_out, _e = self.run_cli(task_id, "same words")
        second_code, second_out, _e2 = self.run_cli(task_id, "same words")

        self.assertEqual((first_code, second_code), (0, 0))
        notes = self.task_on_disk(task_id)["notes"]
        self.assertEqual(len(notes), 2)
        self.assertEqual([n["text"] for n in notes], ["same words", "same words"])
        self.assertEqual(json.loads(first_out)["text"], "same words")
        self.assertEqual(json.loads(second_out)["text"], "same words")

    def test_internal_whitespace_and_unicode_survive_end_trimming(self) -> None:
        task_id = self.seed_task("Unicode")
        text = "  안녕  하세요\tμ — ok  "

        code, out, _err = self.run_cli(task_id, text)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["text"], "안녕  하세요\tμ — ok")

    def test_empty_text_is_rejected_before_any_write(self) -> None:
        task_id = self.seed_task("Rejects empty")

        code, out, err = self.run_cli(task_id, "   ")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("text is required", err)
        self.assertEqual(self.task_on_disk(task_id)["notes"], [])

    def test_unknown_task_refuses_without_writing(self) -> None:
        self.seed_task("Present")

        code, out, err = self.run_cli("T-4242", "no such task")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(self.tasks_on_disk()[0]["notes"], [])


class TaskNoteAbsentOwnerParity(_IsolatedRuntimeCase):
    """With no advertisement at all the exclusive-local path still applies."""

    def test_absent_owner_keeps_the_local_path_and_prints_the_same_shape(self) -> None:
        task_id = self.seed_task("Local only")
        self.assertFalse(self.store.server_info_path.exists())

        code, out, err = self.run_cli(task_id, "  local write  ")

        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(tuple(record), NOTE_KEYS)
        self.assertEqual(record["text"], "local write")
        self.assertEqual(self.task_on_disk(task_id)["notes"], [record])

    def test_unusable_advertisements_refuse_before_local_initialization(self) -> None:
        task_id = self.seed_task("Guarded")
        idle = self.start_idle_endpoint()
        path = self.store.server_info_path
        path.parent.mkdir(parents=True, exist_ok=True)

        cases = {
            "malformed": b"{not json",
            "structurally invalid": json.dumps({"version": 1, "host": []}).encode(),
            "oversized": json.dumps(
                {
                    "version": 1,
                    "host": "127.0.0.1",
                    # An endpoint this fixture owns and serves, so "never
                    # contacted" is an observation of its own log.
                    "port": idle.port,
                    "pad": "x" * 70000,
                }
            ).encode(),
        }
        for label, payload in cases.items():
            with self.subTest(advertisement=label):
                path.write_bytes(payload)
                code, out, err = self.run_cli(task_id, "must not be written")
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "")
                self.assertNotEqual(err, "")
                # No local fallback and no metadata cleanup.
                self.assertEqual(self.task_on_disk(task_id)["notes"], [])
                self.assertTrue(path.exists())
                # The parser refuses before any contact, so the owned endpoint
                # named by the oversized document logged nothing.
                self.assertEqual(idle.contacts, [], label)

    def test_a_directory_advertisement_refuses_and_is_not_removed(self) -> None:
        task_id = self.seed_task("Directory owner")
        path = self.store.server_info_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()

        code, out, err = self.run_cli(task_id, "must not be written")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(self.task_on_disk(task_id)["notes"], [])
        self.assertTrue(path.is_dir())


# ---------------------------------------------------------------------------
# A scripted owner for focused protocol cases. These payloads are synthetic.
# ---------------------------------------------------------------------------


class _RecordingHTTPServer(ThreadingHTTPServer):
    """A fixture server that retains handler exceptions instead of printing them.

    ``ThreadingHTTPServer.handle_error`` writes a traceback to stderr, which the
    CLI-output redirection in these tests swallows: a broken fixture handler can
    therefore hide inside a passing run. Every failure is kept here and asserted
    to be empty once the owned servers have stopped. Deliberately withholding a
    response is an expected transport event and never reaches this hook; an
    unexpected handler lifecycle error does.
    """

    def __init__(self, *arguments: Any, **keywords: Any) -> None:
        self.handler_errors: list[str] = []
        super().__init__(*arguments, **keywords)

    def handle_error(self, request: Any, client_address: Any) -> None:
        self.handler_errors.append(
            f"{client_address}: {traceback.format_exc()}"
        )


class _ScriptedOwner:
    def __init__(self, workspace_uid: str, task: dict[str, Any]) -> None:
        self.workspace_uid = workspace_uid
        self.task = task
        self.gets: list[str] = []
        self.posts: list[dict[str, Any]] = []
        self.post_status = 200
        self.post_payload: Any = None
        self.after_task_get = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_arguments: Any) -> None:
                return

            def _send(self, status: int, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                owner.gets.append(self.path)
                if self.path == SESSION_PATH:
                    return self._send(200, {"data": {"csrf_token": "scripted-csrf"}})
                if self.path == STORAGE_PATH:
                    return self._send(200, {"data": {"workspace_id": owner.workspace_uid}})
                if self.path == SYNC_PATH:
                    return self._send(200, {"data": {"state": "in-sync"}})
                if self.path.startswith(TASKS_PATH):
                    # The hook runs BEFORE the response is written, so the
                    # advertisement has definitely changed by the time the CLI
                    # reaches its pre-POST revalidation. Mutating afterwards
                    # would race the client and make the case flaky.
                    hook = owner.after_task_get
                    owner.after_task_get = None
                    if hook is not None:
                        hook()
                    return self._send(200, {"data": {"task": owner.task}})
                return self._send(404, {"error": {"code": "not_found"}})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                owner.posts.append({
                    "path": self.path,
                    "body": json.loads(raw) if raw else None,
                    "headers": {k: v for k, v in self.headers.items()},
                })
                payload = owner.post_payload
                if payload is None:
                    payload = {"data": dict(owner.task)}
                return self._send(owner.post_status, payload)

        self.server = _RecordingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


class _IdleEndpoint:
    """A real bound loopback endpoint that records every contact it receives.

    Used where a test needs a port genuinely owned by this fixture that must
    never be contacted, instead of an arithmetic guess such as
    ``owner.port + 1`` or a hard-coded number owned by nobody.
    """

    def __init__(self) -> None:
        self.contacts: list[str] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_arguments: Any) -> None:
                return

            def _record(self, method: str) -> None:
                endpoint.contacts.append(f"{method} {self.path}")
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                self._record("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._record("POST")

        self.server = _RecordingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


class TaskNoteScriptedOwnerContract(_IsolatedRuntimeCase):
    def start_owner(self, task: dict[str, Any]) -> _ScriptedOwner:
        owner = _ScriptedOwner(self.workspace_uid, task)
        self._error_sinks.append(("the scripted owner", owner.server))
        self._owned_threads.append(owner.thread)
        self.addCleanup(owner.thread.join, 10)
        self.addCleanup(owner.close)
        self.write_advertisement(owner.port)
        return owner

    def base_task(self, **overrides: Any) -> dict[str, Any]:
        task = {
            "id": "T-0001",
            "uid": "11111111-1111-4111-8111-111111111111",
            "revision": 3,
            "notes": [{"date": "2026-09-01", "text": "existing"}],
        }
        task.update(overrides)
        return task

    def committed(self, task: dict[str, Any], text: str) -> dict[str, Any]:
        updated = dict(task)
        updated["revision"] = task["revision"] + 1
        updated["notes"] = list(task["notes"]) + [{"date": "2026-09-02", "text": text}]
        return updated

    def test_the_wire_carries_exactly_the_frozen_path_body_and_headers(self) -> None:
        self.seed_task("Scripted")
        task = self.base_task()
        owner = self.start_owner(task)
        owner.post_payload = {"data": self.committed(task, "wire text")}

        code, out, err = self.run_cli("  t-0001  ", "  wire text  ")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"date": "2026-09-02", "text": "wire text"})
        self.assertEqual(len(owner.posts), 1)
        post = owner.posts[0]
        self.assertEqual(post["path"], "/api/v1/tasks/T-0001/notes")
        self.assertEqual(post["body"], {"text": "wire text", "revision": 3})
        self.assertEqual(post["headers"]["Origin"], f"http://127.0.0.1:{owner.port}")
        self.assertEqual(post["headers"]["X-WorkStack-CSRF"], "scripted-csrf")
        self.assertTrue(post["headers"]["Idempotency-Key"])
        # The Task was read once from the same owner before the write.
        self.assertIn("/api/v1/tasks/T-0001", owner.gets)

    def test_each_invocation_uses_a_fresh_key(self) -> None:
        self.seed_task("Scripted")
        task = self.base_task()
        owner = self.start_owner(task)
        owner.post_payload = {"data": self.committed(task, "one")}
        code, out, err = self.run_cli("T-0001", "one")
        # Assert the run succeeded and produced exactly one POST BEFORE indexing,
        # so a baseline failure is an assertion rather than an IndexError.
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["text"], "one")
        self.assertEqual(len(owner.posts), 1)
        first = owner.posts[-1]["headers"]["Idempotency-Key"]

        owner.post_payload = {"data": self.committed(task, "two")}
        code, out, err = self.run_cli("T-0001", "two")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["text"], "two")
        self.assertEqual(len(owner.posts), 2)
        second = owner.posts[-1]["headers"]["Idempotency-Key"]

        self.assertTrue(first and second)
        self.assertNotEqual(first, second)

    def test_unsupported_revisions_refuse_before_any_post(self) -> None:
        self.seed_task("Scripted")
        for label, revision in (
            ("exhausted", MAX_REVISION),
            ("out of range", MAX_REVISION + 1),
            ("negative", -1),
            ("boolean", True),
            ("string", "3"),
        ):
            with self.subTest(revision=label):
                owner = self.start_owner(self.base_task(revision=revision))
                code, out, err = self.run_cli("T-0001", "must not post")
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "")
                self.assertNotEqual(err, "")
                self.assertEqual(owner.posts, [], label)

    def test_a_wrong_type_note_baseline_refuses_before_any_post(self) -> None:
        self.seed_task("Scripted")
        owner = self.start_owner(self.base_task(notes={"not": "a list"}))

        code, out, err = self.run_cli("T-0001", "must not post")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(owner.posts, [])

    def test_an_absent_note_baseline_is_treated_as_empty(self) -> None:
        self.seed_task("Scripted")
        task = self.base_task()
        task.pop("notes")
        owner = self.start_owner(task)
        committed = dict(task)
        committed["revision"] = task["revision"] + 1
        committed["notes"] = [{"date": "2026-09-02", "text": "first ever"}]
        owner.post_payload = {"data": committed}

        code, out, err = self.run_cli("T-0001", "first ever")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"date": "2026-09-02", "text": "first ever"})

    def test_contradictory_success_responses_are_refused(self) -> None:
        self.seed_task("Scripted")
        task = self.base_task()
        good = self.committed(task, "text")

        mutated_prefix = dict(good)
        mutated_prefix["notes"] = [
            {"date": "2026-09-01", "text": "tampered"},
            good["notes"][-1],
        ]
        wrong_revision = dict(good)
        wrong_revision["revision"] = task["revision"] + 2
        wrong_uid = dict(good)
        wrong_uid["uid"] = "22222222-2222-4222-8222-222222222222"
        two_appended = dict(good)
        two_appended["notes"] = list(good["notes"]) + [
            {"date": "2026-09-02", "text": "extra"}
        ]
        wrong_text = dict(good)
        wrong_text["notes"] = list(task["notes"]) + [
            {"date": "2026-09-02", "text": "something else"}
        ]

        # POST success is {data: Task, meta: ...} - the Task is NOT nested under
        # data.task the way the GET detail envelope nests it. Each variant is
        # wrapped here so the intended identity, revision or prefix check is the
        # one that actually fires; missing data stays its own separate negative.
        for label, variant in (
            ("mutated prefix", mutated_prefix),
            ("impossible revision", wrong_revision),
            ("different task uid", wrong_uid),
            ("two appended", two_appended),
            ("wrong text", wrong_text),
        ):
            with self.subTest(response=label):
                owner = self.start_owner(self.base_task())
                owner.post_payload = {"data": variant, "meta": {}}
                code, out, err = self.run_cli("T-0001", "text")
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "")
                self.assertNotEqual(err, "")

    def test_an_explicitly_null_note_baseline_refuses_before_any_post(self) -> None:
        """Absent means empty; explicit null is a shape an owner must not send."""

        self.seed_task("Scripted")
        owner = self.start_owner(self.base_task(notes=None))

        code, out, err = self.run_cli("T-0001", "must not post")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(owner.posts, [], "a null baseline must refuse before the POST")

    def test_a_legitimate_historical_record_with_extra_fields_is_preserved(self) -> None:
        """History may carry supported additional fields; only the appended
        record is held to the created-note shape."""

        self.seed_task("Scripted")
        legacy = {"date": "2026-08-01", "text": "imported", "source": "legacy-import"}
        task = self.base_task(notes=[legacy])
        owner = self.start_owner(task)
        committed = dict(task)
        committed["revision"] = task["revision"] + 1
        committed["notes"] = [dict(legacy), {"date": "2026-09-02", "text": "appended"}]
        owner.post_payload = {"data": committed}

        code, out, err = self.run_cli("T-0001", "appended")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"date": "2026-09-02", "text": "appended"})
        self.assertEqual(len(owner.posts), 1)

    def test_a_dropped_or_rewritten_historical_field_is_refused(self) -> None:
        self.seed_task("Scripted")
        legacy = {"date": "2026-08-01", "text": "imported", "source": "legacy-import"}
        task = self.base_task(notes=[legacy])
        owner = self.start_owner(task)
        committed = dict(task)
        committed["revision"] = task["revision"] + 1
        # The extra field is silently dropped from the prefix.
        committed["notes"] = [
            {"date": "2026-08-01", "text": "imported"},
            {"date": "2026-09-02", "text": "appended"},
        ]
        owner.post_payload = {"data": committed}

        code, out, err = self.run_cli("T-0001", "appended")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")

    def test_an_invalid_appended_date_is_refused_without_clock_substitution(self) -> None:
        """A malformed created date is refused, not printed and not replaced."""

        self.seed_task("Scripted")
        task = self.base_task()
        for label, date in (
            ("empty", ""),
            ("impossible calendar day", "2026-02-30"),
            ("not a date", "yesterday"),
            ("wrong type", 20260902),
        ):
            with self.subTest(date=label):
                owner = self.start_owner(self.base_task())
                committed = dict(task)
                committed["revision"] = task["revision"] + 1
                committed["notes"] = list(task["notes"]) + [
                    {"date": date, "text": "appended"}
                ]
                owner.post_payload = {"data": committed}
                code, out, err = self.run_cli("T-0001", "appended")
                self.assertEqual(code, 2, label)
                self.assertEqual(out, "", label)
                self.assertNotEqual(err, "")
                # The owner may well have committed; the CLI neither claims it
                # did not, nor retries, nor writes locally.
                self.assertEqual(len(owner.posts), 1, label)

    def test_a_well_formed_owner_date_is_preserved_exactly(self) -> None:
        self.seed_task("Scripted")
        task = self.base_task()
        owner = self.start_owner(task)
        committed = dict(task)
        committed["revision"] = task["revision"] + 1
        committed["notes"] = list(task["notes"]) + [
            {"date": "2024-02-29", "text": "leap day"}
        ]
        owner.post_payload = {"data": committed}

        code, out, err = self.run_cli("T-0001", "leap day")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["date"], "2024-02-29")

    def test_a_success_envelope_without_a_task_is_refused(self) -> None:
        """The separate negative: no Task in the success envelope at all."""

        self.seed_task("Scripted")
        owner = self.start_owner(self.base_task())
        owner.post_payload = {"meta": {}}

        code, out, err = self.run_cli("T-0001", "text")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertEqual(len(owner.posts), 1)

    def test_a_determinate_conflict_is_not_refreshed_or_retried(self) -> None:
        self.seed_task("Scripted")
        owner = self.start_owner(self.base_task())
        owner.post_status = 409
        owner.post_payload = {"error": {"code": "revision_conflict"}}

        code, out, err = self.run_cli("T-0001", "stale")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("409", err)
        self.assertEqual(len(owner.posts), 1, "a determinate status must not be retried")
        self.assertEqual(
            len([path for path in owner.gets if path.startswith(TASKS_PATH)]),
            1,
            "a determinate status must not trigger a refetch",
        )

    def test_advertisement_changes_after_the_task_read_refuse_before_the_post(self) -> None:
        self.seed_task("Scripted")
        # A second endpoint this fixture really binds and serves. Both the
        # "replaced" and the "grown" advertisement point at it, so the advertised
        # port is owned and answering: "never contacted" is then an observation
        # of this endpoint's own log rather than an assumption about a port that
        # belongs to nobody.
        idle = self.start_idle_endpoint()
        fired: list[str] = []

        def replace(_owner: _ScriptedOwner) -> None:
            fired.append("replaced")
            self.write_advertisement(idle.port)

        def remove(_owner: _ScriptedOwner) -> None:
            fired.append("removed")
            self.store.server_info_path.unlink()

        def grow(_owner: _ScriptedOwner) -> None:
            fired.append("grown")
            self.store.server_info_path.write_text(
                json.dumps({
                    "version": 1,
                    "host": "127.0.0.1",
                    "port": idle.port,
                    "pad": "x" * 70000,
                }),
                encoding="utf-8",
            )

        for label, mutate in (("replaced", replace), ("removed", remove), ("grown", grow)):
            with self.subTest(advertisement=label):
                notes_before = list(self.tasks_on_disk()[0]["notes"])
                owner = self.start_owner(self.base_task())
                fired.clear()
                owner.after_task_get = lambda owner=owner, mutate=mutate: mutate(owner)

                code, out, err = self.run_cli("T-0001", "must not post")

                self.assertEqual(code, 2, label)
                self.assertEqual(out, "")
                self.assertNotEqual(err, "")
                # The mutation really happened, and it happened after the Task
                # read: the hook runs inside the Task GET handler before that
                # response is released.
                self.assertEqual(fired, [label], label)
                self.assertIsNone(owner.after_task_get, label)
                # The whole preflight ran against the original owner and the
                # writer stopped at the pre-POST revalidation.
                self.assertEqual(
                    owner.gets,
                    [SESSION_PATH, STORAGE_PATH, SYNC_PATH, f"{TASKS_PATH}/T-0001"],
                    label,
                )
                self.assertEqual(owner.posts, [], label)
                # The advertised replacement was never contacted at all.
                self.assertEqual(idle.contacts, [], label)
                # No local fallback write, and the metadata is left exactly as
                # the writer found it: removed stays removed, present stays.
                self.assertEqual(self.tasks_on_disk()[0]["notes"], notes_before, label)
                path = self.store.server_info_path
                if label == "removed":
                    self.assertFalse(path.exists(), label)
                else:
                    self.assertTrue(path.exists(), label)
                    document = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual(document["port"], idle.port, label)
        self.assertEqual(idle.contacts, [])

    def test_errors_never_leak_owner_text_or_internal_paths(self) -> None:
        self.seed_task("Scripted")
        owner = self.start_owner(self.base_task())
        owner.post_status = 500
        owner.post_payload = {"error": {"code": "boom", "message": LEAKY_DIAGNOSTIC_CANARY,
                                        "path": LEAKY_PATH}}

        code, out, err = self.run_cli("T-0001", "leaky")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotIn(LEAKY_DIAGNOSTIC_CANARY, err)
        self.assertNotIn(LEAKY_PATH, err)


# ---------------------------------------------------------------------------
# A real owner behind a proxy that can drop a response after the commit.
# ---------------------------------------------------------------------------


class _RealOwnerProxy:
    """Relay to a real ``create_server`` owner and drop responses after it answers."""

    def __init__(self, backend_port: int) -> None:
        self.backend_port = backend_port
        self.posts: list[dict[str, Any]] = []
        # Every POST exactly as the CLI sent it, recorded before the
        # backend Origin rewrite; every GET route in arrival order.
        self.requests: list[dict[str, Any]] = []
        self.gets: list[str] = []
        self.drop_successful_posts = 0
        # An optional single-field rewrite of a genuine POST success body. The
        # envelope, identity, revision, prefix and appended record all stay as
        # the real owner produced them; only the named field changes.
        self.mutate_post_body = None
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_arguments: Any) -> None:
                return

            def _relay(self, method: str) -> None:
                import http.client

                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else None
                original_headers = {k: v for k, v in self.headers.items()}
                if method == "POST":
                    # Recorded BEFORE the backend Origin rewrite below, so the
                    # bytes and headers are exactly what the CLI put on the wire.
                    parsed = json.loads(payload.decode("utf-8")) if payload else None
                    proxy.requests.append({
                        "route": self.path,
                        "raw": payload,
                        "headers": dict(original_headers),
                        "key": original_headers.get("Idempotency-Key"),
                        "origin": original_headers.get("Origin"),
                        "csrf": original_headers.get("X-WorkStack-CSRF"),
                        "revision": (parsed or {}).get("revision"),
                    })
                else:
                    get_index = len(proxy.gets)
                    proxy.gets.append({"route": self.path, "method": method})
                headers = dict(original_headers)
                headers["Host"] = f"127.0.0.1:{proxy.backend_port}"
                if "Origin" in headers:
                    headers["Origin"] = f"http://127.0.0.1:{proxy.backend_port}"
                connection = http.client.HTTPConnection(
                    "127.0.0.1", proxy.backend_port, timeout=15
                )
                connection.request(method, self.path, body=payload, headers=headers)
                response = connection.getresponse()
                body = response.read()
                status = response.status
                connection.close()
                if method != "POST":
                    # The complete genuine backend answer, retained before it is
                    # relayed onward unchanged.
                    decoded_get = None
                    with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                        decoded_get = json.loads(body.decode("utf-8"))
                    proxy.gets[get_index].update({
                        "status": status,
                        "raw": body,
                        "body": decoded_get,
                    })
                if method == "POST":
                    # The genuine backend answer, recorded BEFORE any drop.
                    decoded = None
                    with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                        decoded = json.loads(body.decode("utf-8"))
                    meta = decoded.get("meta") if isinstance(decoded, dict) else None
                    proxy.posts.append({
                        "path": self.path,
                        "status": status,
                        "body": decoded,
                        "meta": meta,
                        "replayed": (meta or {}).get("replayed") if isinstance(meta, dict) else None,
                    })
                    if 200 <= status < 300 and proxy.mutate_post_body is not None:
                        decoded = json.loads(body.decode("utf-8"))
                        proxy.mutate_post_body(decoded)
                        body = json.dumps(decoded).encode("utf-8")
                    if 200 <= status < 300 and proxy.drop_successful_posts > 0:
                        proxy.drop_successful_posts -= 1
                        # The owner committed; the answer never arrives. The
                        # loss is produced by closing this owned connection the
                        # normal way - no response is written and
                        # ``close_connection`` ends the handler - so the
                        # writer's ordinary lifecycle completes. Closing
                        # ``self.wfile`` here instead makes the handler flush an
                        # already closed file and raise ValueError, which a
                        # passing test would silently absorb.
                        self.close_connection = True
                        return
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self._relay("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._relay("POST")

        self.server = _RecordingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


class TaskNoteAmbiguousCommitContract(_IsolatedRuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        from workstack.server import create_server

        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.backend_port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._owned_threads.append(self.thread)
        self.addCleanup(self.thread.join, 10)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.proxy = _RealOwnerProxy(self.backend_port)
        self._error_sinks.append(("the real-owner proxy", self.proxy.server))
        self._owned_threads.append(self.proxy.thread)
        self.addCleanup(self.proxy.thread.join, 10)
        self.addCleanup(self.proxy.close)
        self.write_advertisement(self.proxy.port)

    def seeded_task(self, title: str) -> tuple[str, list[dict[str, Any]]]:
        """Seed a real, non-empty, ordered Task-note prefix through the service."""

        task_id = self.seed_task(title, notes=("first seeded", "second seeded"))
        prefix = self.task_on_disk(task_id)["notes"]
        self.assertEqual([note["text"] for note in prefix], ["first seeded", "second seeded"])
        return task_id, [dict(note) for note in prefix]

    def assert_genuine_get_capture(self, task_id: str) -> None:
        """Assert the preflight against the complete responses the owner sent.

        Every body here is the real ``create_server`` answer retained before it
        was relayed onward unchanged; nothing is hand-authored.
        """

        self.assertEqual(
            [record["route"] for record in self.proxy.gets],
            [SESSION_PATH, STORAGE_PATH, SYNC_PATH, f"{TASKS_PATH}/{task_id}"],
        )
        self.assertEqual([record["status"] for record in self.proxy.gets], [200] * 4)
        session, storage, sync, detail = self.proxy.gets

        # The CSRF token the writer sent came from the owner's own session
        # response, not from a fixture constant.
        self.assertEqual(
            self.proxy.requests[0]["csrf"], session["body"]["data"]["csrf_token"]
        )
        self.assertEqual(storage["body"]["data"]["workspace_id"], self.workspace_uid)
        self.assertIn("state", sync["body"]["data"])

        # A Task GET envelope carries data.task; a POST answer is the Task
        # itself. Both shapes come from the same real owner.
        task = detail["body"]["data"]["task"]
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["revision"], self.proxy.requests[0]["revision"])
        self.assertEqual(self.proxy.posts[0]["body"]["data"]["id"], task_id)
        self.assertEqual(self.proxy.posts[0]["body"]["data"]["uid"], task["uid"])
        # The raw bytes were retained too, and decode to the same document.
        self.assertEqual(json.loads(detail["raw"].decode("utf-8")), detail["body"])

    def assert_replay_was_identical(self) -> None:
        """The replay repeated the first attempt byte for byte."""

        self.assertEqual(len(self.proxy.requests), 2, "exactly one replay")
        first, second = self.proxy.requests
        self.assertEqual(second["route"], first["route"])
        self.assertEqual(second["raw"], first["raw"], "the replay must resend the same bytes")
        self.assertEqual(second["key"], first["key"], "the replay must reuse the same key")
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(second["csrf"], first["csrf"])
        self.assertEqual(second["origin"], first["origin"])
        # A genuine per-invocation random key, not a constant: the product
        # emits "cli-note-" followed by a uuid4 hex, so parse it back.
        self.assertTrue(first["key"].startswith(KEY_PREFIX), first["key"])
        parsed = uuid.UUID(hex=first["key"][len(KEY_PREFIX):])
        self.assertEqual(parsed.version, 4)
        self.assertEqual(parsed.hex, first["key"][len(KEY_PREFIX):])
        # The Task was read once; the ambiguous replay must not refresh it.
        task_reads = [
            record for record in self.proxy.gets if record["route"].startswith(TASKS_PATH)
        ]
        self.assertEqual(len(task_reads), 1, "the replay must not refetch the Task")
        self.assertEqual(first["revision"], self.base_revision)

    def test_one_lost_response_replays_once_and_commits_exactly_one_note(self) -> None:
        task_id, prefix = self.seeded_task("Ambiguous")
        self.base_revision = self.task_on_disk(task_id)["revision"]
        self.proxy.drop_successful_posts = 1

        code, out, err = self.run_cli(task_id, "committed once")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["text"], "committed once")
        notes = self.task_on_disk(task_id)["notes"]
        self.assertEqual(len(notes), len(prefix) + 1, "the replay must not duplicate the note")
        self.assertEqual(notes[: len(prefix)], prefix, "the seeded prefix is preserved whole")
        self.assertEqual(notes[-1]["text"], "committed once")

        # The genuine owner answers recorded before the proxy dropped them.
        self.assertEqual(len(self.proxy.posts), 2, "exactly one replay")
        first, second = self.proxy.posts
        self.assertEqual(first["status"], 200)
        self.assertIs(first["replayed"], False)
        self.assertEqual(second["status"], 200)
        self.assertIs(second["replayed"], True)
        self.assert_replay_was_identical()
        self.assert_genuine_get_capture(task_id)

    def test_a_second_loss_reports_commit_unknown_without_a_third_attempt(self) -> None:
        task_id, prefix = self.seeded_task("Twice lost")
        self.base_revision = self.task_on_disk(task_id)["revision"]
        self.proxy.drop_successful_posts = 2

        code, out, err = self.run_cli(task_id, "unknown outcome")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("task note commit is unknown", err)
        self.assertEqual(len(self.proxy.posts), 2, "no third attempt")
        first, second = self.proxy.posts
        self.assertEqual((first["status"], first["replayed"]), (200, False))
        self.assertEqual((second["status"], second["replayed"]), (200, True))
        self.assert_replay_was_identical()
        self.assert_genuine_get_capture(task_id)
        # The owner did commit; the CLI must not claim otherwise, must not
        # append again locally, and must not disturb the seeded prefix.
        notes = self.task_on_disk(task_id)["notes"]
        self.assertEqual(len(notes), len(prefix) + 1)
        self.assertEqual(notes[: len(prefix)], prefix)
        self.assertEqual(notes[-1]["text"], "unknown outcome")



# ---------------------------------------------------------------------------
# The committed public entry point, in a real child interpreter.
# ---------------------------------------------------------------------------


class TaskNoteEntryPointSubprocessContract(_OwnedChildTimeoutMixin, _IsolatedRuntimeCase):
    """Drive ``run_work_stack.py`` as a separate process against a real owner.

    Everything above imports ``workstack.cli`` in-process. This suite proves the
    committed public entry point behaves the same way when a genuine child
    interpreter runs it with an explicit ``--data-dir`` and a fully contained
    environment. ``python -m workstack.cli`` is deliberately NOT used: that
    module has no entry guard, so it would not exercise the shipped script.
    """

    def setUp(self) -> None:
        super().setUp()
        from workstack.server import create_server

        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._owned_threads.append(self.thread)
        self.add_release("join", self.thread.join, 10)
        self.add_release("server_close", self.server.server_close)
        self.add_release("shutdown", self.server.shutdown)
        self.write_advertisement(self.port)

    def test_the_entry_point_commits_through_the_owner(self) -> None:
        task_id = self.seed_task("Child routed", notes=("seeded first", "seeded second"))
        sibling = self.seed_task("Child sibling")
        prefix = self.task_on_disk(task_id)["notes"]
        self.assertEqual(len(prefix), 2)
        before = self.task_on_disk(task_id)["revision"]

        code, out, err = self.run_child(task_id, "  주간 회고 — café  ")

        self.assertEqual(code, 0, err)
        # The raw record, in the frozen legacy key order.
        self.assertEqual(list(json.loads(out)), list(NOTE_KEYS))
        record = json.loads(out)
        self.assertEqual(record["text"], "주간 회고 — café")
        self.assertIsInstance(record["date"], str)

        stored = self.task_on_disk(task_id)
        self.assertEqual(stored["notes"][: len(prefix)], prefix)
        self.assertEqual(len(stored["notes"]), len(prefix) + 1, "exactly one append")
        self.assertEqual(stored["notes"][-1], record)
        self.assertEqual(stored["revision"], before + 1)
        # The write reached exactly one Task: the sibling is untouched.
        self.assertEqual(self.task_on_disk(sibling)["notes"], [])
        # The child used the fixture's own data directory, not a default one.
        self.assertTrue(self.store.path("backlog.json").is_file())

    def test_the_entry_point_refuses_an_unusable_advertisement_without_writing(self) -> None:
        task_id = self.seed_task("Child guarded", notes=("seeded first",))
        prefix = self.task_on_disk(task_id)["notes"]
        before = self.task_on_disk(task_id)["revision"]
        path = self.store.server_info_path
        path.write_bytes(b"{not json")

        code, out, err = self.run_child(task_id, "must not be written")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        # No local fallback, no metadata cleanup.
        self.assertEqual(self.task_on_disk(task_id)["notes"], prefix)
        self.assertEqual(self.task_on_disk(task_id)["revision"], before)
        self.assertTrue(path.exists())

        # Positive control: the same child, the same environment and the same
        # arguments succeed once the advertisement is readable again, so the
        # refusal above is the writer's decision and not broken plumbing.
        self.write_advertisement(self.port)
        code, out, err = self.run_child(task_id, "must not be written")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["text"], "must not be written")
        notes = self.task_on_disk(task_id)["notes"]
        self.assertEqual(notes[: len(prefix)], prefix)
        self.assertEqual(len(notes), len(prefix) + 1)


    def test_every_owned_child_of_this_case_was_reaped(self) -> None:
        self.run_child(self.seed_task("Reaped"), "reaped")
        self.assertEqual(len(self.owned_children), 1)
        owned = self.owned_children[0]
        self.assertTrue(owned.reaped)
        self.assertTrue(owned.verified_exit)
        # A child that finished on its own is never terminated.
        self.assertEqual(owned.terminate_calls, 0)
        self.assertFalse(self.preserve_fixture)
        self.assertEqual(self.release_failures, [])


# ---------------------------------------------------------------------------
# Historical records must survive as the JSON the owner actually holds.
# ---------------------------------------------------------------------------


HISTORICAL_NOTE = {
    "date": "2026-08-01",
    "text": "imported",
    "source": "legacy-import",
    "legacy": {"verified": True, "source": "imported", "attempts": 1},
    "tags": ["import", "legacy"],
}


class TaskNoteHistoricalPrefixContract(_IsolatedRuntimeCase):
    """A legitimate historical record with nested extras is preserved exactly.

    The record below is accepted product data: a real local append keeps it, and
    a healthy owner returns it unchanged. The adverse case changes exactly one
    nested JSON boolean into the number 1 inside an otherwise genuine owner
    response; identity, UID, revision, prefix length and the appended record all
    stay as the real owner produced them.
    """

    def seed_history(self, title: str) -> str:
        from workstack.storage.document_repository import WorkspaceDocument

        task_id = self.seed_task(title)
        # Written through the store's own document repository, the way an
        # import would leave it: a raw file write would look like an external
        # change and the owner would refuse before any of this is exercised.
        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        for entry in document["tasks"]:
            if entry["id"] == task_id:
                entry["notes"] = [json.loads(json.dumps(HISTORICAL_NOTE))]
        self.stack.documents.save(WorkspaceDocument.TASKS, document)
        stored = self.task_on_disk(task_id)["notes"]
        self.assertEqual(stored, [HISTORICAL_NOTE])
        self.assertIs(stored[0]["legacy"]["verified"], True)
        return task_id

    def start_owner(self) -> "_RealOwnerProxy":
        from workstack.server import create_server

        server = create_server(self.stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._owned_threads.append(thread)
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        proxy = _RealOwnerProxy(server.server_address[1])
        self._error_sinks.append(("the real-owner proxy", proxy.server))
        self._owned_threads.append(proxy.thread)
        self.addCleanup(proxy.thread.join, 10)
        self.addCleanup(proxy.close)
        self.write_advertisement(proxy.port)
        return proxy

    def assert_history_intact(self, task_id: str) -> list[dict[str, Any]]:
        notes = self.task_on_disk(task_id)["notes"]
        self.assertEqual(notes[0], HISTORICAL_NOTE)
        self.assertIs(notes[0]["legacy"]["verified"], True)
        return notes

    def test_a_local_append_accepts_and_preserves_a_nested_historical_record(self) -> None:
        task_id = self.seed_history("Local history")

        code, out, err = self.run_cli(task_id, "local append")

        self.assertEqual(code, 0, err)
        notes = self.assert_history_intact(task_id)
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[-1], json.loads(out))

    def test_a_healthy_owner_preserves_a_nested_historical_record(self) -> None:
        task_id = self.seed_history("Owner history")
        proxy = self.start_owner()

        code, out, err = self.run_cli(task_id, "owner append")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["text"], "owner append")
        notes = self.assert_history_intact(task_id)
        self.assertEqual(len(notes), 2)
        self.assertEqual(len(proxy.posts), 1)
        # The owner really returned the nested boolean as a boolean.
        self.assertIs(
            proxy.posts[0]["body"]["data"]["notes"][0]["legacy"]["verified"], True
        )

    def test_a_historical_boolean_returned_as_a_number_is_refused(self) -> None:
        task_id = self.seed_history("Type sensitive")
        proxy = self.start_owner()
        before = self.task_on_disk(task_id)["revision"]

        def to_number(document: dict[str, Any]) -> None:
            # One field of an otherwise genuine success body: JSON true -> 1.
            document["data"]["notes"][0]["legacy"]["verified"] = 1

        proxy.mutate_post_body = to_number

        code, out, err = self.run_cli(task_id, "must be refused")

        self.assertEqual(code, 2, out)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.assertNotIn(str(self.root), err, "the refusal must stay sanitized")
        # Exactly one POST: no retry, no rollback, no local fallback.
        self.assertEqual(len(proxy.posts), 1)
        self.assertEqual(
            len([record for record in proxy.gets if record["route"].startswith(TASKS_PATH)]),
            1,
            "a malformed acknowledgement must not trigger a refetch",
        )
        # The owner did commit; the CLI must not claim otherwise and must not
        # append a second copy locally. The durable history is untouched.
        notes = self.assert_history_intact(task_id)
        self.assertEqual(len(notes), 2, "the owner appended exactly once")
        self.assertEqual(notes[-1]["text"], "must be refused")
        self.assertEqual(self.task_on_disk(task_id)["revision"], before + 1)

        # Healthy control after the adverse case, on the same fixture.
        proxy.mutate_post_body = None
        code, out, err = self.run_cli(task_id, "healthy after")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["text"], "healthy after")
        notes = self.assert_history_intact(task_id)
        self.assertEqual(len(notes), 3)
        self.assertEqual(len(proxy.posts), 2)

    def test_a_legitimate_number_in_history_still_compares_equal(self) -> None:
        """The correction must not become a numeric canonicalization policy."""

        task_id = self.seed_history("Numeric history")
        proxy = self.start_owner()

        def to_float(document: dict[str, Any]) -> None:
            # JSON has one number type: 1 and 1.0 are the same value and must
            # keep comparing equal, unlike true and 1.
            document["data"]["notes"][0]["legacy"]["attempts"] = 1.0

        proxy.mutate_post_body = to_float

        code, out, err = self.run_cli(task_id, "numeric control")

        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["text"], "numeric control")
        self.assertEqual(len(proxy.posts), 1)
        self.assert_history_intact(task_id)


# ---------------------------------------------------------------------------
# Fixture-policy acceptance for the observation boundary.
#
# Everything below drives a clearly labelled SYNTHETIC fake process through the
# real run_child and an ordinary inner unittest suite. No real child is made to
# run forever and no production behaviour is exercised here; the genuine
# run_work_stack.py success, refusal, recovery and reap cases stay above.
# ---------------------------------------------------------------------------


NEWLINE = chr(10)
CHECKOUT_ROOT = Path(__file__).resolve(strict=True).parents[1]
ENTRY_POINT = CHECKOUT_ROOT / "run_work_stack.py"
CHILD_TIMEOUT_SECONDS = 120
# ONE additional total cleanup budget for a faulted owned child, measured
# once and never reset. Not a per-operation allowance.
CLEANUP_DEADLINE_SECONDS = 5
LEAKY_DIAGNOSTIC_CANARY = "scripted-task-note-secret-must-not-be-printed"
LEAKY_PATH = r"C:\scripted-owner\internal\path\must-not-be-printed"


class _UnfinishedChild(Exception):
    """Bounded cleanup could not verify the owned child's exit.

    The fixture is preserved and the run stops; this is a failed/unfinished
    result for root, never authority to widen cleanup to another process.
    """


# ---------------------------------------------------------------------------
# Owned-child policy, over clearly labelled SYNTHETIC fakes only.
#
# Every process here is fake. No real Popen, Store or server is created by these
# cases, and no real child is ever made to hang. The genuine public
# run_work_stack.py success, refusal, recovery and reap cases live above.
# ---------------------------------------------------------------------------


class _BrokenStream:
    """A sink whose write and flush raise, proving a sink cannot gate cleanup."""

    def __init__(self) -> None:
        self.attempts = 0

    def write(self, _text: str) -> int:
        self.attempts += 1
        raise OSError("synthetic console sink failure")

    def flush(self) -> None:
        raise OSError("synthetic console flush failure")


class _FakeOwnedChild:
    """SYNTHETIC labelled fake Popen - NOT a real process, NOT product evidence.

    It records every terminate call and every timeout it was given, so the
    bounded policy can be asserted without spawning something that hangs.
    """

    def __init__(
        self,
        *,
        times_out: bool = False,
        exits_during_timeout: bool = False,
        pid_fails: bool = False,
        terminate_fails: bool = False,
        reap_times_out: bool = False,
        returncode: int = 0,
    ) -> None:
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._times_out = times_out
        self._exits_during_timeout = exits_during_timeout
        self._pid_fails = pid_fails
        self._terminate_fails = terminate_fails
        self._reap_times_out = reap_times_out
        self.terminate_calls = 0
        self.kill_calls = 0
        self.signal_calls = 0
        self.communicate_timeouts: list[Any] = []

    @property
    def pid(self) -> int:
        if self._pid_fails:
            raise OSError("synthetic pid failure")
        return -4321

    def poll(self) -> int | None:
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_timeouts.append(timeout)
        if self._times_out and len(self.communicate_timeouts) == 1:
            if self._exits_during_timeout:
                # The child exited in the race just before cleanup looked.
                self.returncode = self._final_returncode
            raise subprocess.TimeoutExpired(
                cmd=["<fake child>"],
                timeout=timeout or 0,
                output="partial child stdout",
                stderr="partial child stderr",
            )
        if self._reap_times_out:
            raise subprocess.TimeoutExpired(
                cmd=["<fake child>"], timeout=timeout or 0
            )
        self.returncode = self._final_returncode
        return "fake child stdout", "fake child stderr"

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self._terminate_fails:
            raise OSError("synthetic terminate failure")

    def kill(self) -> None:
        self.kill_calls += 1
        raise AssertionError("kill is outside the owned-object authority")

    def send_signal(self, _signal: int) -> None:
        self.signal_calls += 1
        raise AssertionError("send_signal is outside the owned-object authority")


def _owned_child_policy_classes() -> tuple[type, type]:
    """Build the synthetic cases on demand.

    They are deliberately NOT module-level TestCase subclasses: unittest
    discovery collects every TestCase in a module regardless of name, so a
    module-level synthetic case would run as a real gate case.
    """

    class _SyntheticChildCase(_OwnedChildTimeoutMixin, unittest.TestCase):
        """SYNTHETIC policy case: fake child, own temporary data, no Store or server."""

        fake: Any = None
        faults: frozenset[str] = frozenset()
        result_callback_fails = False

        def setUp(self) -> None:
            self.console = _BrokenStream() if "console" in self.faults else sys.__stderr__
            self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
            self.holder_preserved = False
            self.preserve_fixture = False
            self.release_failures: list[str] = []
            self.release_order: list[str] = []
            self.add_release("_remove_fixture_root", self._remove_fixture_root)
            self.home = Path(self.temporary.name)
            self.root = self.home / "data"
            self.runtime = self.home / "runtime"
            self.scratch = self.home / "tmp"
            for directory in (self.root, self.runtime, self.scratch):
                directory.mkdir(parents=True, exist_ok=True)
            self.owned_children: list[Any] = []
            self.owned_child: Any = None
            self.removed_root = False
            self.add_release("join", self._release, "join")
            self.add_release("server_close", self._release, "server_close")
            self.add_release("shutdown", self._release, "shutdown")
            if "receipt" in self.faults:
                # Under a directory that does not exist, so this sink fails now
                # and cannot leave a stray file later either.
                self.receipt_override = self.home / "absent" / "receipt.json"

        def _release(self, name: str) -> None:
            if "release" in self.faults and name == "server_close":
                raise RuntimeError("synthetic release failure")

        def _remove_fixture_root(self) -> None:
            if "root" in self.faults:
                raise RuntimeError("synthetic final root verification failure")
            if self.preserve_fixture or self.release_failures:
                self.preserve_holder()
                self.fail(
                    "the fixture root is preserved: "
                    f"owned={self.owned_children} releases={self.release_failures}"
                )
            self.temporary.cleanup()
            self.removed_root = True

        def spawn_child(self, command: list[str], environment: dict[str, str]) -> Any:
            return self.fake

        def child_environment(self) -> dict[str, str]:
            environment = dict(os.environ)
            environment.update({
                "WORK_STACK_HOME": str(self.home),
                "WORK_STACK_RUNTIME": str(self.runtime),
                "TEMP": str(self.scratch),
                "TMP": str(self.scratch),
                "TMPDIR": str(self.scratch),
            })
            return environment

        def run_child(self, *arguments: str) -> tuple[int, str, str]:
            outcome = super().run_child(*arguments)
            return outcome

        def test_owned_child(self) -> None:
            code, out, _err = self.run_child("T-0001", "synthetic")
            self.assertEqual(code, 0)
            self.assertEqual(out, "fake child stdout")

    class _SentinelCase(unittest.TestCase):
        ran: list[str] = []

        def test_sentinel(self) -> None:
            self.ran.append("sentinel")

    return _SyntheticChildCase, _SentinelCase


class _FaultyHolder:
    """SYNTHETIC holder wrapper - only the final cleanup is faulted.

    It stands in for the TemporaryDirectory the fixture holds. The cleanup
    boundary under review is NOT replaced: the real base
    ``_IsolatedRuntimeCase._remove_fixture_root`` still calls ``cleanup()`` here
    and still verifies the path afterwards.
    """

    def __init__(self, holder: tempfile.TemporaryDirectory, mode: str) -> None:
        self._holder = holder
        self.mode = mode
        self.name = holder.name
        self._finalizer = holder._finalizer
        self.cleanup_calls = 0

    def cleanup(self) -> None:
        self.cleanup_calls += 1
        if self.mode == "raise":
            raise RuntimeError("synthetic holder cleanup failure")
        if self.mode == "residual":
            # Returns without removing, so the path survives the call.
            return
        self._holder.cleanup()

    def dispose(self) -> None:
        """Test-driver disposal of this driver's own synthetic leftovers."""

        self._holder.cleanup()


def _real_cleanup_case_class() -> type:
    """A case that uses the ACTUAL base cleanup method and its registration.

    Only the Store and server construction is skipped and only the holder is
    faulted; ``_remove_fixture_root`` and its ``addCleanup`` registration are the
    real ones inherited from :class:`_IsolatedRuntimeCase`.
    """

    class _RealCleanupCase(_IsolatedRuntimeCase):
        holder_mode = "healthy"

        def setUp(self) -> None:
            self.console = sys.__stderr__
            self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
            # The real registration: the inherited method, registered directly
            # with addCleanup exactly as the base fixture does.
            self.addCleanup(self._remove_fixture_root)
            self._owned_threads: list[threading.Thread] = []
            self._error_sinks: list[tuple[str, Any]] = []
            self.unfinished_children: list[dict[str, Any]] = []
            self.preserve_fixture = False
            self.release_failures: list[str] = []
            self.release_order: list[str] = []
            self.owned_children: list[Any] = []
            self.owned_child: Any = None
            self.unverified_owner: Any = None
            self.retained_cleanups: list[Any] = []
            self.holder_preserved = False
            self.home = Path(self.temporary.name)
            self.root = self.home / "data"
            self.runtime = self.home / "runtime"
            self.scratch = self.home / "tmp"
            for directory in (self.root, self.runtime, self.scratch):
                directory.mkdir(parents=True, exist_ok=True)
            self.temporary = _FaultyHolder(self.temporary, self.holder_mode)

        def test_child_then_cleanup(self) -> None:
            # A normally completed fake child, so the cleanup boundary is the
            # only thing under test.
            self.owned_child = _FakeOwnedChild()
            self.owned_children.append(self.owned_child)

    return _RealCleanupCase


class _StoppingResult(unittest.TestResult):
    """A result whose addFailure raises once, to prove cleanup is not gated on it."""

    def addFailure(self, test: Any, err: Any) -> None:  # noqa: N802
        super().addFailure(test, err)
        raise RuntimeError("synthetic result callback failure")


class TaskNoteOwnedChildPolicyContract(unittest.TestCase):
    """The bounded owned-child policy, through an actual inner suite."""

    def drive(
        self,
        fake: _FakeOwnedChild,
        *,
        faults: frozenset[str] = frozenset(),
        result_class: type = unittest.TestResult,
    ) -> dict[str, Any]:
        synthetic, sentinel = _owned_child_policy_classes()
        synthetic.fake = fake
        synthetic.faults = faults
        sentinel.ran = []
        case = synthetic("test_owned_child")
        suite = unittest.TestSuite([case, sentinel("test_sentinel")])
        result = result_class()
        run_error = None
        try:
            suite.run(result)
        except BaseException as failure:
            # A deliberately raising result callback escapes the suite; the
            # driver records it instead of losing the case under test.
            run_error = failure
        return {
            "run_error": run_error,
            "case": case,
            "fake": fake,
            "result": result,
            "sentinel": sentinel,
            "owned": case.owned_child,
        }

    def test_ordinary_completion_never_terminates_and_allows_the_sentinel(self) -> None:
        driven = self.drive(_FakeOwnedChild())
        owned, result = driven["owned"], driven["result"]

        self.assertEqual(result.failures, [])
        self.assertEqual(result.errors, [])
        self.assertFalse(result.shouldStop)
        self.assertEqual(driven["sentinel"].ran, ["sentinel"])
        self.assertEqual(driven["fake"].terminate_calls, 0)
        self.assertEqual(driven["fake"].communicate_timeouts, [CHILD_TIMEOUT_SECONDS])
        self.assertTrue(owned.reaped)
        self.assertTrue(owned.verified_exit)
        self.assertEqual(owned.stdout, "fake child stdout")
        self.assertEqual(owned.returncode, 0)
        # Ordinary cleanup ran in the ordinary LIFO order and removed the root.
        self.assertEqual(
            driven["case"].release_order,
            ["shutdown", "server_close", "join", "_remove_fixture_root"],
        )
        self.assertTrue(driven["case"].removed_root)

    def test_a_timeout_stops_the_suite_and_terminates_exactly_once(self) -> None:
        fake = _FakeOwnedChild(times_out=True)
        driven = self.drive(fake)
        owned, result = driven["owned"], driven["result"]

        self.assertTrue(result.shouldStop)
        self.assertEqual(driven["sentinel"].ran, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("OWNED CHILD FAULT", result.failures[0][1])
        # Exactly one terminate on the exact object, then one bounded reap whose
        # timeout is a positive remainder of the single cleanup deadline.
        self.assertEqual(fake.terminate_calls, 1)
        self.assertEqual(fake.kill_calls, 0)
        self.assertEqual(fake.signal_calls, 0)
        self.assertEqual(len(fake.communicate_timeouts), 2)
        self.assertEqual(fake.communicate_timeouts[0], CHILD_TIMEOUT_SECONDS)
        reap_budget = fake.communicate_timeouts[1]
        self.assertGreater(reap_budget, 0)
        self.assertLessEqual(reap_budget, CLEANUP_DEADLINE_SECONDS)
        self.assertTrue(owned.verified_exit)
        self.assertTrue(owned.reaped)
        self.assertEqual(owned.cleanup_errors, [])
        # The original failure stands, the receipt was written, and cleanup ran.
        self.assertIn("receipt:fault", owned.sink_records)
        self.assertTrue(owned.receipt_path.exists())
        self.assertEqual(
            driven["case"].release_order,
            ["shutdown", "server_close", "join", "_remove_fixture_root"],
        )
        self.assertTrue(driven["case"].removed_root)

    def test_early_failures_and_broken_sinks_cannot_bypass_cleanup(self) -> None:
        fake = _FakeOwnedChild(pid_fails=True)
        driven = self.drive(
            fake,
            faults=frozenset({"console", "receipt"}),
            result_class=_StoppingResult,
        )
        owned, result = driven["owned"], driven["result"]

        # The post-spawn pid access failed, both sinks failed and addFailure
        # raised - and the exact child was still cleaned up.
        self.assertEqual(fake.terminate_calls, 1)
        self.assertTrue(owned.verified_exit)
        self.assertTrue(owned.reaped)
        self.assertNotEqual(owned.report_errors, [])
        self.assertEqual(owned.sink_records, [])
        self.assertTrue(result.shouldStop)
        self.assertEqual(driven["sentinel"].ran, [])
        self.assertNotEqual(len(result.failures) + len(result.errors), 0)
        # The result callback really did raise, and it still did not skip
        # cleanup: the release order completed and the root was removed.
        self.assertIsInstance(driven["run_error"], RuntimeError)
        self.assertEqual(
            driven["case"].release_order,
            ["shutdown", "server_close", "join", "_remove_fixture_root"],
        )
        self.assertTrue(driven["case"].removed_root)

    def test_an_exit_race_is_verified_through_the_same_object(self) -> None:
        fake = _FakeOwnedChild(times_out=True, exits_during_timeout=True)
        driven = self.drive(fake)
        owned = driven["owned"]

        # The same object already reported its exit, so nothing was terminated.
        self.assertEqual(fake.terminate_calls, 0)
        self.assertTrue(owned.already_exited)
        self.assertTrue(owned.verified_exit)
        self.assertTrue(owned.reaped)
        self.assertEqual(owned.returncode, 0)
        self.assertEqual(owned.cleanup_errors, [])
        self.assertTrue(driven["result"].shouldStop)
        self.assertTrue(driven["case"].removed_root)

    def test_unverifiable_cleanup_preserves_the_fixture_and_stops(self) -> None:
        fake = _FakeOwnedChild(times_out=True, terminate_fails=True, reap_times_out=True)
        driven = self.drive(fake)
        owned, case = driven["owned"], driven["case"]

        self.assertEqual(fake.terminate_calls, 1, "no retry after a failed terminate")
        self.assertEqual(len(fake.communicate_timeouts), 2, "one bounded reap only")
        self.assertFalse(owned.verified_exit)
        self.assertFalse(owned.reaped)
        self.assertEqual(len(owned.cleanup_errors), 2)
        self.assertIn("terminate", owned.cleanup_errors[0])
        self.assertIn("bounded reap timed out", owned.cleanup_errors[1])
        self.assertTrue(case.preserve_fixture)
        self.assertTrue(driven["result"].shouldStop)
        self.assertEqual(driven["sentinel"].ran, [])
        self.assertTrue(Path(case.temporary.name).exists(), "the unsafe root survives")
        self.assertFalse(case.removed_root)
        # OC-A1: keeping the directory is not enough. NOTHING was released
        # beneath a child whose exit could not be verified - no shutdown, no
        # server_close, no join, no environment restoration, no root removal.
        self.assertEqual(case.release_order, [])
        self.assertEqual(case.release_failures, [])
        # The actual pending callback stack and the exact owner are retained
        # for root instead of being run.
        self.assertEqual(len(case.retained_cleanups), 4)
        self.assertEqual(case._cleanups, [])
        self.assertIs(case.unverified_owner, owned)
        self.assertIs(owned.process, fake)
        self.assertTrue(case.holder_preserved, "only this holder's finalizer detached")
        # Test-driver disposal of this driver's own synthetic leftovers, only
        # after the preservation assertions above. Not a production retry path.
        case.temporary.cleanup()

    def test_a_failing_release_stops_the_run_and_prevents_root_removal(self) -> None:
        driven = self.drive(_FakeOwnedChild(), faults=frozenset({"release"}))
        case = driven["case"]

        self.assertTrue(driven["owned"].reaped)
        self.assertEqual(driven["fake"].terminate_calls, 0)
        # OC-A2: a resource this case could not release must stop the run, so
        # no following case starts on top of it.
        self.assertTrue(driven["result"].shouldStop)
        self.assertEqual(driven["sentinel"].ran, [])
        self.assertTrue(case.holder_preserved, "preserved before the error propagated")
        # Independent releases were still attempted, in order, and the failure
        # is visible rather than swallowed.
        self.assertEqual(
            case.release_order,
            ["shutdown", "server_close", "join", "_remove_fixture_root"],
        )
        # Two recorded failures: the release itself, and the refused final root
        # removal that follows it. Independent releases in between still ran.
        self.assertEqual(len(case.release_failures), 2)
        self.assertIn("server_close", case.release_failures[0])
        self.assertIn("_remove_fixture_root", case.release_failures[1])
        self.assertFalse(case.removed_root)
        self.assertTrue(Path(case.temporary.name).exists())
        case.temporary.cleanup()  # test-driver disposal of its own leftovers

    def test_a_final_root_verification_failure_stops_the_run(self) -> None:
        """The last verification step is held to the same rule as a release."""

        driven = self.drive(_FakeOwnedChild(), faults=frozenset({"root"}))
        case = driven["case"]

        self.assertTrue(driven["owned"].reaped)
        self.assertTrue(driven["result"].shouldStop)
        self.assertEqual(driven["sentinel"].ran, [])
        self.assertTrue(case.holder_preserved)
        self.assertFalse(case.removed_root)
        self.assertTrue(Path(case.temporary.name).exists())
        self.assertEqual(len(case.release_failures), 1)
        self.assertIn("_remove_fixture_root", case.release_failures[0])
        case.temporary.cleanup()  # test-driver disposal of its own leftovers

    def drive_real_cleanup(self, mode: str) -> dict[str, Any]:
        case_class = _real_cleanup_case_class()
        case_class.holder_mode = mode
        _, sentinel = _owned_child_policy_classes()
        sentinel.ran = []
        case = case_class("test_child_then_cleanup")
        suite = unittest.TestSuite([case, sentinel("test_sentinel")])
        result = unittest.TestResult()
        suite.run(result)
        return {"case": case, "result": result, "sentinel": sentinel}

    def test_the_actual_root_cleanup_boundary_stops_the_run_on_failure(self) -> None:
        """OC-A3, through the real base method and its real registration."""

        for mode, expected in (("raise", "errors"), ("residual", "failures")):
            with self.subTest(holder=mode):
                driven = self.drive_real_cleanup(mode)
                case, result = driven["case"], driven["result"]

                self.assertEqual(len(getattr(result, expected)), 1)
                self.assertTrue(result.shouldStop, "the run must stop")
                self.assertEqual(driven["sentinel"].ran, [], "no following case")
                self.assertTrue(case.holder_preserved)
                self.assertEqual(case.temporary.cleanup_calls, 1, "no retry")
                self.assertTrue(Path(case.temporary.name).exists())
                # Test-driver disposal of this driver's own synthetic leftover,
                # after the preservation assertions.
                case.temporary.dispose()

    def test_the_actual_root_cleanup_boundary_stays_strict_when_healthy(self) -> None:
        driven = self.drive_real_cleanup("healthy")
        case, result = driven["case"], driven["result"]

        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertFalse(result.shouldStop)
        self.assertEqual(driven["sentinel"].ran, ["sentinel"])
        self.assertFalse(case.holder_preserved)
        self.assertEqual(case.temporary.cleanup_calls, 1)
        self.assertFalse(Path(case.temporary.name).exists())

    def test_a_healthy_run_does_not_stop_the_following_case(self) -> None:
        """Control for OC-A2: stopping is caused by the failure, not by cleanup."""

        driven = self.drive(_FakeOwnedChild())

        self.assertFalse(driven["result"].shouldStop)
        self.assertEqual(driven["sentinel"].ran, ["sentinel"])
        self.assertEqual(driven["case"].release_failures, [])
        self.assertFalse(driven["case"].holder_preserved)
        self.assertTrue(driven["case"].removed_root)



if __name__ == "__main__":  # pragma: no cover - convenience for local runs
    unittest.main()

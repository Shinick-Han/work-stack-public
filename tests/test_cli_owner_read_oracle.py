"""Owner-held CLI read oracle: six frozen GET routes and their ordinary CLI use.

The oracle for every route is the WorkStack domain method itself, never a GUI
projection: ``/api/v1/tasks/{id}`` adds ``context_count`` and drops
``status_fact_id``, and substituting it here is exactly the defect these tests
exist to catch. The real ephemeral owner, its recording relay and the contained
temporary workspace come from the admitted checkin fixture unchanged; the read
requests are this file's own client rather than the writer's POST wire, so a
change to write framing cannot quietly rewrite what a read is asserted to be.

The base has none of these routes, so the failures here are expected until the
runtime lane lands: a 404 envelope is the RED signal, not a broken test.
"""

from __future__ import annotations

import contextlib
import datetime
import http.client
import io
import json
import threading
import unittest
import uuid
from unittest import mock
from urllib.parse import urlencode

# Package qualified: ``python -m unittest tests.test_cli_owner_read_oracle`` and
# ``python -m unittest discover -s tests`` put different directories on sys.path,
# and only the repository root is on both.
from tests import test_cli_worklog_checkin_writer_contract as owner_fixture
from workstack.storage.document_repository import WorkspaceDocument

DAY = "2026-09-03"
QUIET = "2026-09-02"
EARLIER = "2026-09-01"
ABSENT_DAY = "2026-08-01"

# The owner is bound to an ephemeral port so it can never be the advertised
# desktop port; both are accepted Host values, which is why the two must differ
# for the Host assertions below to mean anything.
PUBLIC_PORT = 28765

BACKLOG = "/api/v1/cli/backlog"
OKR = "/api/v1/cli/okr"
ROLLUP = "/api/v1/cli/okr/rollup"
WORKLOG = "/api/v1/cli/worklog"
WEEKLY = "/api/v1/cli/weekly"

_ABSENT = object()


class _ContendedProcessLock:
    """The Store's own process lock, announcing when a caller must truly wait.

    A blocking acquire that fails its non-blocking probe is proof that another
    thread holds the boundary right now. That is the deterministic signal the
    overlap tests need: no sleep, no polling, and no guess about scheduling.
    """

    def __init__(self, lock, on_contention):
        self._lock = lock
        self._on_contention = on_contention

    def acquire(self, blocking=True, timeout=-1):
        if self._lock.acquire(False):
            return True
        if not blocking:
            return False
        self._on_contention()
        return self._lock.acquire(True, timeout)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return True

    def __exit__(self, *_exception):
        self._lock.release()
        return False

    def __getattr__(self, name):
        return getattr(self._lock, name)


class _ReadCase(owner_fixture._Case):
    """The admitted owner fixture, seeded for reads and given a read client."""

    def setUp(self):
        super().setUp()
        self.seed_reads()

    def seed_reads(self):
        stack = self.stack
        self.objective = stack.add_objective("Read oracle objective 한글", "2026-Q3")
        self.open_task = stack.add_task("Open task 한글")
        self.started_task = stack.add_task(
            "Started task", "detail", "P1", None, ["tag"], [self.objective["id"]], None, []
        )
        self.done_task = stack.add_task("Done task")
        stack.set_task_status(self.started_task["id"], "started")
        stack.set_task_status(self.done_task["id"], "done")
        stack.checkin("09:00", DAY)
        stack.checkin("08:00", QUIET)
        stack.add_worklog(self.started_task["id"], ["done item"], ["next item"], [], DAY)
        stack.add_worklog(self.open_task["id"], ["earlier item"], [], ["blocked"], EARLIER)
        manifest = json.loads(self.store.store_manifest_path.read_text(encoding="utf8"))
        self.workspace_uid = manifest["workspace_id"]
        self.assertEqual(str(uuid.UUID(self.workspace_uid)), self.workspace_uid)

    def start_owner(self):
        """The fixture's owner, bound ephemerally beside the advertised port."""

        from workstack import server as server_module

        create = server_module.create_server

        def with_public_port(stack, host, port):
            return create(stack, host, port, public_port=PUBLIC_PORT)

        with mock.patch.object(server_module, "create_server", with_public_port):
            relay = super().start_owner()
        self.assertNotEqual(self.owner.actual_port, PUBLIC_PORT)
        return relay

    def every_route(self):
        return [
            ("backlog.list", BACKLOG),
            ("backlog.show", self.detail_path()),
            ("okr.list", OKR),
            ("okr.rollup", ROLLUP),
            ("worklog.list", WORKLOG),
            ("weekly", WEEKLY),
        ]

    def detail_path(self, task_id=None):
        return "{}/{}".format(BACKLOG, self.started_task["id"] if task_id is None else task_id)

    def get(self, path, *, params=(), uid=_ABSENT, headers=(), query=None, timeout=5):
        """One independent read request. A None header value suppresses it."""

        port = self.owner.actual_port
        if query is None:
            pairs = list(params)
            if uid is _ABSENT:
                uid = self.workspace_uid
            if uid is not None:
                pairs.insert(0, ("workspace_uid", uid))
            query = urlencode(pairs)
        target = "{}?{}".format(path, query) if query else path
        defaults = [
            ("Host", "127.0.0.1:{}".format(port)),
            ("Origin", "http://127.0.0.1:{}".format(port)),
            ("X-WorkStack-CSRF", self.owner.csrf_token),
        ]
        overridden = {name.lower() for name, _value in headers}
        sent = [(name, value) for name, value in defaults if name.lower() not in overridden]
        sent.extend((name, value) for name, value in headers if value is not None)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            connection.putrequest("GET", target, skip_host=True, skip_accept_encoding=True)
            for name, value in sent:
                connection.putheader(name, value)
            connection.endheaders()
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def data(self, response):
        status, payload = response
        self.assertEqual(status, 200, payload)
        self.assertEqual(list(payload), ["data"])
        return payload["data"]

    def refused(self, response, *allowed):
        status, payload = response
        self.assertIn(status, allowed, payload)
        self.assertEqual(list(payload), ["error"])
        self.assertIn("code", payload["error"])

    def refused_exactly(self, response, status, code):
        """One refusal identity, so a status or code regression cannot pass."""

        observed, payload = response
        self.assertEqual(observed, status, payload)
        self.assertEqual(list(payload), ["error"])
        self.assertEqual(payload["error"].get("code"), code, payload)

    def overlapping_owner_write(self, request, write):
        """Suspend one real GET mid-read while a real owner write contends.

        The read is held at its first Task document load, so the second load is
        still outstanding; only then does the writer start. ``contended`` is
        True when that writer had to block on the very Store boundary the read
        is holding, which is what "overlapping" has to mean once the read is
        serialized. Nothing here waits on a clock for its evidence.
        """

        reader_paused = threading.Event()
        release_reader = threading.Event()
        writer_blocked = threading.Event()
        armed = [True]
        arm_lock = threading.Lock()
        writer_thread = [None]
        answer = {}
        original_load = self.stack.documents.load

        def interleave(document):
            value = original_load(document)
            pause = False
            if document == WorkspaceDocument.TASKS:
                with arm_lock:
                    if armed[0] and threading.current_thread() is not writer_thread[0]:
                        armed[0] = False
                        pause = True
            if pause:
                reader_paused.set()
                if not release_reader.wait(30):
                    raise RuntimeError("the suspended owner read was never released")
            return value

        def note_contention():
            if threading.current_thread() is writer_thread[0]:
                writer_blocked.set()

        def run(key, call):
            def body():
                try:
                    answer[key] = call()
                except BaseException as error:  # reported by the assertions below
                    answer[key + "_error"] = error

            return body

        lock = _ContendedProcessLock(self.store._process_lock, note_contention)
        contexts = contextlib.ExitStack()
        with contexts:
            contexts.enter_context(
                mock.patch.object(self.stack.documents, "load", side_effect=interleave)
            )
            contexts.enter_context(mock.patch.object(self.store, "_process_lock", lock))
            reader = threading.Thread(target=run("response", request), name="oracle-read")
            writer = threading.Thread(target=run("write", write), name="oracle-write")
            writer_thread[0] = writer
            reader.start()
            try:
                self.assertTrue(reader_paused.wait(20), "the read never reached the barrier")
                writer.start()
                contended = writer_blocked.wait(20)
            finally:
                release_reader.set()
            reader.join(30)
            writer.join(30)
        self.assertFalse(reader.is_alive(), "the suspended read never finished")
        self.assertFalse(writer.is_alive(), "the overlapping write never finished")
        for key in ("response_error", "write_error"):
            if key in answer:
                raise answer[key]
        return answer["response"], contended

    def invoke_cli(self, *args):
        from workstack import cli

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as contexts:
            contexts.enter_context(contextlib.redirect_stdout(stdout))
            contexts.enter_context(contextlib.redirect_stderr(stderr))
            contexts.enter_context(
                mock.patch.object(cli, "WorkStack", side_effect=AssertionError("local fallback"))
            )
            code = cli.main(["--data-dir", str(self.root), *args])
        return code, stdout.getvalue(), stderr.getvalue()


class OwnerReadOracle(_ReadCase):
    def test_every_route_answers_with_its_exact_domain_result(self):
        self.start_owner()
        cases = [
            (BACKLOG, (), lambda: self.stack.list_tasks("active")),
            (BACKLOG, (("status", "active"),), lambda: self.stack.list_tasks("active")),
            (BACKLOG, (("status", "all"),), lambda: self.stack.list_tasks("all")),
            (BACKLOG, (("status", "open"),), lambda: self.stack.list_tasks("open")),
            (BACKLOG, (("status", "started"),), lambda: self.stack.list_tasks("started")),
            (BACKLOG, (("status", "done"),), lambda: self.stack.list_tasks("done")),
            (BACKLOG, (("status", "dropped"),), lambda: self.stack.list_tasks("dropped")),
            (self.detail_path(), (), lambda: self.stack.get_task(self.started_task["id"])),
            (self.detail_path(self.done_task["id"]), (), lambda: self.stack.get_task(self.done_task["id"])),
            (OKR, (), lambda: self.stack.list_objectives("active")),
            (OKR, (("status", "all"),), lambda: self.stack.list_objectives("all")),
            (OKR, (("status", "done"),), lambda: self.stack.list_objectives("done")),
            (ROLLUP, (), self.stack.objective_rollup),
            (WORKLOG, (), lambda: self.stack.list_worklog(None)),
            (WORKLOG, (("date", DAY),), lambda: self.stack.list_worklog(DAY)),
            (WEEKLY, (("end", DAY),), lambda: self.stack.weekly_report(DAY, 7)),
            (WEEKLY, (("end", DAY), ("days", "1")), lambda: self.stack.weekly_report(DAY, 1)),
        ]
        for path, params, expected in cases:
            with self.subTest(path=path, params=params):
                self.assertEqual(self.data(self.get(path, params=params)), expected())

    def test_reads_carry_the_domain_status_fact_not_the_gui_projection(self):
        self.start_owner()
        task_id = self.started_task["id"]
        detail = self.data(self.get(self.detail_path(task_id)))
        self.assertEqual(detail, self.stack.get_task(task_id))
        self.assertRegex(detail["status_fact_id"], r"\APS-[0-9]{6,}\Z")
        self.assertNotIn("context_count", detail)
        listed = self.data(self.get(BACKLOG, params=(("status", "all"),)))
        self.assertEqual(
            [task.get("status_fact_id") for task in listed],
            [task.get("status_fact_id") for task in self.stack.list_tasks("all")],
        )
        self.assertIn(detail["status_fact_id"], [task.get("status_fact_id") for task in listed])
        projection = self.get("/api/v1/tasks/{}".format(task_id))
        self.assertEqual(projection[0], 200)
        self.assertNotEqual(projection[1]["data"], detail)

    def test_backlog_status_filter_and_unknown_status_are_domain_faithful(self):
        self.start_owner()
        every = self.data(self.get(BACKLOG, params=(("status", "all"),)))
        active = self.data(self.get(BACKLOG))
        self.assertEqual(
            {task["status"] for task in active},
            {task["status"] for task in every} & {"open", "started"},
        )
        self.assertLess(len(active), len(every))
        for status in ("", "ALL", "unknown", "active ", "open,started"):
            with self.subTest(status=status):
                self.refused(self.get(BACKLOG, params=(("status", status),)), 400)
                self.refused(self.get(OKR, params=(("status", status),)), 400)

    def test_backlog_show_refuses_unknown_and_injected_identifiers(self):
        self.start_owner()
        # Anchored on the canonical spelling so a missing route cannot make the
        # refusals below pass for the wrong reason.
        self.data(self.get(self.detail_path()))
        # A well-formed identity that names no Task is a domain miss (404); an
        # identity the grammar refuses never reaches the domain at all (400).
        unknown = ["T-9999", "T-000000", self.objective["id"]]
        invalid = [
            "%2e%2e%2f%2e%2e%2fapi%2fv1%2fsession",
            "%2e%2e",
            "%2e",
            "T-1%00",
            "T-1%0d%0aX-Injected:%20yes",
            "%2fetc%2fpasswd",
            "%5cwindows%5csystem32",
            "T-" + "9" * 200,
        ]
        for target in unknown:
            with self.subTest(unknown=target):
                self.refused_exactly(self.get(self.detail_path(target)), 404, "not_found")
        for target in invalid:
            with self.subTest(invalid=target):
                self.refused_exactly(self.get(self.detail_path(target)), 400, "invalid_query")

    def test_worklog_reads_cover_seeded_quiet_and_absent_days(self):
        self.start_owner()
        for date in (DAY, EARLIER, QUIET, ABSENT_DAY):
            with self.subTest(date=date):
                self.assertEqual(
                    self.data(self.get(WORKLOG, params=(("date", date),))),
                    self.stack.list_worklog(date),
                )
        self.assertEqual(self.data(self.get(WORKLOG, params=(("date", QUIET),)))["entries"], [])
        self.assertEqual(self.data(self.get(WORKLOG, params=(("date", ABSENT_DAY),)))["entries"], [])
        self.assertTrue(self.data(self.get(WORKLOG, params=(("date", DAY),)))["entries"])
        for date in ("not-a-date", "2026-13-01", "2026-02-30", "2026-09-03T00:00:00Z"):
            with self.subTest(date=date):
                self.refused(self.get(WORKLOG, params=(("date", date),)), 400)

    def test_weekly_defaults_to_seven_days_and_bounds_its_window(self):
        self.start_owner()
        self.assertEqual(self.data(self.get(WEEKLY)), self.stack.weekly_report(None, 7))
        for end, days in ((DAY, 1), (DAY, 7), (DAY, 366), (EARLIER, 2)):
            with self.subTest(end=end, days=days):
                answer = self.data(
                    self.get(WEEKLY, params=(("end", end), ("days", str(days))))
                )
                self.assertEqual(answer, self.stack.weekly_report(end, days))
                start = datetime.date.fromisoformat(end) - datetime.timedelta(days=days - 1)
                self.assertEqual(
                    answer["range"], {"start": start.isoformat(), "end": end, "days": days}
                )
        for days in ("0", "-1", "367", "seven", "7.0", "", " 7", "07 "):
            with self.subTest(days=days):
                self.refused(self.get(WEEKLY, params=(("end", DAY), ("days", days))), 400)
        for end in ("not-a-date", "2026-13-01"):
            with self.subTest(end=end):
                self.refused(self.get(WEEKLY, params=(("end", end),)), 400)

    def test_workspace_uid_is_required_and_canonical_on_every_route(self):
        self.start_owner()
        malformed = [
            None,
            "",
            "not-a-uuid",
            self.workspace_uid.upper(),
            "urn:uuid:{}".format(self.workspace_uid),
            "{{{}}}".format(self.workspace_uid),
            self.workspace_uid.replace("-", ""),
            "00000000-0000-0000-0000-000000000000",
            "{} ".format(self.workspace_uid),
        ]
        malformed = [value for value in malformed if value != self.workspace_uid]
        foreign = str(uuid.uuid4())
        for label, path in self.every_route():
            for value in malformed:
                with self.subTest(route=label, uid=value):
                    self.refused(self.get(path, uid=value), 400)
            with self.subTest(route=label, uid="foreign"):
                # Canonical but not this data directory: a binding refusal, not
                # a grammar one, so 409 workspace_mismatch is the only answer.
                self.refused_exactly(self.get(path, uid=foreign), 409, "workspace_mismatch")

    def test_unknown_repeated_and_foreign_query_keys_are_refused(self):
        self.start_owner()
        uid = self.workspace_uid
        shared = [
            ("unknown", [("workspace_uid", uid), ("limit", "1")]),
            ("alias", [("workspace_uid", uid), ("workspaceUid", uid)]),
            ("repeated_uid", [("workspace_uid", uid), ("workspace_uid", uid)]),
            ("empty_key", [("workspace_uid", uid), ("", "")]),
        ]
        for label, path in self.every_route():
            for name, pairs in shared:
                with self.subTest(route=label, case=name):
                    self.refused(self.get(path, uid=None, params=pairs), 400)
        foreign = [
            (BACKLOG, ("date", DAY)),
            (BACKLOG, ("days", "7")),
            (self.detail_path(), ("status", "all")),
            (OKR, ("date", DAY)),
            (ROLLUP, ("status", "all")),
            (WORKLOG, ("status", "all")),
            (WEEKLY, ("status", "all")),
        ]
        for path, pair in foreign:
            with self.subTest(path=path, pair=pair):
                self.refused(self.get(path, params=(pair,)), 400)
        repeated = [
            (BACKLOG, ("status", "all")),
            (OKR, ("status", "all")),
            (WORKLOG, ("date", DAY)),
            (WEEKLY, ("days", "7")),
        ]
        for path, pair in repeated:
            with self.subTest(path=path, repeated=pair):
                self.refused(self.get(path, params=(pair, pair)), 400)

    def test_params_alias_and_path_variants_are_not_routes(self):
        self.start_owner()
        for _label, path in self.every_route():
            self.data(self.get(path))
        targets = [
            BACKLOG + ";x=1",
            OKR + ";x=1",
            ROLLUP + ";x=1",
            WORKLOG + ";x=1",
            WEEKLY + ";x=1",
            self.detail_path() + ";x=1",
            BACKLOG + "/",
            BACKLOG + "//" + self.started_task["id"],
            BACKLOG + "/../okr",
            OKR + "/rollup/extra",
            WEEKLY + "/",
            "/api/v1/cli",
            "/api/v1/cli/BACKLOG",
            "/api/v1/cli/weekly/rollup",
        ]
        for target in targets:
            with self.subTest(target=target):
                self.refused(self.get(target), 400, 404)

    def test_idempotency_key_is_refused_on_every_read(self):
        self.start_owner()
        keys = [
            (("Idempotency-Key", ""),),
            (("Idempotency-Key", "ordinary-key-123"),),
            (("idempotency-key", "one"), ("IDEMPOTENCY-KEY", "two")),
        ]
        for label, path in self.every_route():
            for headers in keys:
                with self.subTest(route=label, headers=headers):
                    self.refused(self.get(path, headers=headers), 400)

    def test_host_origin_and_csrf_safeguards_still_apply(self):
        self.start_owner()
        token = self.owner.csrf_token
        same_host = "127.0.0.1:{}".format(self.owner.actual_port)
        cases = [
            ((("Origin", None),), (403,)),
            ((("Origin", "http://evil.invalid"),), (403,)),
            ((("Origin", "http://127.0.0.1:{}".format(PUBLIC_PORT)),), (403,)),
            ((("Origin", "https://127.0.0.1:{}".format(self.owner.actual_port)),), (403,)),
            ((("X-WorkStack-CSRF", None),), (403,)),
            ((("X-WorkStack-CSRF", "wrong"),), (403,)),
            ((("X-WorkStack-CSRF", token), ("X-WorkStack-CSRF", token)), (400,)),
            ((("Host", None),), (400,)),
            ((("Host", "evil.invalid"),), (400,)),
            ((("Host", "127.0.0.1:1"),), (400,)),
            ((("Host", same_host), ("Host", same_host)), (400,)),
        ]
        for label, path in self.every_route():
            for headers, expected in cases:
                with self.subTest(route=label, headers=headers):
                    self.refused(self.get(path, headers=headers), *expected)

    def test_reads_are_coherent_with_a_concurrent_owner_write(self):
        self.start_owner()
        before = self.data(self.get(BACKLOG, params=(("status", "all"),)))
        creation = {
            "title": "Concurrent task 한글", "detail": "", "priority": "P2", "due": None,
            "tags": [], "objective_ids": [self.objective["id"]], "parent_id": None,
            "dependencies": [],
        }
        status, payload = self.wire(creation, route="/api/v1/cli/backlog/add")
        self.assertEqual(status, 200, payload)
        created = payload["data"]["id"]
        after = self.data(self.get(BACKLOG, params=(("status", "all"),)))
        # list_tasks orders by status, priority, due date and id, so a new
        # active P2 task sorts ahead of the done ones. The contract is the
        # domain's own order plus set membership, never append position.
        self.assertEqual(after, self.stack.list_tasks("all"))
        self.assertEqual(
            [task["id"] for task in after],
            [task["id"] for task in self.stack.list_tasks("all")],
        )
        self.assertEqual(
            {task["id"] for task in after}, {task["id"] for task in before} | {created}
        )
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(self.data(self.get(self.detail_path(created))), self.stack.get_task(created))
        rollup = self.data(self.get(ROLLUP))
        self.assertEqual(rollup, self.stack.objective_rollup())
        linked = {task["id"] for objective in rollup for task in objective["tasks"]}
        self.assertIn(created, linked)
        status, payload = self.wire({"date": DAY, "time": "11:30"}, route="/api/v1/cli/worklog/checkin")
        self.assertEqual(status, 200, payload)
        worklog = self.data(self.get(WORKLOG))
        self.assertEqual(worklog, self.stack.list_worklog(None))
        self.assertEqual(worklog["days"][DAY]["start_time"], "11:30")

    def test_backlog_show_is_whole_under_an_overlapping_owner_write(self):
        """A status transition that overlaps the read cannot tear its snapshot.

        ``get_task`` loads the Task document and the Activity document, so a
        commit landing between those two loads used to answer with a Task
        revision its own status fact contradicts. The read is suspended after
        the first load while a real owner write contends for the same Store.
        """

        self.start_owner()
        task_id = self.open_task["id"]
        before = self.stack.get_task(task_id)
        response, contended = self.overlapping_owner_write(
            lambda: self.get(self.detail_path(task_id), timeout=60),
            lambda: self.stack.set_task_status(task_id, "started"),
        )
        after = self.stack.get_task(task_id)
        self.assertNotEqual(before, after)
        self.assertNotEqual(before["revision"], after["revision"])
        self.assertNotEqual(before["status_fact_id"], after["status_fact_id"])
        observed = self.data(response)
        self.assertIn(observed, (before, after))
        self.assertEqual(observed, before)
        self.assertTrue(contended, "the overlapping write never met the read's boundary")
        self.assertEqual(self.data(self.get(self.detail_path(task_id))), after)

    def test_backlog_list_is_whole_under_an_overlapping_owner_write(self):
        """The same boundary for the list reader, which shares both loads."""

        self.start_owner()
        task_id = self.open_task["id"]
        before = self.stack.list_tasks("all")
        response, contended = self.overlapping_owner_write(
            lambda: self.get(BACKLOG, params=(("status", "all"),), timeout=60),
            lambda: self.stack.set_task_status(task_id, "started"),
        )
        after = self.stack.list_tasks("all")
        self.assertNotEqual(before, after)
        observed = self.data(response)
        self.assertIn(observed, (before, after))
        self.assertEqual(observed, before)
        self.assertEqual({task["id"] for task in observed}, {task["id"] for task in after})
        self.assertTrue(contended, "the overlapping write never met the read's boundary")
        self.assertEqual(self.data(self.get(BACKLOG, params=(("status", "all"),))), after)

    def test_ordinary_cli_reads_use_http_without_a_second_writer(self):
        from workstack.store import Store

        relay = self.start_owner()
        cases = [
            (["backlog", "list", "--status", "all"], BACKLOG, lambda: self.stack.list_tasks("all")),
            (["backlog", "list"], BACKLOG, lambda: self.stack.list_tasks("active")),
            (["backlog", "show", self.started_task["id"]], self.detail_path(),
             lambda: self.stack.get_task(self.started_task["id"])),
            (["okr", "list"], OKR, lambda: self.stack.list_objectives("active")),
            (["okr", "rollup"], ROLLUP, self.stack.objective_rollup),
            (["worklog", "list", "--date", DAY], WORKLOG, lambda: self.stack.list_worklog(DAY)),
            (["weekly", "--end", DAY, "--days", "7"], WEEKLY, lambda: self.stack.weekly_report(DAY, 7)),
        ]
        acquire = Store.try_acquire_writer_lease
        before = self.snapshot()
        for arguments, path, expected in cases:
            with self.subTest(arguments=arguments):
                start = len(relay.requests)
                leases = []

                def record(store, _acquire=acquire, _leases=leases):
                    lease = _acquire(store)
                    _leases.append(lease)
                    return lease

                with mock.patch.object(Store, "try_acquire_writer_lease", record):
                    code, out, err = self.invoke_cli(*arguments)
                self.assertEqual((code, err), (0, ""))
                answer = expected()
                self.assertEqual(out, json.dumps(answer, ensure_ascii=False, indent=2) + "\n")
                observed = relay.requests[start:]
                self.assertEqual([request[0] for request in observed], ["GET"] * len(observed))
                self.assertTrue(
                    any(request[1] == path or request[1].startswith(path + "?") for request in observed),
                    [request[1] for request in observed],
                )
                self.assertEqual(leases, [None] * len(leases))
                self.assertTrue(leases)
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()

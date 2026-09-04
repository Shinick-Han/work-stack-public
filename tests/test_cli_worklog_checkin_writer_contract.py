"""Ordinary CLI checkin parity through the real owner's one-attempt boundary."""
from __future__ import annotations

import contextlib
import builtins
import datetime
import errno
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import traceback
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PATH = "/api/v1/cli/worklog/checkin"
PREFLIGHT = ["/api/v1/session", "/api/v1/storage", "/api/v1/sync/status"]
DAY = "2026-09-03"
BODY = {"date": DAY, "time": "09:42"}


class _Date(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 3)


class _Datetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 3, 9, 42, tzinfo=tz)


class _RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self._relay("GET")

    def do_POST(self):
        self._relay("POST")

    def _relay(self, method):
        relay = self.server
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        headers = dict(self.headers.items())
        relay.requests.append((method, self.path, raw, headers.copy()))
        headers["Host"] = f"127.0.0.1:{relay.backend_port}"
        if "Origin" in headers:
            headers["Origin"] = f"http://127.0.0.1:{relay.backend_port}"
        if method == "POST" and relay.before_post:
            relay.before_post()
        connection = http.client.HTTPConnection("127.0.0.1", relay.backend_port, timeout=5)
        try:
            connection.request(method, self.path, raw or None, headers)
            response = connection.getresponse()
            status, data = response.status, response.read()
        finally:
            connection.close()
        status, data, drop = self._transform(method, status, data)
        if drop:
            self.close_connection = True
            return
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _transform(self, method, status, raw):
        relay = self.server
        payload = json.loads(raw)
        if method == "POST":
            relay.responses.append((status, payload))
            if relay.drop and status == 200:
                return status, raw, True
            if relay.raw_success is not None and status == 200:
                return status, relay.raw_success, False
            if relay.transform and status == 200:
                status, payload = relay.transform(status, payload)
        elif self.path in relay.get_hooks:
            relay.get_hooks[self.path](payload)
        return status, json.dumps(payload, ensure_ascii=False).encode("utf8"), False


class _IdleHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        self.server.contacts.append(self.path)
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.do_POST()


class _Case(unittest.TestCase):
    def setUp(self):
        result = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
        self.temporary = tempfile.TemporaryDirectory(dir=result)
        self.home = Path(self.temporary.name)
        self.addCleanup(self._remove)
        self.endpoints = []
        self.expected_errors = {}
        self.root = self.home / "data"
        self.local_number = 0
        runtime = self.home / "runtime"
        runtime.mkdir()
        patch = mock.patch.dict(os.environ, {"WORK_STACK_RUNTIME": str(runtime)})
        patch.start()
        self.addCleanup(patch.stop)
        from workstack.service import WorkStack
        from workstack.store import Store
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.owner = None
        self.relay = None

    def _remove(self):
        for label, server, thread in self.endpoints:
            self.assertFalse(thread.is_alive(), label)
            self.assertEqual(server.errors, self.expected_errors.get(label, []), label)
        self.temporary.cleanup()
        self.assertFalse(self.home.exists())

    def _endpoint(self, server, label):
        server.errors = []

        def handle_error(_request, _address):
            server.errors.append(traceback.format_exc())

        server.handle_error = handle_error
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.endpoints.append((label, server, thread))
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def start_owner(self):
        from workstack.server import create_server
        self.owner = self._endpoint(create_server(self.stack, "127.0.0.1", 0), "owner")
        relay = ThreadingHTTPServer(("127.0.0.1", 0), _RelayHandler)
        relay.backend_port = self.owner.actual_port
        relay.requests, relay.responses = [], []
        relay.drop, relay.transform, relay.before_post = False, None, None
        relay.raw_success = None
        relay.get_hooks = {}
        self.relay = self._endpoint(relay, "relay")
        self.advertise(relay.server_address[1])
        return relay

    def advertise(self, port):
        self.store.server_info_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.server_info_path.write_text(json.dumps({
            "version": 1, "host": "127.0.0.1", "port": port,
        }), encoding="utf8")

    def snapshot(self, root=None):
        from workstack.store import DEFAULTS
        return {name: ((root or self.root) / name).read_bytes() for name in DEFAULTS}

    def seed(self):
        from workstack.storage.document_repository import WorkspaceDocument
        self.stack.add_task("Unchanged Task 한글")
        worklog = self.stack.documents.load(WorkspaceDocument.WORKLOG)
        worklog["days"] = {
            DAY: {"start_time": "07:10", "entries": [{"opaque": [True, 1]}], "extra": "keep"},
            "2026-09-01": {"start_time": "08:00", "entries": [{"old": "entry"}]},
            "2026-09-02": {"extra": {"no_entries": True}},
        }
        self.stack.documents.save(WorkspaceDocument.WORKLOG, worklog)

    def run_cli(self, *args, root=None, forbid_local=True):
        from workstack import cli
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as contexts:
            contexts.enter_context(contextlib.redirect_stdout(stdout))
            contexts.enter_context(contextlib.redirect_stderr(stderr))
            if self.owner is not None and root is None and forbid_local:
                contexts.enter_context(mock.patch.object(cli, "WorkStack", side_effect=AssertionError("local fallback")))
            code = cli.main(["--data-dir", str(root or self.root), "worklog", "checkin", *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke(self, date=DAY, time="09:42"):
        return self.run_cli("--date", date, "--time", time)

    def assert_success(self, result, date=DAY, time="09:42"):
        self.assertEqual(result, (0, json.dumps({"date": date, "start_time": time}, ensure_ascii=False, indent=2) + "\n", ""))

    def assert_unknown(self, result):
        self.assertEqual(result[:2], (2, ""))
        self.assertIn("checkin commit is unknown", result[2])

    def assert_only_worklog_changed(self, before):
        after = self.snapshot()
        self.assertEqual({k: v for k, v in before.items() if k != "worklog.json"},
                         {k: v for k, v in after.items() if k != "worklog.json"})

    def posts(self):
        return [r for r in self.relay.requests if r[0] == "POST"]

    def wire(self, body=BODY, *, raw=None, headers=(), route=PATH, length=None):
        data = json.dumps(body).encode("utf8") if raw is None else raw
        port = self.owner.actual_port
        pairs = [("Content-Type", "application/json"), ("Host", f"127.0.0.1:{port}"), ("Origin", f"http://127.0.0.1:{port}"),
                 ("X-WorkStack-CSRF", self.owner.csrf_token)]
        overrides = {name.lower() for name, _value in headers}
        pairs = [(name, value) for name, value in pairs if name.lower() not in overrides]
        pairs.extend(headers)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.putrequest("POST", route, skip_host=True)
            for name, value in pairs:
                connection.putheader(name, value)
            connection.putheader("Content-Length", str(len(data) if length is None else length))
            connection.endheaders(data)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


class CheckinParity(_Case):
    def test_absent_owner_remains_local(self):
        self.assert_success(self.invoke())
        self.assertFalse(self.store.server_info_path.exists())

    def test_running_owner_matches_local_bytes_and_legacy_spellings(self):
        self.seed()
        self.start_owner()
        vectors = [(DAY, "09:42"), ("20260903", "10:00"), ("2026-W36-4", "11:00"),
                   ("2026-09-02", "12:00\n"), (DAY, "0١:3٢")]
        for date, time in vectors:
            with self.subTest(date=date, time=time):
                self._compare(date, time)
        days = json.loads(self.snapshot()["worklog.json"])["days"]
        self.assertTrue({DAY, "20260903", "2026-W36-4"} <= set(days))
        self.assertNotIn("entries", days["2026-09-02"])

    def _compare(self, date, time):
        local = self.home / f"local-{self.local_number}"
        self.local_number += 1
        shutil.copytree(self.root, local, ignore=shutil.ignore_patterns(".workstack.lock"))
        before = self.snapshot()
        local_result = self.run_cli("--date", date, "--time", time, root=local)
        from workstack.storage.document_repository import WorkspaceDocument
        with mock.patch.object(self.stack, "checkin", wraps=self.stack.checkin) as setter, mock.patch.object(
            self.stack.documents, "save", wraps=self.stack.documents.save
        ) as save:
            owner_result = self.invoke(date, time)
        self.assert_success(owner_result, date, time)
        self.assertEqual(owner_result, local_result)
        self.assertEqual(self.snapshot(), self.snapshot(local))
        setter.assert_called_once_with(time=time, date=date)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(save.call_args.args[0], WorkspaceDocument.WORKLOG)
        after = self.snapshot()
        self.assertEqual({k: v for k, v in before.items() if k != "worklog.json"},
                         {k: v for k, v in after.items() if k != "worklog.json"})

    def test_omitted_and_empty_date_use_frozen_cli_clock(self):
        self.start_owner()
        from workstack import cli_writer
        clock = types.SimpleNamespace(date=_Date, datetime=_Datetime)
        for args in [(), ("--date", ""), ("--date", DAY)]:
            with self.subTest(args=args), mock.patch.object(cli_writer, "datetime", clock), mock.patch(
                "workstack.service.today", side_effect=AssertionError("remote clock")
            ):
                self.assert_success(self.run_cli(*args))
                self.assertEqual(json.loads(self.posts()[-1][2]), BODY)

    def test_clock_defaults_match_the_actual_local_dispatcher(self):
        from workstack import cli_writer, service
        local_clock = types.SimpleNamespace(date=datetime.date, datetime=_Datetime)
        owner_clock = types.SimpleNamespace(date=_Date, datetime=_Datetime)
        with mock.patch.object(service, "today", return_value=DAY), mock.patch.object(service, "dt", local_clock):
            expected = self.run_cli()
        self.start_owner()
        with mock.patch.object(cli_writer, "datetime", owner_clock):
            self.assertEqual(self.run_cli(), expected)

    def test_empty_time_and_date_first_domain_refusals(self):
        self.start_owner()
        for date, time, expected in [(DAY, "", "time must use HH:MM"), ("bad-date", "bad-time", "Invalid isoformat")]:
            with self.subTest(date=date):
                before = self.snapshot()
                result = self.invoke(date, time)
                self.assertEqual(result[:2], (2, ""))
                self.assertIn("HTTP 400", result[2])
                self.assertIn(expected, self.relay.responses[-1][1]["error"]["message"])
                self.assertEqual(before, self.snapshot())
        with self.assertRaisesRegex(ValueError, "Invalid isoformat"):
            self.stack.checkin(time="bad-time", date="bad-date")

    def test_exact_wire_and_repeated_invocations_have_no_receipts(self):
        relay = self.start_owner()
        activity = self.snapshot()["activity.json"]
        for _ in range(2):
            self.assert_success(self.invoke())
        self.assertEqual([r[1] for r in relay.requests], (PREFLIGHT + [PATH]) * 2)
        self.assertEqual(len(self.posts()), 2)
        for _, route, raw, headers in self.posts():
            self.assertEqual((route, json.loads(raw)), (PATH, BODY))
            self.assertNotIn("idempotency-key", {key.lower() for key in headers})
            self.assertNotIn("x-workstack-client", {key.lower() for key in headers})
            self.assertEqual(headers["Origin"], f"http://127.0.0.1:{relay.server_address[1]}")
            self.assertEqual(headers["X-WorkStack-CSRF"], self.owner.csrf_token)
        self.assertEqual(relay.responses, [(200, {"data": {"date": DAY, "start_time": "09:42"}})] * 2)
        self.assertEqual(activity, self.snapshot()["activity.json"])


class CheckinFraming(_Case):
    def test_security_and_exact_json_refuse_before_setter(self):
        self.start_owner()
        cases = [(BODY, b"{", (), 400), ([], None, (), 400), ({"date": DAY}, None, (), 400),
                 ({**BODY, "extra": True}, None, (), 400), ({**BODY, "time": 1}, None, (), 400),
                 (BODY, None, (("Origin", "http://evil.invalid"),), 403),
                 (BODY, None, (("X-WorkStack-CSRF", "bad"),), 403),
                 (BODY, None, (("Host", "evil.invalid"),), 400), (BODY, b"\xff", (), 400)]
        for body, raw, headers, status in cases:
            with self.subTest(body=body, headers=headers), mock.patch.object(self.stack, "checkin", wraps=self.stack.checkin) as setter:
                before = self.snapshot()
                self.assertEqual(self.wire(body, raw=raw, headers=headers)[0], status)
                setter.assert_not_called()
                self.assertEqual(before, self.snapshot())

    def test_any_key_presence_is_refused(self):
        self.start_owner()
        for headers in [(('Idempotency-Key', ''),), (('Idempotency-Key', 'valid-key-123'),),
                        (('iDeMpOtEnCy-KeY', 'a'), ('IDEMPOTENCY-KEY', 'b'))]:
            with self.subTest(headers=headers), mock.patch.object(self.stack, "checkin", wraps=self.stack.checkin) as setter:
                before = self.snapshot()
                status, payload = self.wire(headers=headers)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "unsupported_idempotency_key")
                setter.assert_not_called()
                self.assertEqual(before, self.snapshot())

    def test_body_limit_refuses_before_mutation(self):
        self.start_owner()
        before = self.snapshot()
        with mock.patch.object(self.stack, "checkin", wraps=self.stack.checkin) as setter:
            self.assertEqual(self.wire(raw=b"", length=1048577)[0], 413)
        setter.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_strict_gui_route_keeps_canonical_dates_and_receipts(self):
        self.start_owner()
        route = "/api/v1/review/checkin"
        headers = (("Idempotency-Key", "checkin-gui-001"),)
        self.assertEqual(self.wire({**BODY, "date": "20260903"}, headers=headers, route=route)[0], 400)
        first = self.wire(headers=headers, route=route)
        replay = self.wire(headers=headers, route=route)
        self.assertEqual((first[0], first[1]["meta"]), (201, {"replayed": False}))
        self.assertEqual((replay[0], replay[1]["meta"]), (200, {"replayed": True}))
        self.assertEqual(len(json.loads(self.snapshot()["activity.json"])["idempotency"]), 1)

    def test_direct_service_shape_and_composition_before_domain(self):
        from workstack.service import DomainError
        class DerivedDict(dict):
            pass
        for body in [None, [], {}, DerivedDict(BODY), {**BODY, "time": None}, {**BODY, "extra": 1}]:
            with self.subTest(body=body), mock.patch.object(self.stack, "checkin") as setter:
                with self.assertRaises(DomainError):
                    self.stack.checkin_cli(body)
                setter.assert_not_called()

    def test_same_store_guard_refuses_cross_store_and_injected_repository(self):
        from workstack.service import WorkStack, DomainError
        from workstack.store import Store
        from workstack.storage.document_repository import StoreDocumentRepository
        other = Store(self.home / "other")
        other_stack = WorkStack(other)
        original = self.stack.documents
        injected = mock.Mock(wraps=original)
        for repository in [StoreDocumentRepository(other), injected]:
            with self.subTest(repository=type(repository).__name__), mock.patch.object(self.stack, "documents", repository), mock.patch.object(
                self.stack, "checkin", wraps=self.stack.checkin
            ) as setter, mock.patch.object(repository, "load", side_effect=AssertionError("repository read")):
                before = self.snapshot(), self.snapshot(other.root)
                with self.assertRaises(DomainError):
                    self.stack.checkin_cli(BODY)
                setter.assert_not_called()
                self.assertEqual(before, (self.snapshot(), self.snapshot(other.root)))
        self.assertIs(other_stack.store, other)

    def test_every_optional_delegate_refuses_without_calls(self):
        from workstack.service import DomainError
        names = ["capture_reply_commands", "intent_commands", "objective_commands", "task_commands",
                 "relationship_commands", "planning_commands", "work_session_commands", "query_commands"]
        for name in names:
            backend = mock.Mock()
            with self.subTest(name=name), mock.patch.object(self.stack, name, backend), mock.patch.object(
                self.stack.documents, "load", side_effect=AssertionError("read before capability")
            ), mock.patch.object(self.stack, "checkin") as setter:
                with self.assertRaises(DomainError):
                    self.stack.checkin_cli(BODY)
                setter.assert_not_called()
                self.assertEqual(backend.mock_calls, [])

    def test_http_composition_refusal_is_before_repository_read(self):
        self.start_owner()
        from workstack.storage.document_repository import StoreDocumentRepository
        from workstack.service import WorkStack
        from workstack.store import Store
        other = WorkStack(Store(self.home / "other"))
        repository = StoreDocumentRepository(other.store)
        before = self.snapshot(), self.snapshot(other.store.root)
        with mock.patch.object(self.stack, "documents", repository), mock.patch.object(
            repository, "load", side_effect=AssertionError("cross-store read")
        ), mock.patch.object(self.stack, "checkin") as setter:
            self.assertEqual(self.wire()[0], 400)
            setter.assert_not_called()
        self.assertEqual(before, (self.snapshot(), self.snapshot(other.store.root)))

    def test_actual_v4_factory_composition_refuses_before_domain_calls(self):
        from workstack.service import WorkStack, DomainError
        from workstack.store import DEFAULTS
        from workstack.storage.canonical import canonical_json_bytes
        from workstack.storage.experimental_application import create_experimental_v4_application
        from workstack.checkpoint_change import build_checkpoint_facts
        from workstack.storage.manifest import build_v4_manifest
        from workstack.storage.manifest_store import publish_runtime_manifest
        from workstack.storage.migration_conversion import convert_v3_documents
        from workstack.storage.reader import read_v4
        from workstack.storage.runtime import resolve_runtime_authority
        now = "2026-09-03T00:00:00Z"
        documents = {name: self.store.load(name) for name in DEFAULTS}
        conversion = convert_v3_documents(documents, candidate_created_at=now)
        authority = self.home / "v"
        self._write_conversion(authority, conversion)
        runtime = resolve_runtime_authority(authority, self.home / "r", str(conversion.store["workspace_uid"]))
        runtime.runtime_root.mkdir(parents=True)
        publish_runtime_manifest(runtime.manifest_path, build_v4_manifest(read_v4(authority), generation=0), expected_digest=None)
        runtime.idempotency_path.write_bytes(canonical_json_bytes(dict(conversion.idempotency_ledger)))
        application = create_experimental_v4_application(authority, runtime, enable_v4_application=True, checkpoint_facts=build_checkpoint_facts,
            clock=lambda: now, uid_factory=lambda: "11111111-1111-4111-8111-111111111111", today=lambda: DAY,
            task_note_source_indexes=conversion.task_note_source_indexes)
        stack = WorkStack(application.store)
        before = self._files(authority), self._files(runtime.runtime_root)
        with mock.patch.object(stack.documents, "load", side_effect=AssertionError("v4 read")), mock.patch.object(
            stack, "checkin", wraps=stack.checkin
        ) as setter:
            with self.assertRaises(DomainError):
                stack.checkin_cli(BODY)
            setter.assert_not_called()
        self.assertEqual(before, (self._files(authority), self._files(runtime.runtime_root)))

    def _files(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    def _write_conversion(self, root, conversion):
        from workstack.storage.canonical import canonical_json_bytes
        root.mkdir()
        (root / "store.json").write_bytes(canonical_json_bytes(dict(conversion.store)))
        (root / "workspace.json").write_bytes(canonical_json_bytes(dict(conversion.workspace)))
        for kind, records in conversion.records.items():
            for record in records:
                uid = str(record["uid"])
                path = root / "records" / kind / uid[:2] / (uid + ".json")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(canonical_json_bytes(dict(record)))
        segments = {}
        for kind, events in conversion.streams.items():
            for event in events:
                segments.setdefault((kind, str(event["created_at"])[:7]), []).append(dict(event))
        for (kind, month), events in segments.items():
            path = root / "streams" / kind / (month + ".ndjson")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"".join(canonical_json_bytes(event) + b"\n" for event in sorted(events, key=lambda event: event["sequence"])))


class CheckinOwnerTransport(_Case):
    def test_commit_then_disconnect_is_unknown_and_new_invocation_is_new_action(self):
        relay = self.start_owner()
        before = self.snapshot()
        relay.drop = True
        self.assert_unknown(self.invoke())
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual([r[1] for r in relay.requests], PREFLIGHT + [PATH])
        self.assertEqual(json.loads(self.snapshot()["worklog.json"])["days"][DAY]["start_time"], "09:42")
        self.assertEqual(before["activity.json"], self.snapshot()["activity.json"])
        self.assert_only_worklog_changed(before)
        relay.drop = False
        self.assert_success(self.invoke(time="10:00"), time="10:00")
        self.assertEqual(len(self.posts()), 2)

    def test_genuine_success_corruptions_are_unknown(self):
        relay = self.start_owner()
        variants = [(201, {"data": {"date": DAY, "start_time": "09:42"}}), (200, {}), (200, []),
                    (200, {"data": None}), (200, {"data": {"date": 1, "start_time": "09:42"}}),
                    (200, {"data": {"date": DAY, "start_time": 942}}),
                    (200, {"data": {"date": "2026-09-04", "start_time": "09:42"}}),
                    (200, {"data": {"date": DAY, "start_time": "10:00"}}),
                    (200, {"data": {"date": DAY, "start_time": "09:42"}, "meta": {}}),
                    (200, {"data": {"start_time": "09:42", "date": DAY}})]
        for changed in variants:
            with self.subTest(changed=changed):
                before = self.snapshot()
                count = len(self.posts())
                relay.transform = lambda status, payload: changed
                self.assert_unknown(self.invoke())
                self.assertEqual(len(self.posts()), count + 1)
                self.assertEqual(relay.responses[-1][0], 200)
                self.assertEqual(relay.responses[-1][1], {"data": {"date": DAY, "start_time": "09:42"}})
                self.assert_only_worklog_changed(before)

    def test_invalid_json_after_commit_is_unknown_with_one_send(self):
        relay = self.start_owner()
        before = self.snapshot()
        relay.raw_success = b'{"data":'
        self.assert_unknown(self.invoke())
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(relay.responses[-1][0], 200)
        self.assertEqual(before["activity.json"], self.snapshot()["activity.json"])
        self.assert_only_worklog_changed(before)
        self.assertEqual(json.loads(self.snapshot()["worklog.json"])["days"][DAY]["start_time"], "09:42")

    def test_real_determinate_domain_security_and_conflict_refusals(self):
        relay = self.start_owner()
        self.assertIn("HTTP 400", self.invoke(time="invalid")[2])
        self.owner.csrf_token = "changed-after-session"
        relay.get_hooks[PREFLIGHT[-1]] = lambda payload: setattr(self.owner, "csrf_token", "changed-again")
        before = self.snapshot()
        self.assertIn("HTTP 403", self.invoke()[2])
        self.assertEqual(before, self.snapshot())
        relay.get_hooks.clear()
        relay.before_post = lambda: (self.root / "worklog.json").write_bytes(before["worklog.json"] + b"\n")
        self.assertIn("HTTP 409", self.invoke()[2])
        self.assertEqual(len(self.posts()), 3)
        self.assertEqual([r[0] for r in relay.responses], [400, 403, 409])

    def test_invalid_owner_advertisements_never_initialize_locally(self):
        relay = self.start_owner()
        path = self.store.server_info_path
        for raw in [b"", b"{", b"x" * 65537]:
            with self.subTest(length=len(raw)):
                path.write_bytes(raw)
                before = self.snapshot()
                self.assertEqual(self.invoke()[:2], (2, ""))
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(before, self.snapshot())
        path.unlink()
        path.mkdir()
        self.assertEqual(self.invoke()[:2], (2, ""))
        self.assertTrue(path.is_dir())
        path.rmdir()
        self.advertise(relay.server_address[1])
        self.assertEqual(relay.requests, [])
        self.assert_success(self.invoke())

    def test_scoped_permission_denial_then_same_owner_control(self):
        relay = self.start_owner()
        from workstack import cli_writer
        original = builtins.open
        hits = []
        path = self.store.server_info_path

        def deny(target, mode="r", *args, **kwargs):
            if Path(target) == path and mode == "rb":
                hits.append(target)
                raise PermissionError(errno.EACCES, "fixture denial")
            return original(target, mode, *args, **kwargs)

        before = self.snapshot()
        advertisement = path.read_bytes()
        with mock.patch.object(cli_writer, "open", deny, create=True):
            self.assertEqual(self.invoke()[:2], (2, ""))
        self.assertEqual(hits, [path])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(path.read_bytes(), advertisement)
        self.assertEqual(relay.requests, [])
        self.assert_success(self.invoke())

    def test_foreign_identity_and_not_in_sync_refuse(self):
        relay = self.start_owner()
        for route, replacement in [(PREFLIGHT[1], {"workspace_id": "12345678-1234-4234-8234-123456789abc"}),
                                   (PREFLIGHT[2], {"state": "external-change-detected"})]:
            with self.subTest(route=route):
                relay.get_hooks[route] = lambda payload: payload["data"].update(replacement)
                before = self.snapshot()
                self.assertEqual(self.invoke()[:2], (2, ""))
                self.assertEqual(before, self.snapshot())
                self.assertEqual(self.posts(), [])
                relay.get_hooks.clear()
        self.assert_success(self.invoke())

    def test_final_binding_removed_or_replaced_has_zero_wrong_owner_contacts(self):
        relay = self.start_owner()
        idle = ThreadingHTTPServer(("127.0.0.1", 0), _IdleHandler)
        idle.contacts = []
        self._endpoint(idle, "idle")
        connection = http.client.HTTPConnection("127.0.0.1", idle.server_address[1], timeout=5)
        connection.request("POST", "/control")
        connection.getresponse().read()
        connection.close()
        self.assertEqual(idle.contacts, ["/control"])
        idle.contacts.clear()
        for replace in (False, True):
            with self.subTest(replace=replace):
                self.advertise(relay.server_address[1])
                relay.get_hooks[PREFLIGHT[-1]] = lambda payload: self._change_binding(replace, idle)
                before = self.snapshot()
                self.assertEqual(self.invoke()[:2], (2, ""))
                self.assertEqual(before, self.snapshot())
                self.assertEqual(self.posts(), [])
                self.assertEqual(idle.contacts, [])
                self._assert_binding(replace, idle)

    def _assert_binding(self, replaced, idle):
        if replaced:
            self.assertEqual(json.loads(self.store.server_info_path.read_text())["port"], idle.server_address[1])
        else:
            self.assertFalse(self.store.server_info_path.exists())

    def _change_binding(self, replace, idle):
        if replace:
            self.advertise(idle.server_address[1])
        else:
            self.store.server_info_path.unlink()

    def test_keyless_transport_rejects_contradictory_options_before_network(self):
        from workstack import cli_writer
        for values in [{"idempotency_key": "key-12345"}, {"idempotency_key": ""}, {"replay": True},
                       {"extra_headers": {"idempotency-key": "x"}}, {"method": "PATCH"}]:
            with self.subTest(values=values):
                options = dict(idempotency_key=None, replay=False, keyless_post=True)
                options.update(values)
                network = mock.Mock(side_effect=AssertionError("network"))
                with self.assertRaises(ValueError):
                    cli_writer._forward_write(self.store, "present", path=PATH, body=BODY,
                        coordinates_reader=network, request_json=network, project=lambda payload: payload,
                        changed_message="changed", unknown_message="unknown", refused_message="refused", **options)
                network.assert_not_called()

    def test_real_owner_handler_error_sink_is_load_bearing(self):
        self.start_owner()
        self.assert_success(self.invoke())
        self.assertEqual(self.owner.errors, [])
        from workstack.server import Handler
        with mock.patch.object(Handler, "_get_workspace", side_effect=RuntimeError("labelled-owner-fixture-failure")):
            connection = http.client.HTTPConnection("127.0.0.1", self.owner.actual_port, timeout=5)
            try:
                connection.request("GET", "/api/v1/workspace")
                with self.assertRaises(http.client.RemoteDisconnected):
                    connection.getresponse()
            finally:
                connection.close()
        self.assertEqual(len(self.owner.errors), 1)
        self.assertIn("labelled-owner-fixture-failure", self.owner.errors[0])
        with self.assertRaises(AssertionError):
            self.assertEqual(self.owner.errors, [])
        self.expected_errors["owner"] = self.owner.errors.copy()


if __name__ == "__main__":
    unittest.main()

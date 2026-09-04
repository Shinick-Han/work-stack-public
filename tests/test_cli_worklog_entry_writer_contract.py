"""Actual ordinary Worklog-entry CLI/owner contract.

Reuses the admitted checkin test's owned server/relay/cleanup fixture unchanged;
the public invocation, domain parity and response/failure assertions are entry-specific.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import shutil
import types
import unittest
from unittest import mock

import test_cli_worklog_checkin_writer_contract as checkin_fixture

PATH = "/api/v1/cli/worklog/add"
DAY = "2026-09-03"
FIELDS = ["date", "task_id", "task", "done", "next", "blockers"]


class _EntryCase(checkin_fixture._Case):
    def setUp(self):
        super().setUp()
        self.task = self.stack.add_task("Actual e\u0301  한글\t title")

    def body(self, **changes):
        body = {"task_id": self.task["id"], "date": DAY,
                "done": [" did "], "next_items": [], "blockers": []}
        body.update(changes)
        return body

    def run_entry(self, task, *args, root=None):
        from workstack import cli
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as contexts:
            contexts.enter_context(contextlib.redirect_stdout(stdout))
            contexts.enter_context(contextlib.redirect_stderr(stderr))
            if self.owner is not None and root is None:
                contexts.enter_context(mock.patch.object(cli, "WorkStack", side_effect=AssertionError("local fallback")))
            code = cli.main(["--data-dir", str(root or self.root), "worklog", "add", task, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke_body(self, body, root=None):
        args = ["--date", body["date"]]
        for field, flag in (("done", "--done"), ("next_items", "--next"), ("blockers", "--blocker")):
            for item in body[field]:
                args.extend((flag, item))
        return self.run_entry(body["task_id"], *args, root=root)

    def invoke(self):
        return self.invoke_body(self.body())

    def assert_success(self, result):
        self.assertEqual(result[0], 0, result[2])
        self.assertEqual(result[2], "")
        data = json.loads(result[1])
        self.assertEqual(list(data), FIELDS)
        self.assertEqual(result[1], json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return data

    def assert_unknown(self, result):
        self.assertEqual(result[:2], (2, ""))
        self.assertIn("worklog entry commit is unknown", result[2])

    def wire(self, body=None, **kwargs):
        kwargs.setdefault("route", PATH)
        return super().wire(self.body() if body is None else body, **kwargs)

    def clone(self):
        local = self.home / f"local-{self.local_number}"
        self.local_number += 1
        shutil.copytree(self.root, local, ignore=shutil.ignore_patterns(".workstack.lock"))
        return local

    def worklog(self):
        return json.loads(self.snapshot()["worklog.json"])

    def seed_history(self):
        from workstack.storage.document_repository import WorkspaceDocument
        document = self.worklog()
        document["days"] = {
            DAY: {"start_time": "08:15", "extra": {"nfd": "e\u0301"}, "entries": [
                {"task_id": "legacy-id", "opaque": [True, 1]}, 17]},
            "2026-09-01": {"start_time": "07:00", "entries": [{"old": "evidence"}]},
        }
        self.stack.documents.save(WorkspaceDocument.WORKLOG, document)

    def set_day(self, value):
        from workstack.storage.document_repository import WorkspaceDocument
        document = self.worklog()
        document["days"][DAY] = value
        self.stack.documents.save(WorkspaceDocument.WORKLOG, document)


class EntryParity(_EntryCase):
    def test_absent_owner_still_appends_locally(self):
        data = self.assert_success(self.invoke())
        self.assertEqual(data["task_id"], self.task["id"])
        self.assertEqual(data["task"], self.task["title"])
        self.assertEqual(data["done"], ["did"])
        self.assertFalse(self.store.server_info_path.exists())

    def test_local_owner_bytes_match_for_legacy_dates_and_categories(self):
        self.seed_history()
        self.start_owner()
        vectors = [
            self.body(), self.body(date="20260903"), self.body(date="2026-W36-4"),
            self.body(task_id="  t-0001  ", done=[" ", " e\u0301 ", "한 글", "e\u0301", " A  B ", "\t"],
                      next_items=[" next ", "next"], blockers=[" 막힘 "]),
            self.body(done=["item " + str(i) for i in range(25)] + ["x" * 1501]),
        ]
        for body in vectors:
            with self.subTest(date=body["date"], count=len(body["done"])):
                self._compare(body)
        days = self.worklog()["days"]
        self.assertTrue({DAY, "20260903", "2026-W36-4"} <= set(days))
        self.assertEqual(days[DAY]["entries"][:2], [{"task_id": "legacy-id", "opaque": [True, 1]}, 17])
        self.assertEqual(days[DAY]["start_time"], "08:15")
        self.assertEqual(days[DAY]["extra"], {"nfd": "e\u0301"})
        self.assertEqual(days["2026-09-01"], {"start_time": "07:00", "entries": [{"old": "evidence"}]})

    def _compare(self, body):
        from workstack.storage.document_repository import WorkspaceDocument
        local = self.clone()
        before = self.snapshot()
        expected = self.invoke_body(body, root=local)
        generation = self.store.generation
        with mock.patch.object(self.stack, "add_worklog", wraps=self.stack.add_worklog) as setter, mock.patch.object(
            self.stack.documents, "save", wraps=self.stack.documents.save
        ) as save:
            actual = self.invoke_body(body)
        data = self.assert_success(actual)
        self.assertEqual(actual, expected)
        self.assertEqual(self.snapshot(), self.snapshot(local))
        setter.assert_called_once_with(**body)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(save.call_args.args[0], WorkspaceDocument.WORKLOG)
        self.assertEqual(self.store.generation, generation + 1)
        self.assert_only_worklog_changed(before)
        self.assertEqual(data["task"], self.task["title"])
        self.assertEqual(json.loads(self.posts()[-1][2]), body)

    def test_none_and_empty_date_defaults_match_local_and_are_frozen(self):
        from workstack import cli_writer, service
        for args in [("--done", "done"), ("--date", "", "--done", "done")]:
            local = self.clone()
            with mock.patch.object(service, "today", return_value=DAY):
                expected = self.run_entry(self.task["id"], *args, root=local)
            if self.owner is None:
                self.start_owner()
            clock = types.SimpleNamespace(date=checkin_fixture._Date)
            with mock.patch.object(cli_writer, "datetime", clock), mock.patch.object(
                service, "today", side_effect=AssertionError("remote default")
            ):
                actual = self.run_entry(self.task["id"], *args)
            self.assertEqual(actual, expected)
            self.assertEqual(json.loads(self.posts()[-1][2])["date"], DAY)
            self.assertEqual(self.snapshot(), self.snapshot(local))

    def test_task_first_date_second_empty_categories_last(self):
        self.start_owner()
        cases = [(self.body(task_id="missing", date="bad", done=[]), "unknown task"),
                 (self.body(date="bad", done=[]), "Invalid isoformat"),
                 (self.body(done=[" ", "\t"], next_items=[""], blockers=["\n"]), "at least one worklog item")]
        for body, message in cases:
            with self.subTest(message=message):
                local = self.clone()
                before = self.snapshot()
                expected = self.invoke_body(body, root=local)
                actual = self.invoke_body(body)
                self.assertEqual(expected[:2], (2, ""))
                self.assertIn(message, expected[2])
                self.assertEqual(actual[:2], (2, ""))
                self.assertIn(message, self.relay.responses[-1][1]["error"]["message"])
                self.assertEqual(before, self.snapshot())
                self.assertEqual(before, self.snapshot(local))

    def test_missing_and_malformed_entry_containers_keep_local_failure(self):
        self.start_owner()
        for value, kind in [({"extra": "missing"}, KeyError), ({"entries": None}, AttributeError),
                            ({"entries": {}}, AttributeError), (None, TypeError)]:
            with self.subTest(value=value):
                self._compare_container_failure(value, kind)

    def _compare_container_failure(self, value, kind):
        self.set_day(value)
        local = self.clone()
        before = self.snapshot()
        with self.assertRaises(kind):
            self.invoke_body(self.body(), root=local)
        owner_errors, relay_errors = len(self.owner.errors), len(self.relay.errors)
        posts = len(self.posts())
        self.assert_unknown(self.invoke())
        self.assertEqual(len(self.posts()), posts + 1)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(before, self.snapshot(local))
        self.assertEqual(len(self.owner.errors), owner_errors + 1)
        self.assertIn(kind.__name__, self.owner.errors[-1])
        self.assertEqual(len(self.relay.errors), relay_errors + 1)
        self.assertIn("RemoteDisconnected", self.relay.errors[-1])
        self.expected_errors["owner"] = self.owner.errors.copy()
        self.expected_errors["relay"] = self.relay.errors.copy()

    def test_two_invocations_append_twice_without_receipt_or_fact(self):
        relay = self.start_owner()
        before = self.snapshot()
        for _ in range(2):
            self.assert_success(self.invoke())
        self.assertEqual(len(self.worklog()["days"][DAY]["entries"]), 2)
        self.assertEqual([r[1] for r in relay.requests], (checkin_fixture.PREFLIGHT + [PATH]) * 2)
        self.assert_only_worklog_changed(before)
        for method, path, raw, headers in self.posts():
            self.assertEqual((method, path, json.loads(raw)), ("POST", PATH, self.body()))
            self.assertNotIn("idempotency-key", {name.lower() for name in headers})
            self.assertNotIn("x-workstack-client", {name.lower() for name in headers})
            self.assertEqual(headers["Origin"], f"http://127.0.0.1:{relay.server_address[1]}")
            self.assertEqual(headers["X-WorkStack-CSRF"], self.owner.csrf_token)
        self.assertTrue(all(status == 200 and list(payload) == ["data"] for status, payload in relay.responses))

    def test_title_comes_from_current_owner_without_task_get(self):
        relay = self.start_owner()
        updated = "Changed e\u0301  한글 title"
        relay.before_post = lambda: self.stack.patch_task(self.task["id"], {"title": updated, "revision": 0})
        data = self.assert_success(self.invoke())
        self.assertEqual(data["task"], updated)
        self.assertEqual(self.worklog()["days"][DAY]["entries"][-1]["task"], updated)
        self.assertEqual([r[1] for r in relay.requests], checkin_fixture.PREFLIGHT + [PATH])

    def test_existing_checkpoint_facts_and_populated_okr_survive(self):
        objective = self.stack.add_objective("Existing objective")
        self.stack.add_key_result(objective["id"], "Existing outcome")
        self.stack.link_task(objective["id"], self.task["id"])
        self.stack.add_worklog_v1(
            {"task_id": self.task["id"], "date": DAY, "done": ["existing checkpoint"],
             "next": [], "blockers": []}, "prior-entry-0001",
        )
        before = self.snapshot()
        activity = json.loads(before["activity.json"])
        facts = [item for item in activity["activity"] if item["type"] == "worklog.recorded"]
        self.assertEqual(len(facts), 1)
        self.assertIn("entry_digest", facts[0]["details"])
        self.assertIn("ordinal", facts[0]["details"])
        prior_entry = self.worklog()["days"][DAY]["entries"][0]
        self.start_owner()
        self._compare(self.body(done=[" ordinary followup "]))
        self.assertEqual(self.worklog()["days"][DAY]["entries"][0], prior_entry)
        self.assert_only_worklog_changed(before)
        refused_before = self.snapshot()
        self.assertEqual(self.invoke_body(self.body(date="bad", done=[]))[:2], (2, ""))
        self.assertEqual(self.snapshot(), refused_before)


class EntryFraming(_EntryCase):
    def test_http_exact_fields_types_and_security(self):
        self.start_owner()
        cases = [({}, None, (), 400), ([], None, (), 400), (self.body(), b"{", (), 400),
                 (self.body(), b"\xff", (), 400), (self.body(extra=True), None, (), 400),
                 (self.body(task_id=1), None, (), 400), (self.body(date=None), None, (), 400),
                 (self.body(done="bad"), None, (), 400), (self.body(next_items=[True]), None, (), 400),
                 (self.body(blockers=[{}]), None, (), 400),
                 (self.body(), None, (("Origin", "http://evil.invalid"),), 403),
                 (self.body(), None, (("X-WorkStack-CSRF", "bad"),), 403),
                 (self.body(), None, (("Host", "evil.invalid"),), 400)]
        for body, raw, headers, expected in cases:
            with self.subTest(body=body, headers=headers), mock.patch.object(self.stack, "add_worklog", wraps=self.stack.add_worklog) as setter:
                before = self.snapshot()
                self.assertEqual(self.wire(body, raw=raw, headers=headers)[0], expected)
                setter.assert_not_called()
                self.assertEqual(before, self.snapshot())

    def test_any_key_presence_and_body_limit_refuse_before_setter(self):
        self.start_owner()
        keys = [(('Idempotency-Key', ''),), (('Idempotency-Key', 'ordinary-key-123'),),
                (('idempotency-key', 'one'), ('IDEMPOTENCY-KEY', 'two'))]
        for headers in keys:
            with self.subTest(headers=headers), mock.patch.object(self.stack, "add_worklog") as setter:
                before = self.snapshot()
                status, payload = self.wire(headers=headers)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "unsupported_idempotency_key")
                setter.assert_not_called()
                self.assertEqual(before, self.snapshot())
        with mock.patch.object(self.stack, "add_worklog") as setter:
            before = self.snapshot()
            self.assertEqual(self.wire(raw=b"", length=1048577)[0], 413)
            setter.assert_not_called()
            self.assertEqual(before, self.snapshot())

    def test_direct_service_requires_plain_shapes(self):
        from workstack.service import DomainError
        class DictSubclass(dict):
            pass
        class ListSubclass(list):
            pass
        class StringSubclass(str):
            pass
        cases = [None, [], {}, DictSubclass(self.body()), self.body(done=ListSubclass(["item"])),
                 self.body(task_id=StringSubclass(self.task["id"])), self.body(done=[StringSubclass("item")]),
                 self.body(next_items=[1]), self.body(blockers=None)]
        for body in cases:
            with self.subTest(body=body), mock.patch.object(self.stack, "add_worklog") as setter:
                before = self.snapshot()
                with self.assertRaises(DomainError):
                    self.stack.add_worklog_cli(body)
                setter.assert_not_called()
                self.assertEqual(before, self.snapshot())

    def test_cross_store_and_injected_repositories_refuse_before_document_calls(self):
        from workstack.service import DomainError, WorkStack
        from workstack.store import Store
        from workstack.storage.document_repository import StoreDocumentRepository
        other = WorkStack(Store(self.home / "other"))
        repositories = [StoreDocumentRepository(other.store), mock.Mock(wraps=self.stack.documents)]
        self.start_owner()
        for repository in repositories:
            with self.subTest(repository=type(repository).__name__), mock.patch.object(self.stack, "documents", repository), mock.patch.object(
                repository, "load", side_effect=AssertionError("unadmitted read")
            ), mock.patch.object(self.stack, "add_worklog") as setter:
                before = self.snapshot(), self.snapshot(other.store.root)
                with self.assertRaises(DomainError):
                    self.stack.add_worklog_cli(self.body())
                self.assertEqual(self.wire()[0], 400)
                setter.assert_not_called()
                self.assertEqual(before, (self.snapshot(), self.snapshot(other.store.root)))

    def test_every_alternate_delegate_refuses_before_document_or_backend_call(self):
        from workstack.service import DomainError
        names = ["capture_reply_commands", "intent_commands", "objective_commands", "task_commands",
                 "relationship_commands", "planning_commands", "work_session_commands", "query_commands"]
        for name in names:
            backend = mock.Mock()
            with self.subTest(name=name), mock.patch.object(self.stack, name, backend), mock.patch.object(
                self.stack.documents, "load", side_effect=AssertionError("document read")
            ), mock.patch.object(self.stack, "add_worklog") as setter:
                with self.assertRaises(DomainError):
                    self.stack.add_worklog_cli(self.body())
                setter.assert_not_called()
                self.assertEqual(backend.mock_calls, [])

    def test_real_v4_factory_refuses_before_calls(self):
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
        conversion = convert_v3_documents({name: self.store.load(name) for name in DEFAULTS}, candidate_created_at=now)
        authority = self.home / "v"
        checkin_fixture.CheckinFraming._write_conversion(self, authority, conversion)
        runtime = resolve_runtime_authority(authority, self.home / "r", str(conversion.store["workspace_uid"]))
        runtime.runtime_root.mkdir(parents=True)
        publish_runtime_manifest(runtime.manifest_path, build_v4_manifest(read_v4(authority), generation=0), expected_digest=None)
        runtime.idempotency_path.write_bytes(canonical_json_bytes(dict(conversion.idempotency_ledger)))
        app = create_experimental_v4_application(authority, runtime, enable_v4_application=True, checkpoint_facts=build_checkpoint_facts,
            clock=lambda: now, uid_factory=lambda: "11111111-1111-4111-8111-111111111111", today=lambda: DAY,
            task_note_source_indexes=conversion.task_note_source_indexes)
        stack = WorkStack(app.store)
        before = checkin_fixture.CheckinFraming._files(self, authority)
        runtime_before = checkin_fixture.CheckinFraming._files(self, runtime.runtime_root)
        with mock.patch.object(stack.documents, "load", side_effect=AssertionError("v4 read")), mock.patch.object(stack, "add_worklog") as setter:
            with self.assertRaises(DomainError):
                stack.add_worklog_cli(self.body())
            setter.assert_not_called()
        self.assertEqual(before, checkin_fixture.CheckinFraming._files(self, authority))
        self.assertEqual(runtime_before, checkin_fixture.CheckinFraming._files(self, runtime.runtime_root))

    def test_strict_gui_entry_keeps_bounds_fact_and_receipt(self):
        self.start_owner()
        route = "/api/v1/review/entries"
        headers = (("Idempotency-Key", "gui-entry-12345"),)
        body = {"task_id": self.task["id"], "date": DAY, "done": ["did"], "next": [], "blockers": []}
        for patch in [{"done": ["a"] * 21}, {"done": ["x" * 1001]}, {"date": "20260903"}]:
            with self.subTest(patch=patch):
                before = self.snapshot()
                self.assertEqual(self.wire({**body, **patch}, route=route, headers=headers)[0], 400)
                self.assertEqual(before, self.snapshot())
        first = self.wire(body, route=route, headers=headers)
        replay = self.wire(body, route=route, headers=headers)
        self.assertEqual((first[0], first[1]["meta"]), (201, {"replayed": False}))
        self.assertEqual((replay[0], replay[1]["meta"]), (200, {"replayed": True}))
        activity = json.loads(self.snapshot()["activity.json"])
        self.assertEqual(len(activity["idempotency"]), 1)
        self.assertEqual(sum(item["type"] == "worklog.recorded" for item in activity["activity"]), 1)
        self.assertEqual(len(self.worklog()["days"][DAY]["entries"]), 1)


class EntryOwnerTransport(_EntryCase):
    def test_commit_then_disconnect_has_one_append_and_next_invocation_is_new(self):
        relay = self.start_owner()
        before = self.snapshot()
        relay.drop = True
        self.assert_unknown(self.invoke())
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual([r[1] for r in relay.requests], checkin_fixture.PREFLIGHT + [PATH])
        self.assertEqual(len(self.worklog()["days"][DAY]["entries"]), 1)
        self.assert_only_worklog_changed(before)
        relay.drop = False
        self.assert_success(self.invoke())
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(len(self.worklog()["days"][DAY]["entries"]), 2)
        self.assert_only_worklog_changed(before)

    def test_genuine_success_corruptions_report_unknown_without_retry(self):
        relay = self.start_owner()
        variants = [lambda status, p: (201, p), lambda status, p: (200, {}), lambda status, p: (200, []),
                    lambda status, p: (200, {**p, "meta": {}}), lambda status, p: (200, {"data": None}),
                    lambda status, p: (200, {"data": dict(reversed(list(p["data"].items())))}),
                    lambda status, p: (200, {"data": {k: v for k, v in p["data"].items() if k != "next"}})]
        fields = [("date", "2026-09-04"), ("task_id", "T-9999"), ("task", 1), ("done", ["changed"]),
                  ("next", ["added"]), ("blockers", None), ("done", [True]), ("date", 20260903),
                  ("task_id", 1), ("done", "did"), ("extra", True)]
        variants.extend(lambda status, p, f=f, v=v: (200, {"data": {**p["data"], f: v}}) for f, v in fields)
        for transform in variants:
            before = self.snapshot()
            count = len(self.posts())
            relay.transform = transform
            self.assert_unknown(self.invoke())
            self.assertEqual(len(self.posts()), count + 1)
            self.assertEqual(relay.responses[-1][0], 200)
            self.assertEqual(list(relay.responses[-1][1]["data"]), FIELDS)
            self.assertEqual(len(self.worklog()["days"][DAY]["entries"]), count + 1)
            self.assert_only_worklog_changed(before)

    def test_malformed_json_after_real_commit_is_unknown(self):
        relay = self.start_owner()
        before = self.snapshot()
        relay.raw_success = b'{"data":'
        self.assert_unknown(self.invoke())
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(self.worklog()["days"][DAY]["entries"]), 1)
        self.assert_only_worklog_changed(before)

    def test_real_400_403_409_refusals_are_final_and_sanitized(self):
        relay = self.start_owner()
        before = self.snapshot()
        result = self.invoke_body(self.body(done=[]))
        self.assertEqual(result[:2], (2, ""))
        self.assertIn("HTTP 400", result[2])
        self.assertNotIn("at least one", result[2])
        self.assertEqual(before, self.snapshot())
        relay.get_hooks[checkin_fixture.PREFLIGHT[-1]] = lambda p: setattr(self.owner, "csrf_token", "changed")
        self.assertIn("HTTP 403", self.invoke()[2])
        self.assertEqual(before, self.snapshot())
        relay.get_hooks.clear()
        relay.before_post = lambda: (self.root / "worklog.json").write_bytes(before["worklog.json"] + b"\n")
        self.assertIn("HTTP 409", self.invoke()[2])
        self.assertEqual(self.snapshot()["worklog.json"], before["worklog.json"] + b"\n")
        self.assert_only_worklog_changed(before)
        self.assertEqual([status for status, _ in relay.responses], [400, 403, 409])
        self.assertEqual(len(self.posts()), 3)

    def test_invalid_advertisements_and_healthy_control(self):
        checkin_fixture.CheckinOwnerTransport.test_invalid_owner_advertisements_never_initialize_locally(self)

    def test_scoped_binary_eacces_and_same_owner_control(self):
        checkin_fixture.CheckinOwnerTransport.test_scoped_permission_denial_then_same_owner_control(self)

    def test_foreign_workspace_and_sync_refuse_before_post(self):
        checkin_fixture.CheckinOwnerTransport.test_foreign_identity_and_not_in_sync_refuse(self)

    def test_final_binding_changes_and_real_idle_post_control(self):
        checkin_fixture.CheckinOwnerTransport.test_final_binding_removed_or_replaced_has_zero_wrong_owner_contacts(self)

    def _change_binding(self, replace, idle):
        checkin_fixture.CheckinOwnerTransport._change_binding(self, replace, idle)

    def _assert_binding(self, replaced, idle):
        checkin_fixture.CheckinOwnerTransport._assert_binding(self, replaced, idle)

    def test_actual_owner_handler_error_sink_is_load_bearing(self):
        checkin_fixture.CheckinOwnerTransport.test_real_owner_handler_error_sink_is_load_bearing(self)


if __name__ == "__main__":
    unittest.main()

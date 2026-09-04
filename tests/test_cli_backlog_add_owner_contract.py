"""Actual legacy backlog create contract and testless fixtures for the CLI tail.

The owned HTTP relay/lifecycle and metadata controls reuse the admitted checkin
fixture unchanged. No collected TestCase is imported or inherited.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import unittest
from unittest import mock

import test_cli_worklog_checkin_writer_contract as owner_fixture

DAY = "2026-09-03"
PATH = "/api/v1/cli/backlog/add"
FIELDS = ["id", "uid", "title", "detail", "status", "priority", "due", "scheduled",
          "estimate_minutes", "tags", "objective_ids", "parent_id", "dependencies",
          "subtasks", "notes", "created", "updated_at", "revision", "status_fact_id"]


class _TailCase(owner_fixture._Case):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch("workstack.service.today", return_value=DAY))
        self.enterContext(mock.patch("workstack.service.utc_now", return_value=DAY + "T10:00:00Z"))

    def invoke_body(self, body, root=None):
        return self.invoke_args(self.argv(body), root=root)

    def invoke_args(self, args, root=None):
        from workstack import cli
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as contexts:
            contexts.enter_context(contextlib.redirect_stdout(stdout))
            contexts.enter_context(contextlib.redirect_stderr(stderr))
            if self.owner is not None and root is None:
                contexts.enter_context(mock.patch.object(cli, "WorkStack", side_effect=AssertionError("local fallback")))
            code = cli.main(["--data-dir", str(root or self.root), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def invoke(self):
        return self.invoke_body(self.body())

    def assert_success(self, result):
        self.assertEqual(result[0], 0, result[2])
        self.assertEqual(result[2], "")
        data = json.loads(result[1])
        self.assertIs(type(data), dict)
        self.assertEqual(result[1], json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return data

    def assert_unknown(self, result):
        self.assertEqual(result[:2], (2, ""))
        self.assertIn(self.operation + " commit is unknown", result[2])

    def wire(self, body=None, **kwargs):
        kwargs.setdefault("route", self.path)
        return super().wire(self.body() if body is None else body, **kwargs)

    def clone(self):
        root = self.home / f"local-{self.local_number}"
        self.local_number += 1
        shutil.copytree(self.root, root, ignore=shutil.ignore_patterns(".workstack.lock"))
        return root

    def document(self, name):
        return json.loads(self.snapshot()[name])

    def unchanged_except(self, before, changed):
        after = self.snapshot()
        self.assertEqual({k: v for k, v in before.items() if k not in changed},
                         {k: v for k, v in after.items() if k not in changed})

    def assert_wire(self, body, start):
        requests = self.relay.requests[start:]
        self.assertEqual([(r[0], r[1]) for r in requests],
                         [("GET", path) for path in owner_fixture.PREFLIGHT] + [("POST", self.path)])
        self.assertEqual(json.loads(requests[-1][2]), body)
        headers = {k.lower(): v for k, v in requests[-1][3].items()}
        self.assertNotIn("idempotency-key", headers)
        self.assertFalse(any("agent" in key or "attribution" in key for key in headers))
        self.assertNotIn("authorization", headers)
        self.assertIn("origin", headers)
        self.assertEqual(headers["x-workstack-csrf"], self.owner.csrf_token)

    def compare(self, body):
        local = self.clone()
        before = self.snapshot()
        expected = self.invoke_body(body, root=local)
        start = len(self.relay.requests)
        generation = self.store.generation
        setter = getattr(self.stack, self.setter)
        with mock.patch.object(self.stack, self.setter, wraps=setter) as call, mock.patch.object(
            self.stack.documents, self.save_method, wraps=getattr(self.stack.documents, self.save_method)
        ) as save:
            actual = self.invoke_body(body)
        data = self.assert_success(actual)
        self.assertEqual(actual, expected)
        self.assertEqual(self.snapshot(), self.snapshot(local))
        call.assert_called_once_with(**body)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(self.store.generation, generation + 1)
        self.assert_save(save)
        self.unchanged_except(before, self.changed)
        self.assert_wire(body, start)
        return data, before

    def check_http_frames(self, invalid):
        self.start_owner()
        cases = [(body, None, (), 400) for body in [{}, [], self.body(extra=True), *invalid]]
        cases.extend([(self.body(), b"{", (), 400), (self.body(), b"\xff", (), 400),
                      (self.body(), None, (("Origin", "http://evil.invalid"),), 403),
                      (self.body(), None, (("X-WorkStack-CSRF", "wrong"),), 403),
                      (self.body(), None, (("Host", "evil.invalid"),), 400)])
        for body, raw, headers, expected in cases:
            with self.subTest(body=body, headers=headers), mock.patch.object(self.stack, self.setter) as setter:
                before = self.snapshot()
                self.assertEqual(self.wire(body, raw=raw, headers=headers)[0], expected)
                setter.assert_not_called()
                self.assertEqual(before, self.snapshot())

    def check_headers_and_limit(self):
        self.start_owner()
        keys = [(('Idempotency-Key', ''),), (('Idempotency-Key', 'ordinary-key-123'),),
                (('idempotency-key', 'one'), ('IDEMPOTENCY-KEY', 'two'))]
        with mock.patch.object(self.stack, self.setter) as setter:
            before = self.snapshot()
            for headers in keys:
                status, payload = self.wire(headers=headers)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "unsupported_idempotency_key")
            self.assertEqual(self.wire(raw=b"", length=1048577)[0], 413)
            setter.assert_not_called()
            self.assertEqual(before, self.snapshot())

    def check_direct_frames(self, invalid):
        from workstack.service import DomainError
        class DictSubclass(dict):
            pass
        for body in [None, [], {}, DictSubclass(self.body()), *invalid]:
            with self.subTest(body=body), mock.patch.object(self.stack, self.setter) as setter:
                before = self.snapshot()
                with self.assertRaises(DomainError):
                    getattr(self.stack, self.frame)(body)
                setter.assert_not_called()
                self.assertEqual(before, self.snapshot())

    def check_repository_composition(self):
        from workstack.service import DomainError, WorkStack
        from workstack.store import Store
        from workstack.storage.document_repository import StoreDocumentRepository
        other = WorkStack(Store(self.home / "other"))
        repositories = [StoreDocumentRepository(other.store), mock.Mock(wraps=self.stack.documents)]
        self.start_owner()
        for repository in repositories:
            with mock.patch.object(self.stack, "documents", repository), mock.patch.object(
                repository, "load", side_effect=AssertionError("unadmitted read")
            ), mock.patch.object(self.stack, self.setter) as setter:
                before = self.snapshot(), self.snapshot(other.store.root)
                with self.assertRaises(DomainError):
                    getattr(self.stack, self.frame)(self.body())
                self.assertEqual(self.wire()[0], 400)
                setter.assert_not_called()
                self.assertEqual(before, (self.snapshot(), self.snapshot(other.store.root)))

    def check_all_delegates(self):
        from workstack.service import DomainError
        names = ["capture_reply_commands", "intent_commands", "objective_commands", "task_commands",
                 "relationship_commands", "planning_commands", "work_session_commands", "query_commands"]
        for name in names:
            backend = mock.Mock()
            with self.subTest(name=name), mock.patch.object(self.stack, name, backend), mock.patch.object(
                self.stack.documents, "load", side_effect=AssertionError("document read")
            ), mock.patch.object(self.stack, self.setter) as setter:
                with self.assertRaises(DomainError):
                    getattr(self.stack, self.frame)(self.body())
                setter.assert_not_called()
                self.assertEqual(backend.mock_calls, [])

    def check_actual_v4(self):
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
        now = DAY + "T00:00:00Z"
        conversion = convert_v3_documents({name: self.store.load(name) for name in DEFAULTS}, candidate_created_at=now)
        authority = self.home / "v"
        owner_fixture.CheckinFraming._write_conversion(self, authority, conversion)
        runtime = resolve_runtime_authority(authority, self.home / "r", str(conversion.store["workspace_uid"]))
        runtime.runtime_root.mkdir(parents=True)
        publish_runtime_manifest(runtime.manifest_path, build_v4_manifest(read_v4(authority), generation=0), expected_digest=None)
        runtime.idempotency_path.write_bytes(canonical_json_bytes(dict(conversion.idempotency_ledger)))
        app = create_experimental_v4_application(authority, runtime, enable_v4_application=True, checkpoint_facts=build_checkpoint_facts,
            clock=lambda: now, uid_factory=lambda: "11111111-1111-4111-8111-111111111111", today=lambda: DAY,
            task_note_source_indexes=conversion.task_note_source_indexes)
        stack = WorkStack(app.store)
        before = owner_fixture.CheckinFraming._files(self, authority)
        runtime_before = owner_fixture.CheckinFraming._files(self, runtime.runtime_root)
        with mock.patch.object(stack.documents, "load", side_effect=AssertionError("v4 read")), mock.patch.object(stack, self.setter) as setter:
            with self.assertRaises(DomainError):
                getattr(stack, self.frame)(self.body())
            setter.assert_not_called()
        self.assertEqual(before, owner_fixture.CheckinFraming._files(self, authority))
        self.assertEqual(runtime_before, owner_fixture.CheckinFraming._files(self, runtime.runtime_root))

    def check_dropped_commit(self):
        relay = self.start_owner()
        before = self.effect_count()
        relay.drop = True
        self.assert_unknown(self.invoke())
        self.assertEqual(self.effect_count(), before + 1)
        self.assertEqual(len(self.posts()), 1)
        self.assert_wire(self.body(), 0)
        relay.drop = False
        self.assert_success(self.invoke())
        self.assertEqual(self.effect_count(), before + 2)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(self.document("activity.json")["idempotency"], [])

    def check_corrupted_success(self, fields):
        relay = self.start_owner()
        variants = [lambda s, p: (201, p), lambda s, p: (202, p), lambda s, p: (200, {}),
                    lambda s, p: (200, []), lambda s, p: (200, {**p, "meta": {}}),
                    lambda s, p: (200, {"data": None})]
        variants.extend(lambda s, p, f=f, v=v: (200, {"data": {**p["data"], f: v}}) for f, v in fields)
        for transform in variants:
            before, start = self.effect_count(), len(relay.requests)
            relay.transform = transform
            self.assert_unknown(self.invoke())
            self.assertEqual(relay.responses[-1][0], 200)
            self.assertEqual(self.effect_count(), before + 1)
            self.assert_wire(self.body(), start)
        relay.transform = None
        relay.raw_success = b'{"data":'
        before, start = self.effect_count(), len(relay.requests)
        self.assert_unknown(self.invoke())
        self.assertEqual(self.effect_count(), before + 1)
        self.assert_wire(self.body(), start)

    def check_determinate_refusals(self, invalid):
        relay = self.start_owner()
        before = self.snapshot()
        refused = self.invoke_body(invalid)
        self.assertEqual(refused[:2], (2, ""))
        self.assertIn("HTTP 400", refused[2])
        self.assertNotIn("unknown ", refused[2])
        self.assertEqual(before, self.snapshot())
        relay.get_hooks[owner_fixture.PREFLIGHT[-1]] = lambda p: setattr(self.owner, "csrf_token", "rotated")
        self.assertIn("HTTP 403", self.invoke()[2])
        self.assertEqual(before, self.snapshot())
        relay.get_hooks.clear()
        relay.before_post = lambda: (self.root / "worklog.json").write_bytes(before["worklog.json"] + b"\n")
        self.assertIn("HTTP 409", self.invoke()[2])
        self.assertEqual(self.snapshot()["worklog.json"], before["worklog.json"] + b"\n")
        self.unchanged_except(before, {"worklog.json"})
        self.assertEqual([status for status, _ in relay.responses], [400, 403, 409])
        self.assertEqual(len(self.posts()), 3)

    def _change_binding(self, replace, idle):
        owner_fixture.CheckinOwnerTransport._change_binding(self, replace, idle)

    def _assert_binding(self, replaced, idle):
        owner_fixture.CheckinOwnerTransport._assert_binding(self, replaced, idle)


class _BacklogCase(_TailCase):
    path, operation = PATH, "backlog add"
    setter, frame, save_method = "add_task", "add_task_cli", "save_many"
    changed = {"backlog.json", "activity.json"}

    def setUp(self):
        super().setUp()
        self.objective = self.stack.add_objective("Actual Objective e\u0301 한글", "2026-Q3")
        self.parent = self.stack.add_task("Actual parent")
        self.dependency = self.stack.add_task("Actual dependency")
        self.stack.set_task_status(self.parent["id"], "started")

    def body(self, **changes):
        body = {"title": " Actual e\u0301  한글\t title ", "detail": " detail\n한글 ", "priority": "P2",
                "due": None, "tags": [], "objective_ids": [], "parent_id": None, "dependencies": []}
        body.update(changes)
        return body

    def argv(self, body):
        args = ["backlog", "add", body["title"], "--detail", body["detail"], "--priority", body["priority"]]
        for field, flag in (("due", "--due"), ("parent_id", "--parent")):
            if body[field] is not None:
                args.extend((flag, body[field]))
        for field, flag in (("tags", "--tag"), ("objective_ids", "--objective"), ("dependencies", "--depends-on")):
            for item in body[field]:
                args.extend((flag, item))
        return args

    def effect_count(self):
        return len(self.document("backlog.json")["tasks"])

    def assert_save(self, save):
        from workstack.storage.document_repository import WorkspaceDocument
        self.assertEqual(set(save.call_args.args[0]), {WorkspaceDocument.TASKS, WorkspaceDocument.ACTIVITY})


class BacklogParity(_BacklogCase):
    def test_absent_owner_and_parser_defaults(self):
        data = self.assert_success(self.invoke_args(["backlog", "add", "Only required title"]))
        self.assertEqual(list(data), FIELDS)
        self.assertEqual(data["priority"], "P2")
        self.assertEqual(data["status"], "open")
        self.assertEqual(data["revision"], 0)

    def test_all_flags_dates_collections_and_existing_ledger_match(self):
        self.start_owner()
        for due in [None, "", DAY, "20260903", "2026-W36-4"]:
            body = self.body(due=due, priority="P1", tags=[" e\u0301 ", "한글", "", " e\u0301 ", " A  B "],
                objective_ids=[" " + self.objective["id"].lower(), "", self.objective["id"]],
                parent_id=" " + self.parent["id"].lower() + " ",
                dependencies=[self.dependency["id"].lower(), "", self.dependency["id"], "  "])
            data, before = self.compare(body)
            self.assertEqual(list(data), FIELDS)
            self.assert_bootstrap(data, before)

    def assert_bootstrap(self, data, before):
        old = json.loads(before["activity.json"])
        activity = self.document("activity.json")
        self.assertEqual(activity["planning_status"][:-1], old["planning_status"])
        self.assertEqual(activity["activity"], old["activity"])
        self.assertEqual(activity["idempotency"], old["idempotency"])
        fact = activity["planning_status"][-1]
        self.assertEqual((fact["id"], fact["task_id"], fact["task_uid"]), (data["status_fact_id"], data["id"], data["uid"]))
        self.assertEqual((fact["actor"], fact["provenance"], fact["status"], fact["new_revision"]), ("local.user", "cli", "open", 0))
        self.assertIsNone(fact["previous_fact_id"])
        self.assertEqual(self.document("backlog.json")["tasks"][:-1], json.loads(before["backlog.json"])["tasks"])

    def test_all_parser_priorities_match(self):
        self.start_owner()
        for priority in ("P0", "P1", "P2", "P3"):
            self.compare(self.body(priority=priority))

    def test_due_relationship_title_objective_precedence(self):
        self.start_owner()
        cases = [(self.body(title=" ", due="bad", parent_id="T-9999", objective_ids=["O-9999"]), "Invalid isoformat"),
                 (self.body(title=" ", parent_id="T-9999", objective_ids=["O-9999"]), "unknown task ids"),
                 (self.body(title=" ", dependencies=["T-9999"], objective_ids=["O-9999"]), "unknown task ids"),
                 (self.body(title=" ", objective_ids=["O-9999"]), "title"),
                 (self.body(objective_ids=["O-9999"]), "unknown objective ids")]
        for body, diagnostic in cases:
            local = self.clone()
            before = self.snapshot()
            expected = self.invoke_body(body, root=local)
            actual = self.invoke_body(body)
            self.assertEqual(expected[:2], (2, ""))
            self.assertIn(diagnostic, expected[2])
            self.assertEqual(actual[:2], (2, ""))
            self.assertIn("HTTP 400", actual[2])
            self.assertEqual(self.relay.responses[-1][0], 400)
            self.assertIn(diagnostic, self.relay.responses[-1][1]["error"]["message"])
            self.assertEqual(before, self.snapshot())
            self.assertEqual(before, self.snapshot(local))

    def test_two_explicit_calls_create_two_bootstraps(self):
        self.start_owner()
        first, before = self.compare(self.body())
        self.assert_bootstrap(first, before)
        second, before = self.compare(self.body())
        self.assert_bootstrap(second, before)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["status_fact_id"], second["status_fact_id"])

    def test_owner_calendar_dates_need_not_equal_each_other_or_cli_clock(self):
        self.start_owner()
        with mock.patch("workstack.service.today", side_effect=["2026-09-03", "2026-09-04"]):
            data = self.assert_success(self.invoke())
        self.assertEqual((data["created"], data["updated_at"]), ("2026-09-03", "2026-09-04"))

    def test_atomic_save_failure_has_no_task_or_fact(self):
        self.start_owner()
        before = self.snapshot()
        generation = self.store.generation
        original = self.store._atomic_write_locked
        hits = []

        def fail_journal(path, value):
            if path == self.store.journal_path:
                hits.append(path)
                raise OSError("labelled-tail-journal-write-failure")
            return original(path, value)

        with mock.patch.object(self.store, "_atomic_write_locked", side_effect=fail_journal):
            result = self.invoke()
        self.assertEqual(result[:2], (2, ""))
        self.assertEqual(hits, [self.store.journal_path])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.store.generation, generation)
        self.assertFalse(self.store.journal_path.exists())
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(self.owner.errors), 1)
        self.assertIn("OSError: labelled-tail-journal-write-failure", self.owner.errors[0])
        self.assertEqual(len(self.relay.errors), 1)
        self.assertIn("RemoteDisconnected", self.relay.errors[0])
        self.expected_errors["owner"] = self.owner.errors.copy()
        self.expected_errors["relay"] = self.relay.errors.copy()
        self.assert_success(self.invoke())

    def test_actual_gui_create_keeps_projection_provenance_and_receipt(self):
        self.start_owner()
        headers = (("Idempotency-Key", "gui-create-tail-001"),)
        body = {"title": "GUI control"}
        first = self.wire(body, route="/api/v1/tasks", headers=headers)
        replay = self.wire(body, route="/api/v1/tasks", headers=headers)
        self.assertEqual((first[0], first[1]["meta"]), (201, {"replayed": False}))
        self.assertEqual((replay[0], replay[1]["meta"]), (200, {"replayed": True}))
        self.assertNotIn("status_fact_id", first[1]["data"])
        self.assertIn("context_count", first[1]["data"])
        activity = self.document("activity.json")
        self.assertEqual(activity["planning_status"][-1]["provenance"], "api.v1")
        self.assertEqual(len(activity["idempotency"]), 1)
        self.assertEqual(self.wire({**body, "parent_id": self.parent["id"]}, route="/api/v1/tasks", headers=headers)[0], 400)


class BacklogFraming(_BacklogCase):
    def test_exact_fields_types_and_security(self):
        invalid = [self.body(title=1), self.body(detail=None), self.body(priority=True), self.body(due=1),
                   self.body(parent_id=[]), self.body(tags="bad"), self.body(objective_ids=[True]),
                   self.body(dependencies=[{}])]
        self.check_http_frames(invalid)

    def test_any_key_presence_and_body_limit(self):
        self.check_headers_and_limit()

    def test_direct_plain_shape_before_domain(self):
        class StringSubclass(str):
            pass
        class ListSubclass(list):
            pass
        self.check_direct_frames([self.body(title=StringSubclass("title")), self.body(tags=ListSubclass()),
                                 self.body(tags=[StringSubclass("x")]), self.body(due=False)])

    def test_cross_store_and_injected_repository(self):
        self.check_repository_composition()

    def test_all_eight_alternate_delegates(self):
        self.check_all_delegates()

    def test_actual_v4_composition(self):
        self.check_actual_v4()


class BacklogOwnerTransport(_BacklogCase):
    def test_commit_then_drop_and_explicit_new_action(self):
        self.check_dropped_commit()

    def test_genuine_success_corruptions_and_malformed_json(self):
        fields = [("id", "T-9999"), ("uid", "00000000-0000-0000-0000-000000000000"),
                  ("title", "changed"), ("detail", "changed"), ("priority", "P0"), ("due", DAY),
                  ("status", "done"), ("revision", True), ("scheduled", DAY), ("estimate_minutes", 0),
                  ("tags", [True]), ("objective_ids", ["O-9999"]), ("dependencies", ["T-9999"]),
                  ("parent_id", "T-9999"), ("subtasks", [{}]), ("notes", [{}]), ("created", "bad"),
                  ("updated_at", 1), ("status_fact_id", "fabricated"), ("extra", True)]
        self.check_corrupted_success(fields)

    def test_result_missing_fields_and_wrong_order_after_actual_commit(self):
        relay = self.start_owner()
        transforms = [lambda s, p: (s, {"data": dict(reversed(list(p["data"].items())))}),
                      lambda s, p: (s, {"data": {k: v for k, v in p["data"].items() if k != "status_fact_id"}})]
        for transform in transforms:
            relay.transform = transform
            before = self.effect_count()
            self.assert_unknown(self.invoke())
            self.assertEqual(self.effect_count(), before + 1)
        self.assertEqual(len(self.posts()), 2)

    def test_actual_400_403_409_are_final_and_sanitized(self):
        self.check_determinate_refusals(self.body(parent_id="T-9999"))

    def test_invalid_metadata_and_healthy_control(self):
        owner_fixture.CheckinOwnerTransport.test_invalid_owner_advertisements_never_initialize_locally(self)

    def test_scoped_binary_eacces_and_same_owner_control(self):
        owner_fixture.CheckinOwnerTransport.test_scoped_permission_denial_then_same_owner_control(self)

    def test_foreign_uid_and_not_in_sync(self):
        owner_fixture.CheckinOwnerTransport.test_foreign_identity_and_not_in_sync_refuse(self)

    def test_final_binding_change_and_real_idle_endpoint_control(self):
        owner_fixture.CheckinOwnerTransport.test_final_binding_removed_or_replaced_has_zero_wrong_owner_contacts(self)

    def test_genuine_handler_error_sink(self):
        owner_fixture.CheckinOwnerTransport.test_real_owner_handler_error_sink_is_load_bearing(self)

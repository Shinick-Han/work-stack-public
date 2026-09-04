"""Ordinary OKR link through the real owner, preserving the whole raw Task.

Reuses only the testless tail/checkin fixtures; all collected tests below are
link-specific, including the actual owner metadata and error-sink controls.
"""
from __future__ import annotations

import copy
import json
from unittest import mock

import test_cli_backlog_add_owner_contract as tail

PATH = "/api/v1/cli/okr/link"


class _LinkCase(tail._TailCase):
    path, operation = PATH, "OKR link"
    setter, frame, save_method = "link_task", "link_task_cli", "save"
    changed = {"backlog.json"}

    def setUp(self):
        super().setUp()
        self.objective = self.stack.add_objective("Link target e\u0301 한글", "2026-Q3")
        self.other = self.stack.add_objective("Existing Objective", "2026-Q4")
        self.kr = self.stack.add_key_result(self.other["id"], "Retained KR", "opaque target")
        self.task = self.stack.add_task("Actual title", objective_ids=[self.other["id"]])

    def body(self, **changes):
        body = {"objective_id": self.objective["id"], "task_id": self.task["id"]}
        body.update(changes)
        return body

    def argv(self, body):
        return ["okr", "link", body["objective_id"], body["task_id"]]

    def current_task(self):
        return self.document("backlog.json")["tasks"][0]

    def effect_count(self):
        return self.current_task()["revision"]

    def assert_save(self, save):
        from workstack.storage.document_repository import WorkspaceDocument
        self.assertEqual(save.call_args.args[0], WorkspaceDocument.TASKS)

    def persist_task(self, task):
        from workstack.storage.document_repository import WorkspaceDocument
        document = self.stack.documents.load(WorkspaceDocument.TASKS)
        document["tasks"][0] = task
        with self.store.transaction():
            self.stack.documents.save(WorkspaceDocument.TASKS, document)

    def assert_link_effect(self, data, before):
        prior = json.loads(before["backlog.json"])["tasks"][0]
        self.assertEqual(data["revision"], prior["revision"] + 1)
        self.assertEqual(data["updated_at"], tail.DAY)
        self.assertEqual(data["status_fact_id"], prior["status_fact_id"])
        self.assertEqual(data["objective_ids"], sorted({*prior.get("objective_ids", []), self.objective["id"].upper()}))
        unchanged = set(prior) - {"revision", "updated_at", "objective_ids"}
        self.assertEqual({k: data[k] for k in unchanged}, {k: prior[k] for k in unchanged})
        self.assertEqual(list(data), list(self.current_task()))

    def check_retained_refs(self, alteration):
        from workstack.service import DomainError
        from workstack.storage.document_repository import WorkspaceDocument
        task = self.current_task()
        self.stack.patch_task(task["id"], {"revision": task["revision"], "key_result_refs": [
            {"objective_id": self.other["id"], "key_result_id": self.kr["id"]}]})
        document = self.stack.documents.load(WorkspaceDocument.OBJECTIVES)
        retained = document["objectives"][1]
        alteration(document, retained)
        with self.store.transaction():
            self.stack.documents.save(WorkspaceDocument.OBJECTIVES, document)
        before = self.snapshot()
        with self.assertRaises(DomainError):
            self.stack.patch_task(self.task["id"], {"title": "Generic patch refuses", "revision": self.effect_count()})
        self.assertEqual(before, self.snapshot())
        self.start_owner()
        data, before = self.compare(self.body())
        self.assert_link_effect(data, before)
        self.assertEqual(data["key_result_refs"], [{"objective_id": self.other["id"], "key_result_id": self.kr["id"]}])


class LinkParity(_LinkCase):
    def test_absent_owner_uses_local_link(self):
        before = self.snapshot()
        data = self.assert_success(self.invoke())
        self.assert_link_effect(data, before)
        self.unchanged_except(before, self.changed)

    def test_original_ids_single_save_and_repeated_revision_advance(self):
        self.start_owner()
        body = self.body(objective_id=" " + self.objective["id"].lower() + " ",
                         task_id=" " + self.task["id"].lower() + " ")
        for _ in range(2):
            data, before = self.compare(body)
            self.assert_link_effect(data, before)
        self.assertEqual(self.effect_count(), 2)
        self.assertEqual(self.document("activity.json")["idempotency"], [])

    def test_objective_before_task_and_unknown_ids_are_write_free(self):
        self.start_owner()
        cases = [(self.body(objective_id="O-9999", task_id="T-9999"), "unknown objective"),
                 (self.body(task_id="T-9999"), "unknown task"),
                 (self.body(objective_id=" ", task_id=" "), "unknown objective")]
        for body, message in cases:
            local = self.clone()
            before = self.snapshot()
            expected, actual = self.invoke_body(body, root=local), self.invoke_body(body)
            self.assertEqual(expected[:2], (2, ""))
            self.assertIn(message, expected[2])
            self.assertEqual(actual[:2], (2, ""))
            self.assertIn("HTTP 404", actual[2])
            self.assertIn(message, self.relay.responses[-1][1]["error"]["message"])
            self.assertEqual(before, self.snapshot())
            self.assertEqual(before, self.snapshot(local))

    def test_maximum_revision_success_then_refusal_without_mutation(self):
        from workstack.store import MAX_REVISION
        task = self.current_task()
        task["revision"] = MAX_REVISION - 1
        self.persist_task(task)
        self.start_owner()
        data, before = self.compare(self.body())
        self.assertEqual(data["revision"], MAX_REVISION)
        self.assert_link_effect(data, before)
        local = self.clone()
        before = self.snapshot()
        expected, actual = self.invoke_body(self.body(), root=local), self.invoke()
        self.assertEqual(expected[:2], (2, ""))
        self.assertIn("safe integer limit", expected[2])
        self.assertEqual(actual[:2], (2, ""))
        self.assertIn("HTTP 400", actual[2])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(before, self.snapshot(local))

    def test_raw_extras_order_and_missing_unrelated_optional_fields(self):
        task = self.current_task()
        for key in ("detail", "priority", "due", "scheduled", "estimate_minutes", "tags", "parent_id",
                    "dependencies", "subtasks", "notes", "created"):
            task.pop(key)
        task = {"opaque": {"NFD": "e\u0301", "nested": [None, True, {"x": [3, 2, 1]}]}, **dict(reversed(list(task.items())))}
        self.persist_task(task)
        stored_order = list(self.current_task())
        self.start_owner()
        data, before = self.compare(self.body())
        self.assert_link_effect(data, before)
        self.assertEqual(list(data), stored_order)
        self.assertEqual(data["opaque"], task["opaque"])
        self.assertNotIn("scheduled", data)
        self.assertNotIn("context_count", data)

    def test_legacy_objective_identifier_is_not_restricted_to_gui_grammar(self):
        from workstack.storage.document_repository import WorkspaceDocument
        objectives = self.stack.documents.load(WorkspaceDocument.OBJECTIVES)
        objectives["objectives"][0]["id"] = "LEGACY 한글"
        self.stack.documents.save(WorkspaceDocument.OBJECTIVES, objectives)
        self.objective["id"] = "LEGACY 한글"
        self.start_owner()
        data, before = self.compare(self.body(objective_id=" legacy 한글 "))
        self.assert_link_effect(data, before)

    def test_retained_dangling_refs_keep_local_acceptance(self):
        self.check_retained_refs(lambda document, objective: objective.update(key_results=[]))

    def test_retained_ambiguous_kr_refs_keep_local_acceptance(self):
        self.check_retained_refs(lambda document, objective: objective["key_results"].append(copy.deepcopy(objective["key_results"][0])))

    def test_retained_ambiguous_objective_refs_keep_local_acceptance(self):
        self.check_retained_refs(lambda document, objective: document["objectives"].append(copy.deepcopy(objective)))


class LinkFraming(_LinkCase):
    def test_exact_fields_types_and_security(self):
        self.check_http_frames([self.body(objective_id=None), self.body(task_id=1),
                                self.body(objective_id=[]), self.body(task_id=True)])

    def test_any_key_presence_and_body_limit(self):
        self.check_headers_and_limit()

    def test_direct_plain_shape_before_domain(self):
        class StringSubclass(str):
            pass
        self.check_direct_frames([self.body(task_id=StringSubclass(self.task["id"])),
                                  self.body(objective_id=StringSubclass(self.objective["id"]))])

    def test_cross_store_and_injected_repository(self):
        self.check_repository_composition()

    def test_all_eight_alternate_delegates(self):
        self.check_all_delegates()

    def test_actual_v4_composition(self):
        self.check_actual_v4()


class LinkOwnerTransport(_LinkCase):
    def test_commit_then_drop_and_explicit_new_action(self):
        self.check_dropped_commit()

    def test_genuine_success_corruptions_and_malformed_json(self):
        from workstack.store import MAX_REVISION
        self.check_corrupted_success([("id", "T-9999"), ("id", 1), ("uid", "INVALID"),
            ("uid", "00000000-0000-0000-0000-000000000000"), ("status_fact_id", "fabricated"),
            ("revision", True), ("revision", 0), ("revision", MAX_REVISION + 1), ("updated_at", "bad"),
            ("objective_ids", []), ("objective_ids", [1]), ("objective_ids", "bad"),
            ("objective_ids", [self.objective["id"], self.objective["id"]]),
            ("objective_ids", [self.other["id"], self.objective["id"]])])

    def test_missing_operation_evidence_is_unknown_after_actual_commit(self):
        relay = self.start_owner()
        for field in ("id", "uid", "status_fact_id", "revision", "updated_at", "objective_ids"):
            relay.transform = lambda s, p: (s, {"data": {k: v for k, v in p["data"].items() if k != field}})
            before = self.effect_count()
            self.assert_unknown(self.invoke())
            self.assertEqual(self.effect_count(), before + 1)
        self.assertEqual(len(self.posts()), 6)

    def test_actual_400_403_409_are_final_and_sanitized(self):
        relay = self.start_owner()
        before = self.snapshot()
        with mock.patch.object(self.stack, "intent_commands", mock.Mock()):
            refused = self.invoke()
        self.assertEqual(refused[:2], (2, ""))
        self.assertIn("HTTP 400", refused[2])
        self.assertNotIn("storage composition", refused[2])
        self.assertEqual(before, self.snapshot())
        relay.get_hooks[tail.owner_fixture.PREFLIGHT[-1]] = lambda p: setattr(self.owner, "csrf_token", "rotated")
        self.assertIn("HTTP 403", self.invoke()[2])
        self.assertEqual(before, self.snapshot())
        relay.get_hooks.clear()
        relay.before_post = lambda: (self.root / "worklog.json").write_bytes(before["worklog.json"] + b"\n")
        self.assertIn("HTTP 409", self.invoke()[2])
        self.unchanged_except(before, {"worklog.json"})
        self.assertEqual(self.snapshot()["worklog.json"], before["worklog.json"] + b"\n")
        self.assertEqual([status for status, _ in relay.responses], [400, 403, 409])
        self.assertEqual(len(self.posts()), 3)

    def test_received_raw_dictionary_and_order_are_returned_unchanged(self):
        from workstack import cli_writer
        relay = self.start_owner()
        relay.transform = lambda s, p: (s, {"data": dict(reversed(list(p["data"].items())))})
        data = self.assert_success(self.invoke())
        genuine = relay.responses[-1][1]["data"]
        self.assertEqual(data, genuine)
        self.assertEqual(list(data), list(reversed(genuine)))
        self.assertIs(cli_writer._okr_link_result(200, {"data": data}, self.body()), data)

    def test_invalid_metadata_and_healthy_control(self):
        tail.owner_fixture.CheckinOwnerTransport.test_invalid_owner_advertisements_never_initialize_locally(self)

    def test_scoped_binary_eacces_and_same_owner_control(self):
        tail.owner_fixture.CheckinOwnerTransport.test_scoped_permission_denial_then_same_owner_control(self)

    def test_foreign_uid_and_not_in_sync(self):
        tail.owner_fixture.CheckinOwnerTransport.test_foreign_identity_and_not_in_sync_refuse(self)

    def test_final_binding_change_and_real_idle_endpoint_control(self):
        tail.owner_fixture.CheckinOwnerTransport.test_final_binding_removed_or_replaced_has_zero_wrong_owner_contacts(self)

    def test_genuine_handler_error_sink(self):
        tail.owner_fixture.CheckinOwnerTransport.test_real_owner_handler_error_sink_is_load_bearing(self)

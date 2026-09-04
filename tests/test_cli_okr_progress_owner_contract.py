"""Actual legacy progress clamping, raw KR output and prior/current audit.

Reuses the testless tail/checkin owned fixture and the explicit actual journal
fault scenario on this progress fixture; collected methods remain distinct.
"""
from __future__ import annotations

import http.client
import json
from unittest import mock

import test_cli_backlog_add_owner_contract as tail

PATH = "/api/v1/cli/okr/progress"


class _ProgressCase(tail._TailCase):
    path, operation = PATH, "OKR progress"
    setter, frame, save_method = "set_key_result_progress", "set_key_result_progress_cli", "save_many"
    changed = {"okr.json", "activity.json"}

    def setUp(self):
        super().setUp()
        self.objective = self.stack.add_objective("Progress target e\u0301 한글", "2026-Q3")
        self.kr = self.stack.add_key_result(self.objective["id"], "First KR", "keep target")
        self.sibling = self.stack.add_key_result(self.objective["id"], "Sibling KR", "another target")
        other = self.stack.add_objective("Other objective", "2026-Q4")
        self.stack.add_key_result(other["id"], "Same display ID in another Objective")
        self.stack.add_task("Task remains unchanged", objective_ids=[self.objective["id"]])

    def body(self, **changes):
        body = {"objective_id": self.objective["id"], "key_result_id": str(self.kr["id"]), "progress": 42}
        body.update(changes)
        return body

    def argv(self, body):
        return ["okr", "progress", body["objective_id"], body["key_result_id"], str(body["progress"])]

    def current_objective(self):
        return self.document("okr.json")["objectives"][0]

    def effect_count(self):
        return self.current_objective().get("revision", 0)

    def assert_save(self, save):
        from workstack.storage.document_repository import WorkspaceDocument
        self.assertEqual(set(save.call_args.args[0]), {WorkspaceDocument.OBJECTIVES, WorkspaceDocument.ACTIVITY})

    def persist_objective(self, objective):
        from workstack.storage.document_repository import WorkspaceDocument
        document = self.stack.documents.load(WorkspaceDocument.OBJECTIVES)
        document["objectives"][0] = objective
        with self.store.transaction():
            self.stack.documents.save(WorkspaceDocument.OBJECTIVES, document)

    def assert_progress_effect(self, data, before, expected):
        prior_document = json.loads(before["okr.json"])
        prior = prior_document["objectives"][0]
        prior_kr = prior["key_results"][0]
        objective = self.current_objective()
        self.assertEqual(objective["revision"], prior.get("revision", 0) + 1)
        self.assertEqual(objective["updated_at"], tail.DAY)
        self.assertEqual(data["progress"], expected)
        self.assertEqual(data["status"], "done" if expected == 100 else "active")
        self.assertEqual(objective["key_results"][0], data)
        self.assertEqual(objective["key_results"][1:], prior["key_results"][1:])
        self.assertEqual(self.document("okr.json")["objectives"][1:], prior_document["objectives"][1:])
        self.assert_preserved_raw_fields(data, objective, prior_kr, prior)
        self.assert_audit(before, prior, prior_kr, data)

    def assert_preserved_raw_fields(self, data, objective, prior_kr, prior):
        kr_keys = set(prior_kr) - {"progress", "status"}
        self.assertEqual({k: data[k] for k in kr_keys}, {k: prior_kr[k] for k in kr_keys})
        objective_keys = set(prior) - {"revision", "updated_at", "key_results"}
        self.assertEqual({k: objective[k] for k in objective_keys}, {k: prior[k] for k in objective_keys})
        expected_order = dict(prior_kr)
        expected_order.update(progress=data["progress"], status=data["status"])
        self.assertEqual(list(data), list(expected_order))

    def assert_audit(self, before, prior, prior_kr, data):
        activity = self.document("activity.json")
        old_activity = json.loads(before["activity.json"])
        self.assertEqual(activity["activity"][:-1], old_activity["activity"])
        self.assertEqual(activity["planning_status"], old_activity["planning_status"])
        self.assertEqual(activity["idempotency"], old_activity["idempotency"])
        event = activity["activity"][-1]
        self.assertEqual(event["type"], "key_result.updated")
        self.assertEqual(event["details"], {
            "objective_id": prior["id"], "key_result_id": prior_kr["id"],
            "prior": {"progress": prior_kr.get("progress", 0), "status": prior_kr.get("status", "active")},
            "current": {"progress": data["progress"], "status": data["status"]},
            "revision": prior.get("revision", 0) + 1,
        })


class ProgressParity(_ProgressCase):
    def test_absent_owner_keeps_legacy_progress_and_audit(self):
        before = self.snapshot()
        data = self.assert_success(self.invoke())
        self.assert_progress_effect(data, before, 42)
        self.unchanged_except(before, self.changed)

    def test_clamping_original_integer_status_and_repeated_equal_values(self):
        self.start_owner()
        for sent, expected in [(-500, 0), (0, 0), (37, 37), (100, 100), (900, 100), (900, 100)]:
            body = self.body(progress=sent, objective_id=" " + self.objective["id"].lower() + " ",
                             key_result_id=" " + str(self.kr["id"]).lower() + " ")
            data, before = self.compare(body)
            self.assert_progress_effect(data, before, expected)
            self.assertNotIn("objective_id", data)
            self.assertNotIn("revision", data)

    def test_objective_before_key_result_unknown_lookup_is_write_free(self):
        self.start_owner()
        cases = [(self.body(objective_id="O-9999", key_result_id="KR-9999", progress=-999), "unknown objective"),
                 (self.body(key_result_id="KR-9999", progress=999), "unknown key result")]
        for body, diagnostic in cases:
            local = self.clone()
            before = self.snapshot()
            expected, actual = self.invoke_body(body, root=local), self.invoke_body(body)
            self.assertEqual(expected[:2], (2, ""))
            self.assertIn(diagnostic, expected[2])
            self.assertEqual(actual[:2], (2, ""))
            self.assertIn("HTTP 404", actual[2])
            self.assertIn(diagnostic, self.relay.responses[-1][1]["error"]["message"])
            self.assertEqual(before, self.snapshot())
            self.assertEqual(before, self.snapshot(local))

    def test_objective_max_revision_precedes_unknown_kr(self):
        from workstack.store import MAX_REVISION
        objective = self.current_objective()
        objective["revision"] = MAX_REVISION - 1
        self.persist_objective(objective)
        self.start_owner()
        data, before = self.compare(self.body())
        self.assert_progress_effect(data, before, 42)
        self.assertEqual(self.effect_count(), MAX_REVISION)
        local = self.clone()
        before = self.snapshot()
        body = self.body(key_result_id="KR-UNKNOWN", progress=999)
        expected, actual = self.invoke_body(body, root=local), self.invoke_body(body)
        self.assertIn("safe integer limit", expected[2])
        self.assertEqual(actual[:2], (2, ""))
        self.assertIn("HTTP 400", actual[2])
        self.assertIn("safe integer limit", self.relay.responses[-1][1]["error"]["message"])
        self.assertEqual(before, self.snapshot())
        self.assertEqual(before, self.snapshot(local))

    def test_raw_extras_optional_fields_and_legacy_kr_identifier(self):
        objective = self.current_objective()
        kr = objective["key_results"][0]
        for key in ("text", "target", "progress", "status"):
            kr.pop(key)
        kr["id"] = "legacy e\u0301 결과"
        kr["opaque"] = {"nested": [False, {"nfd": "e\u0301"}], "keep": None}
        objective["key_results"][0] = dict(reversed(list(kr.items())))
        objective["extra"] = {"keep": [3, 2, 1]}
        self.persist_objective(objective)
        self.kr["id"] = kr["id"]
        self.start_owner()
        data, before = self.compare(self.body(key_result_id=" LEGACY E\u0301 결과 ", progress=-1))
        self.assert_progress_effect(data, before, 0)
        self.assertNotIn("text", data)
        self.assertNotIn("target", data)
        self.assertEqual(data["id"], "legacy e\u0301 결과")

    def test_numeric_stored_kr_id_uses_actual_string_lookup_rule(self):
        objective = self.current_objective()
        objective["key_results"][0]["id"] = 17
        self.persist_objective(objective)
        self.kr["id"] = 17
        self.start_owner()
        data, before = self.compare(self.body(key_result_id=" 17 "))
        self.assert_progress_effect(data, before, 42)
        self.assertIs(type(data["id"]), int)

    def test_missing_objective_revision_still_advances_from_zero(self):
        objective = self.current_objective()
        objective.pop("revision")
        self.persist_objective(objective)
        self.start_owner()
        data, before = self.compare(self.body())
        self.assert_progress_effect(data, before, 42)
        self.assertEqual(self.effect_count(), 1)

    def test_atomic_journal_failure_preserves_progress_and_audit(self):
        tail.BacklogParity.test_atomic_save_failure_has_no_task_or_fact(self)

    def test_actual_gui_patch_retains_its_distinct_fields_audit(self):
        self.start_owner()
        revision = self.effect_count()
        port = self.owner.actual_port
        body = {"revision": revision, "progress": 25, "status": "active"}
        path = f"/api/v1/objectives/{self.objective['id']}/key-results/{self.kr['id']}"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("PATCH", path, json.dumps(body), {
                "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}",
                "X-WorkStack-CSRF": self.owner.csrf_token,
            })
            response = connection.getresponse()
            status, payload = response.status, json.loads(response.read())
        finally:
            connection.close()
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["id"], self.objective["id"])
        event = self.document("activity.json")["activity"][-1]
        self.assertEqual(event["type"], "key_result.updated")
        self.assertEqual(event["details"], {"objective_id": self.objective["id"],
            "key_result_id": self.kr["id"], "fields": ["progress", "status"], "revision": revision + 1})
        self.assertEqual(self.document("activity.json")["idempotency"], [])


class ProgressFraming(_ProgressCase):
    def test_exact_fields_types_and_security(self):
        self.check_http_frames([self.body(objective_id=None), self.body(key_result_id=17),
            self.body(progress=True), self.body(progress=1.0), self.body(progress="42"),
            self.body(progress=None), self.body(progress=[])])

    def test_any_key_presence_and_body_limit(self):
        self.check_headers_and_limit()

    def test_direct_plain_shape_before_domain(self):
        class StringSubclass(str):
            pass
        class IntSubclass(int):
            pass
        self.check_direct_frames([self.body(objective_id=StringSubclass(self.objective["id"])),
            self.body(key_result_id=StringSubclass(str(self.kr["id"]))), self.body(progress=IntSubclass(42)),
            self.body(progress=True)])

    def test_cross_store_and_injected_repository(self):
        self.check_repository_composition()

    def test_all_eight_alternate_delegates(self):
        self.check_all_delegates()

    def test_actual_v4_composition(self):
        self.check_actual_v4()


class ProgressOwnerTransport(_ProgressCase):
    def test_commit_then_drop_and_explicit_new_action(self):
        self.check_dropped_commit()

    def test_genuine_success_corruptions_and_malformed_json(self):
        self.check_corrupted_success([("id", "KR-9999"), ("id", None), ("progress", True),
            ("progress", 42.0), ("progress", "42"), ("progress", 43), ("status", "done"),
            ("status", None), ("status", True)])

    def test_missing_operation_evidence_is_unknown_after_actual_commit(self):
        relay = self.start_owner()
        for field in ("id", "progress", "status"):
            relay.transform = lambda s, p: (s, {"data": {k: v for k, v in p["data"].items() if k != field}})
            before = self.effect_count()
            self.assert_unknown(self.invoke())
            self.assertEqual(self.effect_count(), before + 1)
        self.assertEqual(len(self.posts()), 3)

    def test_received_raw_dictionary_and_order_are_returned_unchanged(self):
        from workstack import cli_writer
        relay = self.start_owner()
        relay.transform = lambda s, p: (s, {"data": dict(reversed(list(p["data"].items())))})
        data = self.assert_success(self.invoke())
        genuine = relay.responses[-1][1]["data"]
        self.assertEqual(data, genuine)
        self.assertEqual(list(data), list(reversed(genuine)))
        self.assertIs(cli_writer._okr_progress_result(200, {"data": data}, self.body()), data)

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

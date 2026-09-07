"""Persistence and query tests for durable Task-status mutation notices."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from workstack.mutation_notice import (
    COMMITTED,
    TASK_STATUS_OPERATION,
    build_notice,
    derive_notice_id,
    serialize_notice,
)
from workstack.mutation_receipts import (
    EVENT_TYPE,
    NOTICE_DETAIL_KEY,
    MutationReceiptError,
    encode_list_cursor,
    find_notice,
    parse_list_query,
    unkeyed_status_key,
)
from workstack.service import WorkStack
from workstack.storage.task_deletion_transaction import TaskDeletionTransactionError
from workstack.store import Store


def _notices(stack: WorkStack) -> list[dict]:
    return stack.list_mutation_notices(limit=50)["items"]


def _notice_events(root: Path) -> list[dict]:
    activity = json.loads((root / "activity.json").read_text(encoding="utf-8"))
    return [event for event in activity.get("activity", []) if event.get("type") == EVENT_TYPE]


def _opaque_details(notice: dict) -> dict[str, str]:
    return {NOTICE_DETAIL_KEY: serialize_notice(notice).decode("utf-8")}


def _workspace_uid(stack: WorkStack) -> str:
    return stack.workspace_projection()["workspace"]["id"]


def _commit_permanent_delete(stack: WorkStack, task: dict, revision: int, suffix: str) -> dict:
    preview = stack.preview_task_deletion(
        task["id"],
        {
            "revision": revision,
            "workspace_uid": _workspace_uid(stack),
            "client_request_id": "repair-delete-{}".format(suffix),
        },
    )
    return stack.commit_task_deletion(
        task["id"],
        {"preview_token": preview["preview_token"], "confirm": task["id"]},
        request_digest="sha256:" + "a" * 64,
        path="/api/v1/tasks/{}".format(task["id"]),
        idempotency_key="repair-delete-commit-{}".format(suffix),
        if_match=str(revision),
    )


class QueryContract(unittest.TestCase):
    def test_limit_and_cursor_are_bounded_and_pinned(self) -> None:
        self.assertEqual(parse_list_query({}), (None, 20))
        self.assertEqual(parse_list_query({"limit": ["2"]}), (None, 2))
        cursor = encode_list_cursor(
            "123e4567-e89b-42d3-a456-426614174000", "E-000003"
        )
        self.assertFalse(cursor.startswith("E-"))
        self.assertEqual(
            parse_list_query({"cursor": [cursor], "limit": ["1"]}),
            (cursor, 1),
        )
        with self.assertRaises(ValueError):
            parse_list_query({"extra": ["1"]})
        with self.assertRaises(ValueError):
            parse_list_query({"limit": ["0"]})
        with self.assertRaises(ValueError):
            parse_list_query({"limit": ["51"]})
        with self.assertRaises(ValueError):
            parse_list_query({"cursor": ["3"]})
        with self.assertRaises(ValueError):
            parse_list_query({"cursor": ["E-000003"]})


class PersistenceAndReopen(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Notice persistence")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_status_change_persists_a_committed_notice_queryable_after_reopen(
        self,
    ) -> None:
        self.stack.set_task_status(self.task["id"], "started", 0)
        items = _notices(self.stack)
        self.assertEqual(len(items), 1)
        notice = items[0]
        self.assertEqual(notice["operation"], TASK_STATUS_OPERATION)
        self.assertEqual(notice["commit_state"], COMMITTED)
        self.assertTrue(notice["undoable"])
        self.assertEqual(notice["status_before"], "open")
        self.assertEqual(notice["status_after"], "started")
        key = unkeyed_status_key(self.task["uid"], 0)
        self.assertEqual(
            notice["notice_id"],
            derive_notice_id(
                workspace_uid=self.stack._workspace_uid(),
                idempotency_key=key,
            ),
        )
        reopened = WorkStack(Store(self.root))
        replayed = _notices(reopened)
        self.assertEqual(replayed, items)
        self.assertEqual(len(_notice_events(self.root)), 1)

    def test_a_keyed_intent_reuses_the_caller_key_and_does_not_duplicate_on_replay(
        self,
    ) -> None:
        path = "/api/v1/tasks/{}".format(self.task["id"])
        first = self.stack.set_task_status_v1(
            self.task["id"],
            {"status": "started", "revision": 0},
            "intent.notice.0001",
            path=path,
        )
        self.assertEqual(first["status"], 200)
        items = _notices(self.stack)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["idempotency_key"], "intent.notice.0001")
        self.stack.set_task_status_v1(
            self.task["id"],
            {"status": "started", "revision": 0},
            "intent.notice.0001",
            path=path,
        )
        self.assertEqual(_notices(self.stack), items)
        self.assertEqual(len(_notice_events(self.root)), 1)

    def test_a_same_status_action_does_not_write_a_notice(self) -> None:
        before = self.root.joinpath("activity.json").read_bytes()
        projected = self.stack.set_task_status(self.task["id"], "open", 0)
        self.assertEqual(projected["status"], "open")
        self.assertEqual(self.root.joinpath("activity.json").read_bytes(), before)
        self.assertEqual(_notices(self.stack), [])


class AtomicFailure(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Atomic notice")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_failing_save_leaves_no_task_change_and_no_notice(self) -> None:
        store = self.stack.store
        before = {
            name: (self.root / name).read_bytes()
            for name in ("backlog.json", "activity.json")
        }
        original = store.save_many

        def failing(writes, operation_id=None):
            raise RuntimeError("injected save failure")

        store.save_many = failing
        try:
            with self.assertRaises(RuntimeError):
                self.stack.set_task_status(self.task["id"], "started", 0)
        finally:
            store.save_many = original
        after = {
            name: (self.root / name).read_bytes()
            for name in ("backlog.json", "activity.json")
        }
        self.assertEqual(after, before)
        self.assertEqual(_notices(self.stack), [])


class WorkspaceSeparation(unittest.TestCase):
    def test_notices_do_not_cross_stores(self) -> None:
        first_dir = tempfile.TemporaryDirectory()
        second_dir = tempfile.TemporaryDirectory()
        try:
            left = WorkStack(Store(Path(first_dir.name)))
            right = WorkStack(Store(Path(second_dir.name)))
            left_task = left.add_task("Left")
            right_task = right.add_task("Right")
            left.set_task_status(left_task["id"], "started", 0)
            right.set_task_status(right_task["id"], "done", 0)
            left_items = _notices(left)
            right_items = _notices(right)
            self.assertEqual(len(left_items), 1)
            self.assertEqual(len(right_items), 1)
            self.assertEqual(left_items[0]["workspace_uid"], left._workspace_uid())
            self.assertEqual(right_items[0]["workspace_uid"], right._workspace_uid())
            self.assertNotEqual(
                left_items[0]["notice_id"], right_items[0]["notice_id"]
            )
            self.assertTrue(
                all(item["workspace_uid"] == left._workspace_uid() for item in left_items)
            )
        finally:
            first_dir.cleanup()
            second_dir.cleanup()


class BoundedQueryAndUndo(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stack = WorkStack(Store(self.root))
        self.task = self.stack.add_task("Paged notices")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _cycle_status(self, count: int) -> None:
        status = "open"
        revision = 0
        for index in range(count):
            nxt = "started" if status == "open" else "open"
            self.stack.set_task_status(self.task["id"], nxt, revision)
            status = nxt
            revision += 1

    def test_pages_are_bounded_and_the_cursor_is_stable(self) -> None:
        self._cycle_status(5)
        first = self.stack.list_mutation_notices(limit=2)
        self.assertEqual(len(first["items"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        second = self.stack.list_mutation_notices(
            cursor=first["next_cursor"], limit=2
        )
        self.assertEqual(len(second["items"]), 2)
        ids = [item["notice_id"] for item in first["items"] + second["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        third = self.stack.list_mutation_notices(
            cursor=second["next_cursor"], limit=2
        )
        self.assertEqual(len(third["items"]), 1)
        self.assertIsNone(third["next_cursor"])
        self.assertEqual(len(_notices(self.stack)), 5)

    def test_undo_restores_status_and_stale_or_missing_history_does_not_write(
        self,
    ) -> None:
        self.stack.set_task_status(self.task["id"], "started", 0)
        notice = _notices(self.stack)[0]
        path = "/api/v1/mutation-notices/{}/undo".format(notice["notice_id"])
        result = self.stack.undo_mutation_notice(
            notice["notice_id"],
            {"revision": 1},
            "undo.notice.0001",
            path=path,
        )
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"]["data"]["status"], "open")
        self.assertEqual(result["body"]["data"]["revision"], 2)
        replay = self.stack.undo_mutation_notice(
            notice["notice_id"],
            {"revision": 1},
            "undo.notice.0001",
            path=path,
        )
        self.assertTrue(replay["body"]["meta"]["replayed"])
        before = {
            name: (self.root / name).read_bytes()
            for name in ("backlog.json", "activity.json")
        }
        with self.assertRaises(MutationReceiptError) as stale:
            self.stack.undo_mutation_notice(
                notice["notice_id"],
                {"revision": 1},
                "undo.notice.stale1",
                path=path,
            )
        self.assertEqual(stale.exception.code, "revision_conflict")
        self.assertEqual(
            {
                name: (self.root / name).read_bytes()
                for name in ("backlog.json", "activity.json")
            },
            before,
        )
        missing = "123e4567-e89b-42d3-a456-426614174099"
        with self.assertRaises(MutationReceiptError) as missing_err:
            self.stack.undo_mutation_notice(
                missing,
                {"revision": 2},
                "undo.notice.missing",
                path="/api/v1/mutation-notices/{}/undo".format(missing),
            )
        self.assertEqual(missing_err.exception.code, "not_found")
        self.assertEqual(
            {
                name: (self.root / name).read_bytes()
                for name in ("backlog.json", "activity.json")
            },
            before,
        )

    def test_patch_task_keeps_task_updated_last_and_records_the_status_notice(
        self,
    ) -> None:
        changed = self.stack.patch_task(
            self.task["id"], {"status": "started", "revision": 0}
        )
        self.assertEqual(changed["status"], "started")
        activity = self.stack.store.load("activity.json")
        self.assertEqual(activity["activity"][-1]["type"], "task.updated")
        notices = [
            event for event in activity["activity"] if event.get("type") == EVENT_TYPE
        ]
        self.assertEqual(len(notices), 1)
        self.assertEqual(set(notices[0]["details"]), {NOTICE_DETAIL_KEY})
        self.assertNotIn("entity_uid", notices[0]["details"])
        payload = json.loads(notices[0]["details"][NOTICE_DETAIL_KEY])
        self.assertEqual(payload["status_after"], "started")

    def test_a_planted_non_status_notice_is_not_undoable(self) -> None:
        workspace_uid = self.stack._workspace_uid()
        foreign = build_notice(
            workspace_uid=workspace_uid,
            entity_kind="task",
            entity_uid=self.task["uid"],
            operation="task.create",
            before_revision=0,
            after_revision=0,
            source="gui",
            actor="local.user",
            idempotency_key="create:notice:1",
            commit_state=COMMITTED,
        )
        activity = self.stack.store.load("activity.json")
        activity.setdefault("activity", []).append(
            {
                "id": "E-009900",
                "type": EVENT_TYPE,
                "created_at": "2026-09-05T00:00:00Z",
                "task_id": self.task["id"],
                "details": _opaque_details(foreign),
            }
        )
        self.stack.store.save("activity.json", activity)
        before = {
            name: (self.root / name).read_bytes()
            for name in ("backlog.json", "activity.json")
        }
        path = "/api/v1/mutation-notices/{}/undo".format(foreign["notice_id"])
        with self.assertRaises(MutationReceiptError) as planted:
            self.stack.undo_mutation_notice(
                str(foreign["notice_id"]),
                {"revision": 0},
                "undo.notice.create1",
                path=path,
            )
        self.assertEqual(planted.exception.code, "invalid_request")
        self.assertEqual(
            {
                name: (self.root / name).read_bytes()
                for name in ("backlog.json", "activity.json")
            },
            before,
        )
        self.assertIsNotNone(find_notice(activity, str(foreign["notice_id"])))


class OpaqueWorkspaceCursor(unittest.TestCase):
    def test_cursor_is_opaque_and_bound_to_issuing_workspace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-left-") as left_raw, tempfile.TemporaryDirectory(
            prefix="repair-notices-right-"
        ) as right_raw:
            left = WorkStack(Store(Path(left_raw)))
            right = WorkStack(Store(Path(right_raw)))
            for stack, title in ((left, "Left cursor"), (right, "Right cursor")):
                task = stack.add_task(title)
                stack.set_task_status(task["id"], "started", 0)
                stack.set_task_status(task["id"], "open", 1)
            cursor = left.list_mutation_notices(limit=1)["next_cursor"]
            self.assertIsInstance(cursor, str)
            self.assertFalse(cursor.startswith("E-"), "cursor exposes an Activity event id")

    def test_cursor_from_another_workspace_is_rejected_without_local_leak(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-left-") as left_raw, tempfile.TemporaryDirectory(
            prefix="repair-notices-right-"
        ) as right_raw:
            left = WorkStack(Store(Path(left_raw)))
            right = WorkStack(Store(Path(right_raw)))
            for stack, title in ((left, "Left binding"), (right, "Right binding")):
                task = stack.add_task(title)
                stack.set_task_status(task["id"], "started", 0)
                stack.set_task_status(task["id"], "open", 1)
            cursor = left.list_mutation_notices(limit=1)["next_cursor"]
            with self.assertRaises(ValueError) as refused:
                right.list_mutation_notices(cursor=cursor, limit=1)
            text = str(refused.exception)
            self.assertEqual(text, "mutation notice query is invalid")
            self.assertNotIn(_workspace_uid(left), text)
            self.assertNotIn(_workspace_uid(right), text)
            self.assertNotIn("E-", text)

    def test_malformed_version_and_oversized_cursors_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-cursor-") as raw:
            stack = WorkStack(Store(Path(raw)))
            task = stack.add_task("Malformed cursor")
            stack.set_task_status(task["id"], "started", 0)
            stack.set_task_status(task["id"], "open", 1)
            issued = stack.list_mutation_notices(limit=1)["next_cursor"]
            oversized = "A" * 161
            with self.assertRaises(ValueError) as oversized_err:
                stack.list_mutation_notices(cursor=oversized, limit=1)
            self.assertEqual(str(oversized_err.exception), "mutation notice query is invalid")
            self.assertNotIn(_workspace_uid(stack), str(oversized_err.exception))
            with self.assertRaises(ValueError) as raw_event:
                stack.list_mutation_notices(cursor="E-000001", limit=1)
            self.assertEqual(str(raw_event.exception), "mutation notice query is invalid")
            wrong_version = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "e": "E-000001",
                        "v": 2,
                        "w": _workspace_uid(stack),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).decode("ascii").rstrip("=")
            with self.assertRaises(ValueError) as versioned:
                stack.list_mutation_notices(cursor=wrong_version, limit=1)
            self.assertEqual(str(versioned.exception), "mutation notice query is invalid")
            self.assertNotIn(_workspace_uid(stack), str(versioned.exception))
            self.assertIsInstance(issued, str)


class StableAppendPagination(unittest.TestCase):
    def test_pages_stay_stable_and_non_overlapping_under_appended_events(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-page-") as raw:
            stack = WorkStack(Store(Path(raw)))
            task = stack.add_task("Paged append")
            status = "open"
            revision = 0
            for _index in range(4):
                nxt = "started" if status == "open" else "open"
                stack.set_task_status(task["id"], nxt, revision)
                status = nxt
                revision += 1
            before_full = [item["notice_id"] for item in stack.list_mutation_notices(limit=50)["items"]]
            first = stack.list_mutation_notices(limit=2)
            held = [item["notice_id"] for item in first["items"]]
            cursor = first["next_cursor"]
            stack.set_task_status(task["id"], "started" if status == "open" else "open", revision)
            second = stack.list_mutation_notices(cursor=cursor, limit=2)
            continued = [item["notice_id"] for item in second["items"]]
            self.assertEqual(len(continued), 2)
            self.assertTrue(set(held).isdisjoint(continued))
            self.assertEqual(continued, before_full[2:4])


class PermanentDeleteAfterNotice(unittest.TestCase):
    def _assert_purged(self, root: Path, task: dict) -> None:
        backlog = json.loads((root / "backlog.json").read_text(encoding="utf-8"))
        self.assertFalse(any(row.get("id") == task["id"] for row in backlog["tasks"]))
        activity = json.loads((root / "activity.json").read_text(encoding="utf-8"))
        self.assertEqual(_notice_events(root), [])
        for event in activity.get("activity", []):
            self.assertNotEqual(event.get("task_id"), task["id"])
            self.assertNotIn("entity_uid", event.get("details", {}))
        for fact in activity.get("planning_status", []):
            self.assertNotEqual(fact.get("task_id"), task["id"])
            self.assertNotEqual(fact.get("task_uid"), task["uid"])
        self.assertNotIn('"entity_uid"', (root / "activity.json").read_text(encoding="utf-8"))

    def test_noticed_task_can_be_previewed_and_permanently_deleted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-delete-") as raw:
            root = Path(raw)
            stack = WorkStack(Store(root))
            task = stack.add_task("Delete after noticed transition")
            stack.set_task_status(task["id"], "started", 0)
            receipt = _commit_permanent_delete(stack, task, 1, "after-transition")
            self.assertTrue(receipt["deleted"])
            self._assert_purged(root, task)

    def test_noticed_task_can_be_deleted_after_undo(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-undo-delete-") as raw:
            root = Path(raw)
            stack = WorkStack(Store(root))
            task = stack.add_task("Delete after undo")
            stack.set_task_status(task["id"], "started", 0)
            notice = _notices(stack)[0]
            stack.undo_mutation_notice(
                notice["notice_id"],
                {"revision": 1},
                "repair-undo-before-delete",
                path="/api/v1/mutation-notices/{}/undo".format(notice["notice_id"]),
            )
            receipt = _commit_permanent_delete(stack, task, 2, "after-undo")
            self.assertTrue(receipt["deleted"])
            self._assert_purged(root, task)

    def test_nested_entity_uid_notice_still_refuses_deletion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-unadmitted-") as raw:
            root = Path(raw)
            stack = WorkStack(Store(root))
            task = stack.add_task("Unadmitted nested notice")
            nested = build_notice(
                workspace_uid=_workspace_uid(stack),
                entity_kind="task",
                entity_uid=task["uid"],
                operation=TASK_STATUS_OPERATION,
                before_revision=0,
                after_revision=1,
                source="gui",
                actor="local.user",
                idempotency_key="unadmitted:nested:1",
                commit_state=COMMITTED,
                status_before="open",
                status_after="started",
            )
            activity = stack.store.load("activity.json")
            activity.setdefault("activity", []).append(
                {
                    "id": "E-009901",
                    "type": EVENT_TYPE,
                    "created_at": "2026-09-05T00:00:00Z",
                    "task_id": task["id"],
                    "details": dict(nested),
                }
            )
            stack.store.save("activity.json", activity)
            with self.assertRaises(TaskDeletionTransactionError) as refused:
                stack.preview_task_deletion(
                    task["id"],
                    {
                        "revision": 0,
                        "workspace_uid": _workspace_uid(stack),
                        "client_request_id": "repair-unadmitted-preview",
                    },
                )
            self.assertEqual(refused.exception.code, "unknown_unsafe_reference")
            self.assertTrue(any(row.get("id") == task["id"] for row in stack.store.load("backlog.json")["tasks"]))


class FileReplacementRecovery(unittest.TestCase):
    def _crash_after(self, store: Store, name: str):
        original_write = store._atomic_write_locked
        replaced = False

        def crash_after(path: Path, value: object) -> None:
            nonlocal replaced
            original_write(path, value)
            if path.name == name and not replaced:
                replaced = True
                raise RuntimeError("simulated process loss after {}".format(name))

        return original_write, crash_after

    def _assert_recovered(self, root: Path, task: dict, key: str) -> None:
        reopened = WorkStack(Store(root))
        current = reopened.get_task(task["id"])
        notices = reopened.list_mutation_notices(limit=50)["items"]
        activity = reopened.store.load("activity.json")
        self.assertEqual((current["status"], current["revision"]), ("started", 1))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["idempotency_key"], key)
        self.assertEqual(
            len([fact for fact in activity["planning_status"] if fact["task_id"] == task["id"]]),
            2,
        )
        self.assertEqual(
            len([entry for entry in activity["idempotency"] if entry["key"] == key]),
            1,
        )

    def test_partial_file_replacement_recovers_task_fact_notice_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-recover-backlog-") as raw:
            root = Path(raw)
            store = Store(root)
            stack = WorkStack(store)
            task = stack.add_task("Recover noticed transition")
            original_write, crash_after = self._crash_after(store, "backlog.json")
            store._atomic_write_locked = crash_after
            try:
                with self.assertRaises(RuntimeError):
                    stack.set_task_status_v1(
                        task["id"],
                        {"status": "started", "revision": 0},
                        "repair-recovery-status",
                        path="/api/v1/tasks/{}".format(task["id"]),
                    )
            finally:
                store._atomic_write_locked = original_write
            self.assertTrue(store.journal_path.exists())
            self._assert_recovered(root, task, "repair-recovery-status")

    def test_activity_file_replacement_recovers_or_keeps_all_together(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-recover-activity-") as raw:
            root = Path(raw)
            store = Store(root)
            stack = WorkStack(store)
            task = stack.add_task("Recover after activity write")
            original_write, crash_after = self._crash_after(store, "activity.json")
            store._atomic_write_locked = crash_after
            try:
                with self.assertRaises(RuntimeError):
                    stack.set_task_status_v1(
                        task["id"],
                        {"status": "started", "revision": 0},
                        "repair-recovery-activity",
                        path="/api/v1/tasks/{}".format(task["id"]),
                    )
            finally:
                store._atomic_write_locked = original_write
            self.assertTrue(store.journal_path.exists())
            self._assert_recovered(root, task, "repair-recovery-activity")


class HistoricalReplayAndDuplicateUndo(unittest.TestCase):
    def test_historical_replay_and_second_key_undo_do_not_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repair-notices-replay-") as raw:
            root = Path(raw)
            stack = WorkStack(Store(root))
            task = stack.add_task("Replay and duplicate Undo")
            task_path = "/api/v1/tasks/{}".format(task["id"])
            first = stack.set_task_status_v1(
                task["id"],
                {"status": "started", "revision": 0},
                "repair-original-status",
                path=task_path,
            )
            notice = stack.list_mutation_notices(limit=50)["items"][0]
            undo_path = "/api/v1/mutation-notices/{}/undo".format(notice["notice_id"])
            stack.undo_mutation_notice(
                notice["notice_id"],
                {"revision": 1},
                "repair-first-undo",
                path=undo_path,
            )
            before = {name: root.joinpath(name).read_bytes() for name in ("backlog.json", "activity.json")}
            replay = stack.set_task_status_v1(
                task["id"],
                {"status": "started", "revision": 0},
                "repair-original-status",
                path=task_path,
            )
            self.assertEqual(replay["body"]["data"], first["body"]["data"])
            self.assertTrue(replay["body"]["meta"]["replayed"])
            self.assertEqual(
                {name: root.joinpath(name).read_bytes() for name in before}, before
            )
            with self.assertRaisesRegex(ValueError, "stale"):
                stack.undo_mutation_notice(
                    notice["notice_id"],
                    {"revision": 2},
                    "repair-second-undo",
                    path=undo_path,
                )
            self.assertEqual(
                {name: root.joinpath(name).read_bytes() for name in before}, before
            )
            events = json.loads(root.joinpath("activity.json").read_text(encoding="utf-8"))["activity"]
            self.assertEqual(len([event for event in events if event.get("type") == EVENT_TYPE]), 2)


if __name__ == "__main__":
    unittest.main()

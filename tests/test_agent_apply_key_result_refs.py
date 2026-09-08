from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workstack import cli
from workstack.agent_apply_admission import AGENT_TASK_FIELDS, parse_apply_packet
from workstack.cli import apply_agent_update
from workstack.server import create_server
from workstack.service import WorkStack
from workstack.store import Store


WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
RETAINED_TASK_FIELDS = frozenset({
    "title", "detail", "status", "priority", "due", "scheduled",
    "estimate_minutes", "tags", "objective_ids", "parent_id", "dependencies",
})
STDIN_EXAMPLE = {
    "workspace_id": WORKSPACE_UID,
    "task_id": "T-0001",
    "expected_revision": 0,
    "changes": {
        "key_result_refs": [
            {"objective_id": "O-0001", "key_result_id": "KR-1"},
        ]
    },
}


class AgentApplyKeyResultRefsAdmissionTest(unittest.TestCase):
    def test_admission_adds_key_result_refs_without_dropping_supported_fields(self) -> None:
        self.assertEqual(AGENT_TASK_FIELDS, RETAINED_TASK_FIELDS | {"key_result_refs"})

    def test_parse_admits_scoped_refs_and_an_explicit_empty_clear(self) -> None:
        linked = parse_apply_packet(json.dumps(STDIN_EXAMPLE).encode("utf-8"))
        self.assertEqual(linked["changes"]["key_result_refs"], STDIN_EXAMPLE["changes"]["key_result_refs"])
        cleared = dict(STDIN_EXAMPLE)
        cleared["changes"] = {"key_result_refs": []}
        parsed = parse_apply_packet(json.dumps(cleared).encode("utf-8"))
        self.assertEqual(parsed["changes"]["key_result_refs"], [])

    def test_parse_still_rejects_unknown_fields_and_the_32kib_guard(self) -> None:
        unknown = {
            "workspace_id": WORKSPACE_UID,
            "task_id": "T-0001",
            "expected_revision": 0,
            "changes": {"revision": 99, "key_result_refs": []},
        }
        with self.assertRaisesRegex(ValueError, "supported mutable"):
            parse_apply_packet(json.dumps(unknown).encode("utf-8"))
        with self.assertRaisesRegex(ValueError, "32 KiB"):
            parse_apply_packet(b"{" + b"x" * (32 * 1024))

    def test_parse_does_not_reimplement_domain_ref_shape_checks(self) -> None:
        malformed = dict(STDIN_EXAMPLE)
        malformed["changes"] = {
            "key_result_refs": [{"objective_id": "O-0001", "extra": "x"}],
        }
        parsed = parse_apply_packet(json.dumps(malformed).encode("utf-8"))
        self.assertEqual(parsed["changes"]["key_result_refs"][0]["extra"], "x")


class _ApplyHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.runtime_dir = self.home / "runtime"
        self.env = patch.dict(
            os.environ,
            {"WORK_STACK_RUNTIME": str(self.runtime_dir)},
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temporary.cleanup()

    def invoke(self, argv: list[str], stdin: bytes) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin))):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def apply_argv(self, data_dir: Path, intent_id: str) -> list[str]:
        return [
            "--data-dir",
            str(data_dir),
            "agent",
            "--workspace-uid",
            WORKSPACE_UID,
            "apply",
            "--stdin",
            "--intent-id",
            intent_id,
        ]

    def seed(self, name: str) -> tuple[Path, dict[str, object]]:
        root = self.home / name
        store = Store(root)
        stack = WorkStack(store)
        workspace = store.load("workspace.json")
        workspace["id"] = WORKSPACE_UID
        store.save("workspace.json", workspace)
        first = stack.add_objective("First objective")
        second = stack.add_objective("Second objective")
        first_kr = stack.add_key_result(first["id"], "First outcome")
        second_kr = stack.add_key_result(second["id"], "Second outcome")
        task = stack.add_task("Agent-owned update", detail="Before")
        return root, {
            "task_id": task["id"],
            "revision": task["revision"],
            "first_id": first["id"],
            "second_id": second["id"],
            "first_kr": first_kr["id"],
            "second_kr": second_kr["id"],
        }

    def packet(
        self,
        fixture: dict[str, object],
        changes: dict[str, object],
        *,
        expected_revision: object | None = None,
    ) -> bytes:
        revision = fixture["revision"] if expected_revision is None else expected_revision
        return json.dumps({
            "workspace_id": WORKSPACE_UID,
            "task_id": fixture["task_id"],
            "expected_revision": revision,
            "changes": changes,
        }).encode("utf-8")

    def pair(self, fixture: dict[str, object], *, second: bool = False) -> dict[str, str]:
        if second:
            return {
                "objective_id": str(fixture["second_id"]),
                "key_result_id": str(fixture["second_kr"]),
            }
        return {
            "objective_id": str(fixture["first_id"]),
            "key_result_id": str(fixture["first_kr"]),
        }

    def persisted(self, root: Path, task_id: str) -> dict[str, object]:
        backlog = json.loads((root / "backlog.json").read_text(encoding="utf-8"))
        return next(item for item in backlog["tasks"] if item["id"] == task_id)

    @contextlib.contextmanager
    def owner_http(self, root: Path):
        stack = WorkStack(Store(root))
        server = create_server(stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield stack
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class AgentApplyKeyResultRefsEndToEndTest(_ApplyHarness):
    def _apply(
        self,
        root: Path,
        fixture: dict[str, object],
        changes: dict[str, object],
        intent_id: str,
        *,
        http: bool,
        expected_revision: object | None = None,
    ) -> tuple[int, str, str]:
        stdin = self.packet(fixture, changes, expected_revision=expected_revision)
        if http:
            with self.owner_http(root):
                return self.invoke(self.apply_argv(root, intent_id), stdin)
        return self.invoke(self.apply_argv(root, intent_id), stdin)

    def test_exact_scoped_pairs_apply_on_exclusive_local_and_owner_http(self) -> None:
        for http, name in ((False, "local"), (True, "http")):
            with self.subTest(route=name):
                root, fixture = self.seed(name)
                status, stdout, stderr = self._apply(
                    root,
                    fixture,
                    {"key_result_refs": [self.pair(fixture)]},
                    "agent.kr.apply.{}".format(name),
                    http=http,
                )
                self.assertEqual(status, 0, stderr or stdout)
                receipt = json.loads(stdout)
                self.assertEqual(
                    receipt["meta"]["mode"],
                    "running-server" if http else "exclusive-local-store",
                )
                self.assertEqual(receipt["data"]["key_result_refs"], [self.pair(fixture)])
                self.assertEqual(receipt["data"]["objective_ids"], [fixture["first_id"]])
                self.assertEqual(receipt["data"]["revision"], 1)
                self.assertEqual(
                    self.persisted(root, str(fixture["task_id"]))["key_result_refs"],
                    [self.pair(fixture)],
                )

    def test_unknown_objective_or_key_result_writes_nothing(self) -> None:
        cases = (
            ("unknown-objective", {"objective_id": "O-9999", "key_result_id": "KR-1"}),
            ("unknown-kr", None),
        )
        for http in (False, True):
            for label, ref in cases:
                with self.subTest(route=http, case=label):
                    root, fixture = self.seed("{}-{}".format(label, http))
                    if ref is None:
                        ref = {
                            "objective_id": str(fixture["first_id"]),
                            "key_result_id": "KR-999",
                        }
                    before = (root / "backlog.json").read_bytes()
                    status, stdout, stderr = self._apply(
                        root,
                        fixture,
                        {"key_result_refs": [ref]},
                        "agent.kr.unknown.{}".format(label),
                        http=http,
                    )
                    self.assertEqual(status, 2)
                    self.assertIn("unknown key result reference", stdout + stderr)
                    self.assertEqual((root / "backlog.json").read_bytes(), before)

    def test_parent_objective_is_aligned_when_only_refs_are_sent(self) -> None:
        for http, name in ((False, "align-local"), (True, "align-http")):
            with self.subTest(route=name):
                root, fixture = self.seed(name)
                status, stdout, stderr = self._apply(
                    root,
                    fixture,
                    {"key_result_refs": [self.pair(fixture, second=True)]},
                    "agent.kr.align.{}".format(name),
                    http=http,
                )
                self.assertEqual(status, 0, stderr or stdout)
                receipt = json.loads(stdout)
                self.assertEqual(receipt["data"]["objective_ids"], [fixture["second_id"]])
                self.assertEqual(
                    receipt["data"]["key_result_refs"],
                    [self.pair(fixture, second=True)],
                )

    def test_explicit_empty_list_clears_and_omitted_field_preserves(self) -> None:
        for http, name in ((False, "clear-local"), (True, "clear-http")):
            with self.subTest(route=name):
                root, fixture = self.seed(name)
                status, stdout, stderr = self._apply(
                    root,
                    fixture,
                    {"key_result_refs": [self.pair(fixture)]},
                    "agent.kr.seed.{}".format(name),
                    http=http,
                )
                self.assertEqual(status, 0, stderr or stdout)
                linked = json.loads(stdout)["data"]
                fixture = dict(fixture)
                fixture["revision"] = linked["revision"]
                status, stdout, stderr = self._apply(
                    root,
                    fixture,
                    {"title": "Refs preserved"},
                    "agent.kr.omit.{}".format(name),
                    http=http,
                )
                self.assertEqual(status, 0, stderr or stdout)
                omitted = json.loads(stdout)["data"]
                self.assertEqual(omitted["title"], "Refs preserved")
                self.assertEqual(omitted["key_result_refs"], [self.pair(fixture)])
                self.assertEqual(
                    self.persisted(root, str(fixture["task_id"]))["key_result_refs"],
                    [self.pair(fixture)],
                )
                fixture["revision"] = omitted["revision"]
                status, stdout, stderr = self._apply(
                    root,
                    fixture,
                    {"key_result_refs": []},
                    "agent.kr.clear.{}".format(name),
                    http=http,
                )
                self.assertEqual(status, 0, stderr or stdout)
                cleared = json.loads(stdout)["data"]
                self.assertEqual(cleared["key_result_refs"], [])
                self.assertEqual(
                    self.persisted(root, str(fixture["task_id"]))["key_result_refs"],
                    [],
                )
                self.assertEqual(cleared["objective_ids"], [fixture["first_id"]])

    def test_malformed_refs_are_refused_by_domain_not_a_weaker_admission_check(self) -> None:
        payloads = (
            ["not-an-object"],
            [{"objective_id": "O-0001"}],
            [{"objective_id": "O-0001", "key_result_id": "KR-1", "extra": "x"}],
            "not-a-list",
        )
        for http in (False, True):
            for index, payload in enumerate(payloads):
                with self.subTest(route=http, payload=payload):
                    root, fixture = self.seed("malformed-{}-{}".format(int(http), index))
                    before = (root / "backlog.json").read_bytes()
                    status, stdout, stderr = self._apply(
                        root,
                        fixture,
                        {"key_result_refs": payload},
                        "agent.kr.malformed.0001",
                        http=http,
                    )
                    self.assertEqual(status, 2)
                    self.assertNotIn("Traceback", stdout + stderr)
                    self.assertEqual((root / "backlog.json").read_bytes(), before)

    def test_stale_revision_cas_changes_nothing(self) -> None:
        for http, name in ((False, "cas-local"), (True, "cas-http")):
            with self.subTest(route=name):
                root, fixture = self.seed(name)
                status, stdout, stderr = self._apply(
                    root,
                    fixture,
                    {"key_result_refs": [self.pair(fixture)]},
                    "agent.kr.cas.{}".format(name),
                    http=http,
                )
                self.assertEqual(status, 0, stderr or stdout)
                before = (root / "backlog.json").read_bytes()
                status, stdout, stderr = self._apply(
                    root,
                    fixture,
                    {"key_result_refs": []},
                    "agent.kr.cas.retry.{}".format(name),
                    http=http,
                    expected_revision=0,
                )
                self.assertEqual(status, 2)
                self.assertIn("stale", (stdout + stderr).lower())
                self.assertEqual((root / "backlog.json").read_bytes(), before)

    def test_duplicate_same_intent_does_not_create_a_second_write(self) -> None:
        for http, name in ((False, "dup-local"), (True, "dup-http")):
            with self.subTest(route=name):
                root, fixture = self.seed(name)
                intent = "agent.kr.same.{}".format(name)
                changes = {"key_result_refs": [self.pair(fixture)], "title": "Linked once"}
                status, stdout, stderr = self._apply(
                    root, fixture, changes, intent, http=http
                )
                self.assertEqual(status, 0, stderr or stdout)
                first = json.loads(stdout)["data"]
                status, stdout, stderr = self._apply(
                    root, fixture, changes, intent, http=http
                )
                self.assertEqual(status, 2)
                self.assertIn("stale", (stdout + stderr).lower())
                persisted = self.persisted(root, str(fixture["task_id"]))
                self.assertEqual(persisted["revision"], first["revision"])
                self.assertEqual(persisted["title"], "Linked once")
                self.assertEqual(persisted["key_result_refs"], [self.pair(fixture)])

    def test_supported_task_fields_still_apply_with_refs(self) -> None:
        root, fixture = self.seed("fields")
        status, stdout, stderr = self._apply(
            root,
            fixture,
            {
                "title": "KR and title",
                "priority": "P1",
                "key_result_refs": [self.pair(fixture)],
            },
            "agent.kr.fields.0001",
            http=False,
        )
        self.assertEqual(status, 0, stderr)
        receipt = json.loads(stdout)
        self.assertEqual(receipt["data"]["title"], "KR and title")
        self.assertEqual(receipt["data"]["priority"], "P1")
        self.assertEqual(receipt["data"]["key_result_refs"], [self.pair(fixture)])


class AgentApplyKeyResultRefsCommitUnknownTest(_ApplyHarness):
    def test_commit_unknown_keeps_the_original_intent_and_does_not_retry(self) -> None:
        root, fixture = self.seed("unknown")
        store = Store(root)
        calls: list[tuple[str, str | None]] = []
        changes = {"key_result_refs": [self.pair(fixture)]}
        intent = "agent.kr.loss.keep"

        def fake(
            host: str,
            port: int,
            method: str,
            path: str,
            **kwargs: object,
        ) -> tuple[int, dict[str, object]]:
            headers = kwargs.get("headers") if isinstance(kwargs.get("headers"), dict) else {}
            calls.append((method, headers.get("X-WorkStack-Agent-Intent")))
            if path == "/api/v1/session":
                return 200, {"data": {"csrf_token": "csrf-token"}}
            if path == "/api/v1/storage":
                return 200, {"data": {"workspace_id": WORKSPACE_UID}}
            if method == "PATCH":
                body = kwargs.get("body")
                self.assertIsInstance(body, dict)
                self.assertEqual(body["key_result_refs"], [self.pair(fixture)])
                self.assertEqual(body["revision"], 0)
                raise OSError("patch lost")
            raise OSError("get lost")

        with patch.object(cli, "_request_json", fake):
            with patch.object(cli, "_server_coordinates", return_value=("127.0.0.1", 9)):
                with self.assertRaisesRegex(OSError, "commit is unknown"):
                    apply_agent_update(
                        store,
                        {
                            "workspace_id": WORKSPACE_UID,
                            "task_id": fixture["task_id"],
                            "expected_revision": 0,
                            "changes": changes,
                        },
                        intent,
                        route="running-server",
                    )
        self.assertEqual([item[0] for item in calls].count("PATCH"), 1)
        self.assertEqual({item[1] for item in calls if item[0] == "PATCH"}, {intent})
        self.assertNotIn(None, {item[1] for item in calls if item[0] == "PATCH"})
        self.assertEqual(self.persisted(root, str(fixture["task_id"]))["revision"], 0)
        self.assertNotIn("key_result_refs", self.persisted(root, str(fixture["task_id"])))

    def test_transport_loss_accepts_exact_next_revision_canonical_refs(self) -> None:
        root, fixture = self.seed("verified")
        store = Store(root)
        pair = self.pair(fixture)
        intent = "agent.kr.loss.verify"
        calls: list[str] = []

        def fake(
            host: str,
            port: int,
            method: str,
            path: str,
            **kwargs: object,
        ) -> tuple[int, dict[str, object]]:
            calls.append(method)
            if path == "/api/v1/session":
                return 200, {"data": {"csrf_token": "csrf-token"}}
            if path == "/api/v1/storage":
                return 200, {"data": {"workspace_id": WORKSPACE_UID}}
            if method == "PATCH":
                raise OSError("patch lost")
            return 200, {
                "data": {
                    "task": {
                        "id": fixture["task_id"],
                        "revision": 1,
                        "key_result_refs": [pair],
                    }
                }
            }

        output = io.StringIO()
        with patch.object(cli, "_request_json", fake):
            with patch.object(cli, "_server_coordinates", return_value=("127.0.0.1", 9)):
                with contextlib.redirect_stdout(output):
                    result = apply_agent_update(
                        store,
                        {
                            "workspace_id": WORKSPACE_UID,
                            "task_id": fixture["task_id"],
                            "expected_revision": 0,
                            "changes": {"key_result_refs": [pair]},
                        },
                        intent,
                        route="running-server",
                    )
        self.assertEqual(result, 0)
        receipt = json.loads(output.getvalue())
        self.assertTrue(receipt["meta"]["verified_after_transport_loss"])
        self.assertEqual(receipt["meta"]["intent_id"], intent)
        self.assertEqual(calls.count("PATCH"), 1)


if __name__ == "__main__":
    unittest.main()

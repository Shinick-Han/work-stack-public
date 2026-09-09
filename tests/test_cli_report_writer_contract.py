"""Owner-required ``work-stack report create --date`` wire and refusal contract.

Drives the public ``cli.main`` entry point. Real-owner cases use
``workstack.server.create_server``. Transport families use a scripted loopback
owner that records exact request bytes. No product helper is patched as the
server.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SESSION_PATH = "/api/v1/session"
STORAGE_PATH = "/api/v1/storage"
SYNC_PATH = "/api/v1/sync/status"
PREVIEW_ROUTE = "/api/v1/reports/daily-preview"
CREATE_ROUTE = "/api/v1/reports"
DAY = "2026-08-30"
TEMPLATE = "daily-v1"
DIGEST = "sha256:" + "a" * 64
GENERATED = "2026-09-06T01:02:03Z"
MARKDOWN = "# Daily review 2026-08-30\n"
SUMMARY_KEYS = {
    "uid",
    "workspace_uid",
    "template",
    "period",
    "state",
    "revision",
    "source_digest",
    "replayed",
}
LEAKY_DIAGNOSTIC_CANARY = "scripted-owner-secret-must-not-be-printed"
LEAKY_PATH = r"C:\scripted-owner\internal\path\must-not-be-printed"
CATALOG_CANARY = "MUST-NOT-BE-SAVED-CONTEXT-TITLE"
_MISSING = object()
_REVISION_NOT_EXACT_ONE = (True, 1.0)
_STAMP_INVALID = (
    None,
    "",
    "2026-09-06T01:02:03.500Z",
    "2026-09-06T01:02:03+00:00",
    "2026-09-06 01:02:03Z",
    "2026-02-30T01:02:03Z",
)
_EARLIER_STAMP = "2026-09-06T01:02:02Z"


def _result_root() -> Path | None:
    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "report-writer-fixtures"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


def _assign_create_field(data: dict[str, Any], field: str, value: object) -> dict[str, Any]:
    mutated = copy.deepcopy(data)
    nested = field in ("content_revision", "document_revision", "authored_at")
    target = mutated["content_entry"] if nested else mutated
    if value is _MISSING:
        del target[field]
    else:
        target[field] = value
    return mutated


def _malformed_create_cases(good: dict[str, Any]) -> tuple[tuple[str, dict[str, Any]], ...]:
    cases: list[tuple[str, dict[str, Any]]] = []
    for field in ("revision", "content_revision", "document_revision"):
        for value in _REVISION_NOT_EXACT_ONE:
            cases.append(
                ("{}={!r}".format(field, value), _assign_create_field(good, field, value))
            )
    for field, value in (
        ("archived_from_state", "draft"),
        ("archived_at", GENERATED),
        ("archive_note", "kept"),
    ):
        cases.append(
            ("{}={!r}".format(field, value), _assign_create_field(good, field, value))
        )
    for field in ("created_at", "updated_at", "authored_at"):
        for value in _STAMP_INVALID:
            cases.append(
                ("{}={!r}".format(field, value), _assign_create_field(good, field, value))
            )
        cases.append(
            ("{} missing".format(field), _assign_create_field(good, field, _MISSING))
        )
    inconsistent = copy.deepcopy(good)
    inconsistent["created_at"] = GENERATED
    inconsistent["updated_at"] = _EARLIER_STAMP
    cases.append(("updated_at precedes created_at", inconsistent))
    return tuple(cases)


class _IsolatedRuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
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
        os.environ["TEMP"] = str(self.scratch)
        os.environ["TMP"] = str(self.scratch)
        os.environ["TMPDIR"] = str(self.scratch)
        self.addCleanup(self._restore_environment)

        from workstack.service import WorkStack
        from workstack.store import DEFAULTS, Store

        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.workspace_uid = self.store.load("workspace.json")["id"]
        self._advertised_owner_lease = None
        self._reports_baseline = (self.root / "reports.json").read_bytes()
        self._source_baseline = {
            name: (self.root / name).read_bytes()
            for name in DEFAULTS
            if name != "reports.json"
        }

    def hold_advertised_owner_lock(self) -> None:
        if self._advertised_owner_lease is not None:
            return
        lease = self.store.try_acquire_writer_lease()
        self.assertIsNotNone(lease)
        self._advertised_owner_lease = lease

    def _restore_environment(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def tearDown(self) -> None:
        lease = self._advertised_owner_lease
        self._advertised_owner_lease = None
        if lease is not None:
            self.store.release_writer_lease(lease)
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        from workstack import cli

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--data-dir", str(self.root), "report", "create", *arguments])
            except SystemExit as error:
                code = int(error.code or 2)
        return code, out.getvalue(), err.getvalue()

    def assert_no_report_write(self) -> None:
        self.assertEqual((self.root / "reports.json").read_bytes(), self._reports_baseline)
        for name, baseline in self._source_baseline.items():
            self.assertEqual((self.root / name).read_bytes(), baseline, name)

    def assert_sources_unchanged(self) -> None:
        for name, baseline in self._source_baseline.items():
            self.assertEqual((self.root / name).read_bytes(), baseline, name)


_TEMPLATES: dict[str, dict[str, Any]] | None = None


def _capture_preflight_templates() -> dict[str, dict[str, Any]]:
    global _TEMPLATES
    if _TEMPLATES is not None:
        return _TEMPLATES

    import http.client

    saved = {
        name: os.environ.get(name)
        for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
    }
    holder = tempfile.TemporaryDirectory(dir=_result_root())
    try:
        base = Path(holder.name)
        (base / "runtime").mkdir(parents=True, exist_ok=True)
        (base / "tmp").mkdir(parents=True, exist_ok=True)
        os.environ["WORK_STACK_RUNTIME"] = str(base / "runtime")
        os.environ["TEMP"] = str(base / "tmp")
        os.environ["TMP"] = str(base / "tmp")
        os.environ["TMPDIR"] = str(base / "tmp")

        from workstack.server import create_server
        from workstack.service import WorkStack
        from workstack.store import Store

        stack = WorkStack(Store(base / "data"))
        server = create_server(stack, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            captured: dict[str, dict[str, Any]] = {}
            for path in (SESSION_PATH, STORAGE_PATH, SYNC_PATH):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.actual_port, timeout=10
                )
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    captured[path] = json.loads(response.read().decode("utf-8"))
                finally:
                    connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        holder.cleanup()

    _TEMPLATES = captured
    return captured


class _RecordedRequest:
    def __init__(self, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    @property
    def idempotency_key(self) -> str | None:
        return self.headers.get("idempotency-key")

    @property
    def route(self) -> str:
        return self.path.split("?", 1)[0]


class _ScriptedOwner:
    def __init__(
        self,
        *,
        templates: dict[str, dict[str, Any]],
        workspace_uid: str,
        sync_state: str = "in-sync",
    ) -> None:
        self.requests: list[_RecordedRequest] = []
        self.script: list[Any] = []
        self.committed: list[dict[str, Any]] = []
        self.replies_by_key: dict[str, dict[str, Any]] = {}
        self.sync_state = sync_state
        self.workspace_uid = workspace_uid
        self.digest = DIGEST
        self.markdown = MARKDOWN
        self.generated_at = GENERATED
        self.on_preview = None
        self._templates = templates
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                return

            def _record(self, body: bytes) -> _RecordedRequest:
                recorded = _RecordedRequest(
                    self.command,
                    self.path,
                    {key.lower(): value for key, value in self.headers.items()},
                    body,
                )
                owner.requests.append(recorded)
                return recorded

            def _send(self, status: int, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def _drop(self) -> None:
                self.close_connection = True
                with contextlib.suppress(OSError):
                    self.connection.close()

            def _truncate(self, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded) + 512))
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(encoded[: max(1, len(encoded) // 2)])
                    self.wfile.flush()
                self.close_connection = True
                with contextlib.suppress(OSError):
                    self.connection.close()

            def do_GET(self) -> None:  # noqa: N802
                self._record(b"")
                if self.path == SESSION_PATH:
                    self._send(200, owner.session_payload())
                    return
                if self.path == STORAGE_PATH:
                    self._send(200, owner.storage_payload())
                    return
                if self.path.startswith(SYNC_PATH):
                    self._send(200, owner.sync_payload())
                    return
                if self.path.split("?", 1)[0] == PREVIEW_ROUTE:
                    if owner.on_preview is not None:
                        owner.on_preview()
                    self._send(200, {"data": owner.preview_payload(self.path)})
                    return
                self._send(404, {"error": {"code": "not_found", "message": "no route"}})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                recorded = self._record(body)
                if recorded.route != CREATE_ROUTE:
                    self._send(404, {"error": {"code": "not_found", "message": "no route"}})
                    return
                outcome = owner.script.pop(0) if owner.script else "commit"
                key = recorded.idempotency_key
                if outcome == "replay":
                    stored = owner.replies_by_key.get(key or "")
                    if stored is None:
                        self._send(
                            409,
                            {"error": {"code": "idempotency_conflict", "message": "unknown key"}},
                        )
                        return
                    replayed = {"data": stored["data"], "meta": {"replayed": True}}
                    self._send(201, replayed)
                    return
                if outcome == "drop":
                    self._drop()
                    return
                if outcome == "commit-then-drop":
                    owner.commit(body, key)
                    self._drop()
                    return
                if outcome == "commit-then-truncate":
                    payload = owner.commit(body, key)
                    self._truncate(payload)
                    return
                if isinstance(outcome, tuple):
                    status, payload = outcome
                    self._send(status, payload)
                    return
                self._send(201, owner.commit(body, key))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def session_payload(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._templates[SESSION_PATH]))

    def storage_payload(self) -> dict[str, Any]:
        payload = json.loads(json.dumps(self._templates[STORAGE_PATH]))
        payload["data"]["workspace_id"] = self.workspace_uid
        return payload

    def sync_payload(self) -> dict[str, Any]:
        payload = json.loads(json.dumps(self._templates[SYNC_PATH]))
        payload["data"]["state"] = self.sync_state
        payload["data"]["workspace_id"] = self.workspace_uid
        return payload

    def preview_payload(self, path: str) -> dict[str, Any]:
        date = parse_qs(urlparse(path).query).get("date", [DAY])[0]
        return {
            "workspace_uid": self.workspace_uid,
            "source_digest": self.digest,
            "preview": {
                "template": TEMPLATE,
                "period": {"kind": "day", "date": date},
                "generated_at": self.generated_at,
                "absence": "no records",
                "markdown": self.markdown if date == DAY else "# Daily review {}\n".format(date),
            },
            "context_catalog": {
                "captured_at": self.generated_at,
                "items": [
                    {
                        "capture_id": "C-0001",
                        "capture_revision": 1,
                        "title": CATALOG_CANARY,
                        "linked_task_ids": [],
                        "status": "inbox",
                    }
                ],
                "omitted_count": 0,
            },
        }

    @property
    def csrf_token(self) -> str:
        return str(self._templates[SESSION_PATH]["data"]["csrf_token"])

    def commit(self, body: bytes, key: str | None) -> dict[str, Any]:
        parsed = json.loads(body.decode("utf-8")) if body else {}
        uid = str(uuid.uuid4())
        now = "2026-09-06T01:02:03Z"
        entry = {
            "content_revision": 1,
            "document_revision": 1,
            "markdown": parsed.get("markdown"),
            "authored_at": now,
            "note": None,
        }
        data = {
            "uid": uid,
            "workspace_uid": parsed.get("workspace_uid"),
            "template": parsed.get("template"),
            "period": parsed.get("period"),
            "source_digest": parsed.get("source_digest"),
            "source_generated_at": parsed.get("source_generated_at"),
            "state": "draft",
            "revision": 1,
            "archived_from_state": None,
            "archived_at": None,
            "archive_note": None,
            "created_at": now,
            "updated_at": now,
            "content_entry": entry,
            "source_stale": False,
        }
        payload = {"data": data, "meta": {"replayed": False}}
        self.committed.append(data)
        if key:
            self.replies_by_key[key] = payload
        return payload

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def posts(self) -> list[_RecordedRequest]:
        return [item for item in self.requests if item.method == "POST" and item.route == CREATE_ROUTE]

    @property
    def previews(self) -> list[_RecordedRequest]:
        return [item for item in self.requests if item.method == "GET" and item.route == PREVIEW_ROUTE]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


class _ScriptedOwnerCase(_IsolatedRuntimeCase):
    sync_state = "in-sync"

    def setUp(self) -> None:
        super().setUp()
        templates = _capture_preflight_templates()
        self.owner = _ScriptedOwner(
            templates=templates,
            workspace_uid=self.workspace_uid,
            sync_state=self.sync_state,
        )
        self.addCleanup(self.owner.close)
        self.hold_advertised_owner_lock()
        self.store.write_server_info("127.0.0.1", self.owner.port)

    def assert_owner_was_contacted(self) -> None:
        self.assertTrue(self.owner.requests)


class ReportGrammarContract(_IsolatedRuntimeCase):
    def test_missing_date_is_rejected_before_mutation(self) -> None:
        code, out, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertNotEqual(err.strip(), "")
        self.assertEqual(out.strip(), "")
        self.assert_no_report_write()

    def test_invalid_date_is_rejected_before_mutation(self) -> None:
        for value in ("today", "2026-13-01", "2026-02-29", "2026-8-30"):
            with self.subTest(value=value):
                code, out, err = self.run_cli("--date", value)
                self.assertEqual(code, 2, msg=err)
                self.assertEqual(out.strip(), "")
                self.assert_no_report_write()

    def test_owner_absent_refuses_without_initialization_or_write(self) -> None:
        code, out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 2)
        self.assertIn("not running", err.casefold())
        self.assertEqual(out.strip(), "")
        self.assert_no_report_write()
        self.assertFalse(self.store.server_info_path.is_file())


class ReportWriterFreshSelectedDirectory(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=_result_root())
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
        os.environ["TEMP"] = str(self.scratch)
        os.environ["TMP"] = str(self.scratch)
        os.environ["TMPDIR"] = str(self.scratch)

    def tearDown(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temporary.cleanup()

    def test_owner_absent_on_fresh_selected_directory_creates_no_ssot_json_or_lease(
        self,
    ) -> None:
        from workstack.store import DEFAULTS, JOURNAL_NAME, LOCK_NAME

        self.assertFalse(self.root.exists())
        from workstack import cli

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(
                    ["--data-dir", str(self.root), "report", "create", "--date", DAY]
                )
            except SystemExit as error:
                code = int(error.code or 2)
        self.assertEqual(code, 2)
        self.assertIn("not running", err.getvalue().casefold())
        self.assertEqual(out.getvalue().strip(), "")
        created_files = (
            [path for path in self.root.rglob("*") if path.is_file()]
            if self.root.exists()
            else []
        )
        self.assertEqual(created_files, [])
        for name in (*DEFAULTS, LOCK_NAME, JOURNAL_NAME):
            self.assertFalse((self.root / name).exists(), name)


class ReportWriterRealOwnerContract(_IsolatedRuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        from workstack.server import create_server

        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        super().tearDown()

    def _get_report(self, uid: str) -> tuple[int, dict[str, Any]]:
        import http.client
        from urllib.parse import urlencode

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.actual_port, timeout=10
        )
        try:
            connection.request(
                "GET",
                "/api/v1/reports/" + uid + "?" + urlencode(
                    {"workspace_uid": self.workspace_uid}
                ),
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_real_owner_creates_exactly_one_draft_from_preview(self) -> None:
        code, out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 0, msg=err)
        payload = json.loads(out)
        self.assertEqual(set(payload), SUMMARY_KEYS)
        self.assertEqual(payload["template"], TEMPLATE)
        self.assertEqual(payload["period"], {"kind": "day", "date": DAY})
        self.assertEqual(payload["state"], "draft")
        self.assertEqual(payload["revision"], 1)
        self.assertIs(type(payload["revision"]), int)
        self.assertIs(payload["replayed"], False)
        self.assertNotIn("markdown", payload)
        self.assertNotIn("context_catalog", json.dumps(payload))

        stored = json.loads((self.root / "reports.json").read_text(encoding="utf-8"))
        self.assertEqual(len(stored["reports"]), 1)
        report = stored["reports"][0]
        self.assertEqual(report["uid"], payload["uid"])
        self.assertEqual(report["state"], "draft")
        self.assertNotIn("context_catalog", report)
        self.assertNotIn(CATALOG_CANARY, json.dumps(stored))
        self.assert_sources_unchanged()

        status, body = self._get_report(payload["uid"])
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["uid"], payload["uid"])
        self.assertEqual(body["data"]["state"], "draft")

    def test_second_explicit_invocation_is_duplicate_period(self) -> None:
        first, first_out, first_err = self.run_cli("--date", DAY)
        second, second_out, second_err = self.run_cli("--date", DAY)
        self.assertEqual(first, 0, msg=first_err)
        self.assertEqual(second, 2, msg=second_err)
        self.assertEqual(second_out.strip(), "")
        self.assertNotIn("scheduled", second_err.casefold())
        stored = json.loads((self.root / "reports.json").read_text(encoding="utf-8"))
        self.assertEqual(len(stored["reports"]), 1)
        self.assertEqual(
            stored["reports"][0]["uid"], json.loads(first_out)["uid"]
        )

    def test_real_out_of_process_cli_creates_one_draft(self) -> None:
        environment = dict(os.environ)
        environment["WORK_STACK_RUNTIME"] = str(self.runtime)
        environment["TEMP"] = str(self.scratch)
        environment["TMP"] = str(self.scratch)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import sys; from workstack.cli import main; sys.exit(main())",
                "--data-dir",
                str(self.root),
                "report",
                "create",
                "--date",
                DAY,
            ],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(set(payload), SUMMARY_KEYS)
        self.assertEqual(payload["period"], {"kind": "day", "date": DAY})


class ReportWriterWireContract(_ScriptedOwnerCase):
    def test_preview_then_create_omits_context_catalog_and_uses_report_key(self) -> None:
        code, out, err = self.run_cli("--date", DAY)
        self.assert_owner_was_contacted()
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(len(self.owner.previews), 1)
        self.assertEqual(len(self.owner.posts), 1)
        post = self.owner.posts[0]
        body = json.loads(post.body.decode("utf-8"))
        self.assertEqual(
            set(body),
            {
                "workspace_uid",
                "template",
                "period",
                "source_digest",
                "source_generated_at",
                "markdown",
            },
        )
        self.assertEqual(body["workspace_uid"], self.workspace_uid)
        self.assertEqual(body["template"], TEMPLATE)
        self.assertEqual(body["period"], {"kind": "day", "date": DAY})
        self.assertEqual(body["source_digest"], DIGEST)
        self.assertEqual(body["source_generated_at"], GENERATED)
        self.assertEqual(body["markdown"], MARKDOWN)
        self.assertNotIn("context_catalog", body)
        self.assertNotIn(CATALOG_CANARY, post.body.decode("utf-8"))
        self.assertTrue(post.idempotency_key)
        self.assertTrue(str(post.idempotency_key).startswith("cli-report-"))
        self.assertEqual(post.headers.get("x-workstack-csrf"), self.owner.csrf_token)
        self.assertEqual(
            post.headers.get("origin"), "http://127.0.0.1:{}".format(self.owner.port)
        )
        query = parse_qs(urlparse(post.path).query)
        self.assertEqual(query.get("workspace_uid"), [self.workspace_uid])
        payload = json.loads(out)
        self.assertEqual(set(payload), SUMMARY_KEYS)
        self.assertIs(payload["replayed"], False)
        self.assertEqual(payload["revision"], 1)
        self.assertIs(type(payload["revision"]), int)
        self.assert_no_report_write()

    def test_each_new_invocation_uses_a_distinct_report_key(self) -> None:
        first, _, first_err = self.run_cli("--date", DAY)
        second, _, second_err = self.run_cli("--date", DAY)
        self.assertEqual(first, 0, msg=first_err)
        self.assertEqual(second, 0, msg=second_err)
        keys = [post.idempotency_key for post in self.owner.posts]
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(key and key.startswith("cli-report-") for key in keys))
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(len(self.owner.previews), 2)

    def test_lost_after_commit_replays_identical_bytes_key_and_201(self) -> None:
        self.owner.script = ["commit-then-drop", "replay"]
        code, out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 0, msg=err)
        posts = self.owner.posts
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].body, posts[1].body)
        self.assertEqual(posts[0].path, posts[1].path)
        self.assertEqual(posts[0].idempotency_key, posts[1].idempotency_key)
        self.assertEqual(len(self.owner.committed), 1)
        self.assertEqual(len(self.owner.previews), 1)
        payload = json.loads(out)
        self.assertIs(payload["replayed"], True)
        self.assertEqual(payload["uid"], self.owner.committed[0]["uid"])
        self.assertEqual(payload["revision"], 1)
        self.assertIs(type(payload["revision"]), int)

    def test_second_ambiguous_failure_exits_two_with_commit_unknown(self) -> None:
        self.owner.script = ["drop", "drop"]
        code, out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 2)
        self.assertEqual(len(self.owner.posts), 2)
        self.assertEqual(len(self.owner.previews), 1)
        self.assertIn("unknown", err.casefold())
        self.assertEqual(out.strip(), "")

    def test_determinate_duplicate_period_is_not_retried(self) -> None:
        self.owner.script = [
            (409, {"error": {"code": "report_duplicate_period", "message": LEAKY_DIAGNOSTIC_CANARY}})
        ]
        code, out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 2)
        self.assertEqual(len(self.owner.posts), 1)
        self.assertEqual(out.strip(), "")
        self.assertNotIn(LEAKY_DIAGNOSTIC_CANARY, out + err)
        self.assertNotIn("scheduled", err.casefold())

    def test_source_changed_is_not_retried_or_overwritten(self) -> None:
        self.owner.script = [
            (409, {"error": {"code": "report_source_changed", "message": LEAKY_PATH}})
        ]
        code, out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 2)
        self.assertEqual(len(self.owner.posts), 1)
        self.assertEqual(len(self.owner.committed), 0)
        self.assertNotIn(LEAKY_PATH, out + err)

    def test_false_status_pairing_stays_unknown(self) -> None:
        created = self.owner.commit(
            json.dumps(
                {
                    "workspace_uid": self.workspace_uid,
                    "template": TEMPLATE,
                    "period": {"kind": "day", "date": DAY},
                    "source_digest": DIGEST,
                    "source_generated_at": GENERATED,
                    "markdown": MARKDOWN,
                }
            ).encode("utf-8"),
            "unused",
        )
        created["meta"]["replayed"] = False
        self.owner.script = [(200, created)]
        code, out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 2)
        self.assertIn("unknown", err.casefold())
        self.assertEqual(out.strip(), "")

    def test_wrong_period_digest_and_content_2xx_stay_unknown(self) -> None:
        base = {
            "workspace_uid": self.workspace_uid,
            "template": TEMPLATE,
            "period": {"kind": "day", "date": DAY},
            "source_digest": DIGEST,
            "source_generated_at": GENERATED,
            "markdown": MARKDOWN,
        }
        encoded = json.dumps(base).encode("utf-8")
        good = self.owner.commit(encoded, "probe")["data"]
        cases = (
            {**good, "period": {"kind": "day", "date": "2026-08-31"}},
            {**good, "source_digest": "sha256:" + "b" * 64},
            {
                **good,
                "content_entry": {**good["content_entry"], "markdown": "# other\n"},
            },
            {**good, "workspace_uid": "00000000-0000-4000-8000-000000000001"},
        )
        for data in cases:
            with self.subTest(data=data):
                self.owner.script = [(201, {"data": data, "meta": {"replayed": False}})]
                code, out, err = self.run_cli("--date", DAY)
                self.assertEqual(code, 2)
                self.assertIn("unknown", err.casefold())
                self.assertEqual(out.strip(), "")

    def test_malformed_create_metadata_201_stays_unknown(self) -> None:
        base = {
            "workspace_uid": self.workspace_uid,
            "template": TEMPLATE,
            "period": {"kind": "day", "date": DAY},
            "source_digest": DIGEST,
            "source_generated_at": GENERATED,
            "markdown": MARKDOWN,
        }
        good = self.owner.commit(json.dumps(base).encode("utf-8"), "malformed")["data"]
        for label, data in _malformed_create_cases(good):
            with self.subTest(label=label):
                self.owner.script = [(201, {"data": data, "meta": {"replayed": False}})]
                code, out, err = self.run_cli("--date", DAY)
                self.assertEqual(code, 2, msg=err)
                self.assertIn("unknown", err.casefold())
                self.assertEqual(out.strip(), "")

    def test_error_payloads_never_leak_secrets_or_internal_paths(self) -> None:
        self.owner.script = [
            (
                500,
                {
                    "error": {
                        "code": "internal_error",
                        "message": "failed at {} with token {}".format(
                            LEAKY_PATH, LEAKY_DIAGNOSTIC_CANARY
                        ),
                    }
                },
            )
        ]
        code, out, err = self.run_cli("--date", DAY)
        combined = out + err
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.owner.posts), 1)
        self.assertNotIn(LEAKY_DIAGNOSTIC_CANARY, combined)
        self.assertNotIn(LEAKY_PATH, combined)
        self.assertNotIn(self.owner.csrf_token, combined)


class ReportWriterIdentityAndReadinessContract(_ScriptedOwnerCase):
    def test_workspace_mismatch_blocks_before_preview_or_post(self) -> None:
        self.owner.workspace_uid = "00000000-0000-4000-8000-000000000000"
        code, out, err = self.run_cli("--date", DAY)
        self.assert_owner_was_contacted()
        self.assertEqual(code, 2)
        self.assertEqual(self.owner.previews, [])
        self.assertEqual(self.owner.posts, [])
        self.assertEqual(out.strip(), "")
        self.assert_no_report_write()


class ReportWriterNotReadyContract(_ScriptedOwnerCase):
    sync_state = "external-change-detected"

    def test_not_in_sync_owner_blocks_the_write(self) -> None:
        code, _out, err = self.run_cli("--date", DAY)
        self.assert_owner_was_contacted()
        self.assertEqual(code, 2)
        self.assertEqual(self.owner.previews, [])
        self.assertEqual(self.owner.posts, [])
        self.assert_no_report_write()


class ReportWriterVanishingOwnerContract(_ScriptedOwnerCase):
    def test_metadata_replaced_during_prepare_never_posts(self) -> None:
        info = self.store.server_info_path
        original = info.read_bytes()
        removed = threading.Event()

        def vanish() -> None:
            with contextlib.suppress(OSError):
                info.unlink()
                removed.set()

        self.owner.on_preview = vanish
        code, _out, err = self.run_cli("--date", DAY)
        self.assertTrue(removed.is_set())
        self.assertNotEqual(code, 0, msg=err)
        self.assertEqual(self.owner.posts, [])
        self.assert_no_report_write()
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(original)


class ReportWriterUnusableOwnerMetadataContract(_IsolatedRuntimeCase):
    def _info_path(self) -> Path:
        info = self.store.server_info_path
        info.parent.mkdir(parents=True, exist_ok=True)
        return info

    def test_metadata_that_is_a_directory_fails_closed(self) -> None:
        info = self._info_path()
        self.hold_advertised_owner_lock()
        info.mkdir()
        code, _out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 2)
        self.assert_no_report_write()
        self.assertTrue(info.is_dir())

    def test_empty_metadata_file_fails_closed(self) -> None:
        info = self._info_path()
        self.hold_advertised_owner_lock()
        info.write_bytes(b"")
        code, _out, _err = self.run_cli("--date", DAY)
        self.assertEqual(code, 2)
        self.assert_no_report_write()
        self.assertEqual(info.read_bytes(), b"")

    def test_malformed_metadata_fails_closed_and_is_not_cleaned_up(self) -> None:
        info = self._info_path()
        self.hold_advertised_owner_lock()
        info.write_text("{not json", encoding="utf-8")
        code, _out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 2)
        self.assert_no_report_write()
        self.assertEqual(info.read_text(encoding="utf-8"), "{not json")

    def test_stale_unreachable_owner_fails_closed_without_local_write(self) -> None:
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        self.hold_advertised_owner_lock()
        self.store.write_server_info("127.0.0.1", dead_port)
        code, _out, err = self.run_cli("--date", DAY)
        self.assertEqual(code, 2)
        self.assert_no_report_write()
        self.assertTrue(self.store.server_info_path.is_file())


class ReportCapabilitySurface(unittest.TestCase):
    def test_report_create_is_owner_required_with_no_offline_route(self) -> None:
        from workstack import cli_capabilities as caps

        item = caps.require_capability("report.create")
        self.assertEqual(caps.command_family(item), caps.FAMILY_OWNER_REQUIRED)
        self.assertEqual(item.online_route, "owner_required_post:/api/v1/reports")
        self.assertEqual(item.offline_route, "none")
        self.assertTrue(item.requires_idempotency)
        self.assertNotIn("report.create", caps.SKILL_ADVERTISED_KEYS)

    def test_helper_does_not_import_cli_server_or_routing(self) -> None:
        import ast

        tree = ast.parse(
            (REPOSITORY_ROOT / "workstack" / "cli_writer_reports.py").read_text(
                encoding="utf-8"
            )
        )
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        self.assertTrue(
            names.isdisjoint({"workstack.cli", "workstack.server", "workstack.cli_routing"})
        )


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()

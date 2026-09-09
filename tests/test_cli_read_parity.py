"""Exclusive-local CLI reads match WorkStack; owner-held reads use CLI GET parity."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from workstack import cli, cli_reads
from workstack.cli_read_http import CLI_GET_ROUTES, QUERY_LIMIT
from workstack.service import WorkStack
from workstack.store import DEFAULTS, LOCK_NAME, Store, _FileLease


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
PUBLIC_PORT = 28765
DAY = "2026-09-05"


class ReadParity(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "data"
        self.runtime = self.home / "runtime"
        self.scratch = self.home / "tmp"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self._saved = {
            name: os.environ.get(name)
            for name in ("WORK_STACK_RUNTIME", "TEMP", "TMP", "TMPDIR")
        }
        os.environ["WORK_STACK_RUNTIME"] = str(self.runtime)
        os.environ["TEMP"] = str(self.scratch)
        os.environ["TMP"] = str(self.scratch)
        os.environ["TMPDIR"] = str(self.scratch)
        self.addCleanup(self._restore)
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        created = self.stack.add_task("Parity task", "", "P2", None, [], [], None, [])
        self.task_id = created["id"]
        self.stack.add_objective("Parity objective", "2026-Q3")
        self.stack.checkin("09:00", DAY)
        self.stack.add_worklog(
            created["id"], ["done item"], ["next item"], [], DAY
        )
        self.workspace_uid = self.store.load("workspace.json")["id"]
        self.owner = None
        self.endpoints: list[tuple[object, threading.Thread]] = []

    def _restore(self) -> None:
        for server, thread in self.endpoints:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.endpoints = []
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def snapshot(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in DEFAULTS}

    def run_cli(self, *arguments: str, forbid_local: bool = False) -> tuple[int, object, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.ExitStack() as contexts:
            contexts.enter_context(contextlib.redirect_stdout(out))
            contexts.enter_context(contextlib.redirect_stderr(err))
            if forbid_local:
                contexts.enter_context(
                    mock.patch.object(
                        cli, "WorkStack", side_effect=AssertionError("local fallback")
                    )
                )
            code = cli.main(["--data-dir", str(self.root), *arguments])
        payload = json.loads(out.getvalue()) if out.getvalue().strip() else None
        return code, payload, err.getvalue()

    def start_owner(self, *, public_port: int | None = PUBLIC_PORT):
        from workstack.server import create_server

        server = create_server(self.stack, "127.0.0.1", 0, public_port=public_port)
        server.errors = []

        def handle_error(_request, _address):
            server.errors.append(traceback.format_exc())

        server.handle_error = handle_error
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        self.endpoints.append((server, thread))
        self.owner = server
        return server

    def http_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        host: str | None = None,
        origin: str | None = None,
        csrf: str | None = None,
        port: int | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, object]]:
        listen = port if port is not None else self.owner.actual_port
        request_headers = dict(headers or {})
        if host is not None:
            request_headers["Host"] = host
        if origin is not None:
            request_headers["Origin"] = origin
        if csrf is not None:
            request_headers["X-WorkStack-CSRF"] = csrf
        if body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("Content-Length", str(len(body)))
        connection = http.client.HTTPConnection("127.0.0.1", listen, timeout=5)
        try:
            if host is not None:
                connection.putrequest(method, path, skip_host=True)
                for name, value in request_headers.items():
                    connection.putheader(name, value)
                connection.endheaders(body)
            else:
                connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8"))
        finally:
            connection.close()

    def session_csrf(self) -> str:
        status, payload = self.http_json("GET", "/api/v1/session")
        self.assertEqual(status, 200)
        token = payload["data"]["csrf_token"]
        self.assertIs(type(token), str)
        self.assertTrue(token)
        return token

    def owner_get(self, path: str, **kwargs) -> tuple[int, dict[str, object]]:
        port = self.owner.actual_port
        kwargs.setdefault("origin", "http://127.0.0.1:{}".format(port))
        kwargs.setdefault("csrf", self.session_csrf())
        return self.http_json("GET", path, **kwargs)

    def test_backlog_list_and_show_match_the_workstack_read_api(self) -> None:
        code, listed, err = self.run_cli("backlog", "list", "--status", "all")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(listed, self.stack.list_tasks("all"))
        task_id = listed[0]["id"]
        code, shown, err = self.run_cli("backlog", "show", task_id)
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(shown, self.stack.get_task(task_id))
        self.assertIn("status_fact_id", shown)

    def test_okr_worklog_and_weekly_match_where_supported(self) -> None:
        code, objectives, err = self.run_cli("okr", "list", "--status", "all")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(objectives, self.stack.list_objectives("all"))
        code, rollup, err = self.run_cli("okr", "rollup")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(rollup, self.stack.objective_rollup())
        code, worklog, err = self.run_cli("worklog", "list", "--date", DAY)
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(worklog, self.stack.list_worklog(DAY))
        code, weekly, err = self.run_cli("weekly", "--end", DAY, "--days", "7")
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(weekly, self.stack.weekly_report(DAY, 7))

    def test_owner_http_read_parity_covers_the_six_commands(self) -> None:
        self.assertEqual(cli_reads.OWNER_HTTP_READ_PARITY, cli_reads.PARITY_READ_KEYS)
        self.assertTrue(cli_reads.is_parity_read("backlog.list"))
        self.assertIn("worklog.latest-checkpoint", cli_reads.PARITY_READ_KEYS)
        self.assertTrue(cli_reads.is_parity_read("worklog.latest-checkpoint"))
        self.assertEqual(
            [route.handler for route in CLI_GET_ROUTES],
            [
                "_get_cli_okr_rollup",
                "_get_cli_backlog_show",
                "_get_cli_backlog_list",
                "_get_cli_okr_list",
                "_get_cli_worklog_list",
                "_get_cli_weekly",
            ],
        )

    def test_live_owner_matches_local_cli_without_a_second_writer_or_ssot_write(self) -> None:
        expected = {
            "backlog.list": self.run_cli("backlog", "list", "--status", "all")[1],
            "backlog.list.default": self.run_cli("backlog", "list")[1],
            "backlog.show": self.run_cli("backlog", "show", self.task_id)[1],
            "okr.list": self.run_cli("okr", "list", "--status", "all")[1],
            "okr.list.default": self.run_cli("okr", "list")[1],
            "okr.rollup": self.run_cli("okr", "rollup")[1],
            "worklog.list": self.run_cli("worklog", "list", "--date", DAY)[1],
            "worklog.list.default": self.run_cli("worklog", "list")[1],
            "weekly": self.run_cli("weekly", "--end", DAY, "--days", "7")[1],
            "weekly.default_days": self.run_cli("weekly", "--end", DAY)[1],
        }
        self.start_owner()
        advertised = json.loads(self.store.server_info_path.read_text(encoding="utf-8"))
        self.assertEqual(advertised["port"], self.owner.actual_port)
        self.assertNotEqual(self.owner.actual_port, PUBLIC_PORT)
        before = self.snapshot()
        other = Store(self.root)
        self.assertIsNone(other.try_acquire_writer_lease())
        vectors = (
            ("backlog.list", ("backlog", "list", "--status", "all")),
            ("backlog.list.default", ("backlog", "list")),
            ("backlog.show", ("backlog", "show", self.task_id)),
            ("okr.list", ("okr", "list", "--status", "all")),
            ("okr.list.default", ("okr", "list")),
            ("okr.rollup", ("okr", "rollup")),
            ("worklog.list", ("worklog", "list", "--date", DAY)),
            ("worklog.list.default", ("worklog", "list")),
            ("weekly", ("weekly", "--end", DAY, "--days", "7")),
            ("weekly.default_days", ("weekly", "--end", DAY)),
        )
        for key, argv in vectors:
            with self.subTest(command=key):
                code, payload, err = self.run_cli(*argv, forbid_local=True)
                self.assertEqual(code, 0, msg=err)
                self.assertEqual(payload, expected[key])
        self.assertEqual(self.snapshot(), before)
        shown = expected["backlog.show"]
        self.assertIn("status_fact_id", shown)
        status, gui = self.owner_get("/api/v1/tasks/{}".format(quote(self.task_id, safe="")))
        self.assertEqual(status, 200)
        self.assertNotIn("status_fact_id", gui["data"]["task"])

    def test_public_port_is_not_the_linux_client_endpoint(self) -> None:
        self.start_owner(public_port=PUBLIC_PORT)
        probe = socket.socket()
        try:
            reachable = probe.connect_ex(("127.0.0.1", PUBLIC_PORT)) == 0
        finally:
            probe.close()
        if reachable:
            self.skipTest("public port 28765 is already listening on this host")
        code, payload, err = self.run_cli(
            "backlog", "list", "--status", "all", forbid_local=True
        )
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(payload, self.stack.list_tasks("all"))

    def test_owner_http_requires_origin_csrf_and_rejects_injection(self) -> None:
        self.start_owner()
        uid = self.workspace_uid
        listed = "/api/v1/cli/backlog?workspace_uid={}&status=all".format(uid)
        status, payload = self.http_json("GET", listed)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "origin_required")
        status, payload = self.http_json(
            "GET",
            listed,
            origin="http://127.0.0.1:{}".format(self.owner.actual_port),
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "invalid_csrf")
        status, payload = self.owner_get(
            "/api/v1/cli/backlog?workspace_uid={}&status=all&extra=1".format(uid)
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_query")
        status, payload = self.owner_get(
            "/api/v1/cli/backlog/..%2F..%2Fetc?workspace_uid={}".format(uid)
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_query")
        status, payload = self.http_json(
            "POST",
            "/api/v1/cli/backlog",
            origin="http://127.0.0.1:{}".format(self.owner.actual_port),
            csrf=self.session_csrf(),
            body=b"{}",
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")
        status, payload = self.owner_get("/api/v1/cli/exec?workspace_uid={}".format(uid))
        self.assertEqual(status, 404)
        oversized = "x" * (QUERY_LIMIT + 1)
        status, payload = self.owner_get(
            "/api/v1/cli/backlog?workspace_uid={}&status={}".format(uid, oversized)
        )
        self.assertEqual(status, 400)
        other = "4d36e96e-e325-41ce-bfc1-08002be10318"
        status, payload = self.owner_get(
            "/api/v1/cli/backlog?workspace_uid={}&status=all".format(other)
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "workspace_mismatch")
        status, payload = self.owner_get(
            listed,
            headers={"Idempotency-Key": "cli-read-forbidden"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "unsupported_idempotency_key")

    def test_owner_error_behavior_matches_local_filters(self) -> None:
        code, payload, err = self.run_cli("backlog", "list", "--status", "bogus")
        self.assertEqual(code, 2)
        self.assertIsNone(payload)
        self.assertIn("invalid task status", err)
        missing = self.run_cli("backlog", "show", "T-9999")
        self.assertEqual(missing[0], 2)
        self.assertIn("unknown task", missing[2])
        self.start_owner()
        before = self.snapshot()
        owner_code, owner_payload, owner_err = self.run_cli(
            "backlog", "list", "--status", "bogus", forbid_local=True
        )
        self.assertEqual(owner_code, 2)
        self.assertIsNone(owner_payload)
        self.assertIn("invalid task status", owner_err)
        owner_missing = self.run_cli("backlog", "show", "T-9999", forbid_local=True)
        self.assertEqual(owner_missing[0], 2)
        self.assertIn("unknown task", owner_missing[2])
        self.assertEqual(self.snapshot(), before)

    def test_snapshot_preview_still_refuses_under_a_live_owner(self) -> None:
        self.start_owner()
        code, payload, err = self.run_cli("snapshot", "preview", self.task_id)
        self.assertEqual(code, 2)
        self.assertIsNone(payload)
        self.assertIn(cli_reads.OWNER_READ_REFUSAL, err)

    def test_dead_advertised_owner_refuses_without_local_json_fallback(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        self.store.write_server_info("127.0.0.1", dead_port)
        blocker = _FileLease(self.root / LOCK_NAME)
        blocker.acquire()
        self.addCleanup(blocker.release)
        before = self.snapshot()
        code, payload, err = self.run_cli("backlog", "list", forbid_local=True)
        self.assertEqual(code, 2)
        self.assertIsNone(payload)
        self.assertNotIn(cli_reads.OWNER_READ_REFUSAL, err)
        self.assertNotEqual(err.strip(), "")
        self.assertEqual(self.snapshot(), before)

    def test_out_of_process_owner_read_matches_local_bytes(self) -> None:
        local_code, local_payload, local_err = self.run_cli(
            "backlog", "list", "--status", "all"
        )
        self.assertEqual(local_code, 0, msg=local_err)
        self.start_owner()
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
                "backlog",
                "list",
                "--status",
                "all",
            ],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), local_payload)


if __name__ == "__main__":
    unittest.main()

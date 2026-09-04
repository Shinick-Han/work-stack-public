"""Public CLI/wire conformance for only ``okr add-objective``.

The real server supplies complete preflight and creation-response templates. The
scripted owner records serialized bytes and loses responses at the socket, without
patching a product transport helper. Fixture destinations are resolved and checked
before product imports and Store construction; no live runtime is inspected.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SESSION = "/api/v1/session"
STORAGE = "/api/v1/storage"
SYNC = "/api/v1/sync/status"
OBJECTIVES = "/api/v1/objectives"
LEGACY_KEYS = {"id", "quarter", "objective", "status", "key_results", "created", "updated_at"}
ENV_NAMES = ("WORK_STACK_HOME", "WORK_STACK_RUNTIME", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR")
DIAGNOSTIC_CANARY = "fixture-secret-must-not-leak"
INTERNAL_PATH = "fixture-internal-directory-must-not-leak"
_TEMPLATES: dict[str, Any] | None = None


def _contained(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise AssertionError("fixture destination escaped the configured result root")
    return resolved


@contextlib.contextmanager
def _sandbox():
    # The portable fallback is repository-local, never the user's Work Stack root.
    base = Path(os.environ.get("WORK_STACK_TEST_RESULT_ROOT", REPOSITORY_ROOT / ".artifacts" / "objective-contract")).resolve()
    fixture_parent = _contained(base / "objective-fixtures", base)
    fixture_parent.mkdir(parents=True, exist_ok=True)
    saved = {key: os.environ.get(key) for key in ENV_NAMES}
    saved_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(dir=fixture_parent) as directory:
        home = _contained(Path(directory), base)
        paths = {name: _contained(home / name, base) for name in ("data", "runtime", "localapp", "tmp")}
        root_key = hashlib.sha256(os.path.normcase(str(paths["data"])).encode("utf-8")).hexdigest()[:20]
        # Mirror the read-only path derivation, not a constructor used as a probe.
        runtime_root = _contained(paths["runtime"] / root_key, base)
        for name in (".workstack-store-manifest.json", "server.json"):
            _contained(runtime_root / name, base)
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        os.environ.update({
            "WORK_STACK_HOME": str(paths["data"]),
            "WORK_STACK_RUNTIME": str(paths["runtime"]),
            "LOCALAPPDATA": str(paths["localapp"]),
            "TEMP": str(paths["tmp"]), "TMP": str(paths["tmp"]), "TMPDIR": str(paths["tmp"]),
        })
        tempfile.tempdir = str(paths["tmp"])
        try:
            assert _contained(Path(tempfile.gettempdir()), base) == paths["tmp"]
            yield paths
        finally:
            tempfile.tempdir = saved_tempdir
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@contextlib.contextmanager
def _running_server(stack):
    from workstack.server import create_server

    server = create_server(stack, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive(), "owned fixture server did not shut down"


def _http(port: int, method: str, path: str, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _templates() -> dict[str, Any]:
    global _TEMPLATES
    if _TEMPLATES is None:
        with _sandbox() as paths:
            from workstack.service import WorkStack
            from workstack.store import Store

            with _running_server(WorkStack(Store(paths["data"]))) as server:
                captured = {}
                for path in (SESSION, STORAGE, SYNC):
                    status, captured[path] = _http(server.actual_port, "GET", path)
                    assert status == 200, "real preflight template capture failed"
                status, captured[OBJECTIVES] = _http(
                    server.actual_port, "POST", OBJECTIVES,
                    json.dumps({"objective": "Template", "quarter": "2001-Q1"}),
                    {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.actual_port}",
                     "X-WorkStack-CSRF": captured[SESSION]["data"]["csrf_token"],
                     "Idempotency-Key": "objective-fixture-" + uuid.uuid4().hex},
                )
                assert status == 201, "real objective template capture failed"
                assert set(captured[OBJECTIVES]["data"]) == LEGACY_KEYS | {"revision"}
                _TEMPLATES = captured
    return copy.deepcopy(_TEMPLATES)


class _WireOwner:
    """Fixture HTTP owner with exact wire recording and per-key replay storage."""

    def __init__(self, workspace_uid: str):
        self.payloads = _templates()
        for path in (STORAGE, SYNC):
            self.payloads[path]["data"]["workspace_id"] = workspace_uid
        self.requests: list[dict[str, Any]] = []
        self.commits: list[dict[str, Any]] = []
        self.by_key: dict[str, tuple[bytes, dict[str, Any]]] = {}
        self.script: list[Any] = []
        self.get_errors: dict[str, int] = {}
        self.after_get = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def record(self, raw=b""):
                entry = {"method": self.command, "path": self.path, "body": raw,
                         "headers": {key.lower(): value for key, value in self.headers.items()}}
                owner.requests.append(entry)
                return entry

            def send(self, status, payload, *, truncate=False, invalid=False):
                raw = b"{not-json" if invalid else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw) + (99 if truncate else 0)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
                if truncate:
                    self.drop()

            def drop(self):
                self.close_connection = True
                with contextlib.suppress(OSError):
                    self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()

            def do_GET(self):
                self.record()
                path = self.path.split("?")[0]
                if owner.after_get is not None:
                    owner.after_get(path)
                if path in owner.get_errors:
                    self.send(owner.get_errors[path], {"error": {"message": DIAGNOSTIC_CANARY}})
                elif path in owner.payloads:
                    self.send(200, owner.payloads[path])
                else:
                    self.send(404, {"error": {"message": "no fixture route"}})

            def do_POST(self):
                entry = self.record(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if self.path != OBJECTIVES:
                    self.send(404, {"error": {"message": "wrong objective route"}})
                    return
                action = owner.script.pop(0) if owner.script else "commit"
                if isinstance(action, int):
                    self.send(action, {"error": {"message": f"{DIAGNOSTIC_CANARY} {INTERNAL_PATH}"}})
                    return
                if action == "drop":
                    self.drop()
                    return
                key = entry["headers"].get("idempotency-key", "")
                raw = entry["body"]
                if key in owner.by_key:
                    prior_raw, payload = owner.by_key[key]
                    if prior_raw != raw:
                        self.send(409, {"error": {"message": "key/body conflict"}})
                        return
                    payload = copy.deepcopy(payload)
                    payload["meta"]["replayed"] = True
                else:
                    body = json.loads(raw)
                    payload = copy.deepcopy(owner.payloads[OBJECTIVES])
                    payload["data"].update({
                        "id": f"O-{len(owner.commits) + 1}", "objective": body["objective"].strip(),
                        "quarter": body["quarter"] or "2001-Q1", "created": "2001-02-03",
                        "updated_at": "2001-02-04",
                    })
                    owner.commits.append(payload["data"])
                    owner.by_key[key] = (raw, payload)
                if action == "commit-drop":
                    self.drop()
                else:
                    self.send(201, payload, truncate=action == "commit-truncate", invalid=action == "commit-invalid")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()


class _Case(unittest.TestCase):
    def setUp(self):
        self.context = contextlib.ExitStack()
        self.addCleanup(self.context.close)
        self.paths = self.context.enter_context(_sandbox())
        from workstack.service import WorkStack
        from workstack.store import Store

        self.store = Store(self.paths["data"])
        self.stack = WorkStack(self.store)
        self.root = self.paths["data"]
        self.uid = self.store.load("workspace.json")["id"]
        self.before = self.objectives()

    def objectives(self):
        return json.loads((self.root / "okr.json").read_bytes())["objectives"]

    def planning_bytes(self):
        from workstack.store import DEFAULTS
        return {name: (self.root / name).read_bytes() for name in DEFAULTS}

    def cli(self, text, *extra):
        from workstack.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--data-dir", str(self.root), "okr", "add-objective", text, *extra])
        return code, out.getvalue(), err.getvalue()

    def subprocess_cli(self, text, *extra):
        environment = dict(os.environ)
        for key in ENV_NAMES:
            _contained(Path(environment[key]), Path(os.environ["WORK_STACK_TEST_RESULT_ROOT"]) if os.environ.get("WORK_STACK_TEST_RESULT_ROOT") else REPOSITORY_ROOT / ".artifacts" / "objective-contract")
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import sys; from workstack.cli import main; sys.exit(main())",
             "--data-dir", str(self.root), "okr", "add-objective", text, *extra],
            cwd=REPOSITORY_ROOT, env=environment, capture_output=True, text=True, timeout=30,
        )
        return result.returncode, result.stdout, result.stderr

    def wire_owner(self):
        owner = _WireOwner(self.uid)
        self.context.callback(owner.close)
        self.store.write_server_info("127.0.0.1", owner.port)
        return owner

    def assert_refusal(self, result, before):
        code, out, err = result
        self.assertEqual(code, 2, msg="unusable owner must not fall back to a local objective write")
        self.assertEqual(out.strip(), "")
        self.assertTrue(err.strip())
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.planning_bytes(), before)


class ObjectiveLocalContract(_Case):
    def test_legacy_stdout_has_exact_seven_fields_and_trimmed_text(self):
        code, out, err = self.cli("  Legacy objective  ")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(set(result), LEGACY_KEYS)
        self.assertEqual(result["objective"], "Legacy objective")
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["key_results"], [])
        self.assertEqual(self.objectives(), [result])

    def test_omitted_and_empty_quarter_use_current_quarter(self):
        def quarter():
            day = dt.date.today()
            return f"{day.year}-Q{(day.month - 1) // 3 + 1}"
        for flags in ((), ("--quarter", "")):
            with self.subTest(flags=flags):
                before = quarter()
                code, out, err = self.cli("Quarter default", *flags)
                self.assertEqual(code, 0, err)
                self.assertIn(json.loads(out)["quarter"], {before, quarter()})

    def test_arbitrary_explicit_quarter_is_preserved_exactly(self):
        code, out, err = self.cli("Explicit", "--quarter", "  fiscal period: custom  ")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["quarter"], "  fiscal period: custom  ")

    def test_empty_text_refuses_with_legacy_diagnostic(self):
        before = self.planning_bytes()
        result = self.cli("  \t ")
        self.assert_refusal(result, before)
        self.assertIn("objective is required", result[2])

    def test_real_subprocess_executes_command_and_emits_legacy_record(self):
        code, out, err = self.subprocess_cli("Subprocess", "--quarter", "custom")
        self.assertEqual(code, 0, err)
        self.assertEqual(set(json.loads(out)), LEGACY_KEYS)
        self.assertEqual(self.objectives()[0]["objective"], "Subprocess")


class ObjectiveRealOwnerContract(_Case):
    def setUp(self):
        super().setUp()
        self.server = self.context.enter_context(_running_server(self.stack))

    def test_running_owner_creates_one_and_strips_revision(self):
        code, out, err = self.cli("Owned", "--quarter", " custom ")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(set(result), LEGACY_KEYS)
        self.assertEqual(result["quarter"], " custom ")
        self.assertEqual(len(self.objectives()), 1)
        self.assertEqual({k: v for k, v in self.objectives()[0].items() if k != "revision"}, result)

    def test_running_owner_omitted_and_empty_quarter_are_server_defaults(self):
        for flags in ((), ("--quarter", "")):
            with self.subTest(flags=flags):
                code, out, err = self.cli("Default quarter", *flags)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["quarter"], self.objectives()[-1]["quarter"])
                self.assertEqual(set(json.loads(out)), LEGACY_KEYS)

    def test_fresh_identical_invocations_remain_two_objectives(self):
        results = [self.cli("Same intent text") for _ in range(2)]
        for code, _out, err in results:
            self.assertEqual(code, 0, err)
        self.assertNotEqual(json.loads(results[0][1])["id"], json.loads(results[1][1])["id"])
        self.assertEqual(len(self.objectives()), 2)

    def test_owner_route_works_through_real_subprocess(self):
        code, out, err = self.subprocess_cli("Owned subprocess")
        self.assertEqual(code, 0, err)
        self.assertEqual(set(json.loads(out)), LEGACY_KEYS)
        self.assertEqual(len(self.objectives()), 1)


class ObjectiveWireContract(_Case):
    def setUp(self):
        super().setUp()
        self.owner = self.wire_owner()
        self.initial = self.planning_bytes()

    def contacted(self):
        self.assertTrue(self.owner.requests, "objective command never contacted the advertised loopback owner")

    def test_exact_endpoint_body_csrf_origin_and_legacy_response_dates(self):
        code, out, err = self.cli("  Wire objective  ", "--quarter", " custom quarter ")
        self.contacted()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.owner.posts), 1)
        post = self.owner.posts[0]
        self.assertEqual(post["path"], OBJECTIVES)
        body = json.loads(post["body"])
        self.assertEqual(set(body), {"objective", "quarter"})
        self.assertIs(type(body["objective"]), str)
        self.assertEqual(body["objective"].strip(), "Wire objective")
        self.assertEqual(body["quarter"], " custom quarter ")
        self.assertEqual(post["headers"].get("x-workstack-csrf"), self.owner.payloads[SESSION]["data"]["csrf_token"])
        self.assertEqual(post["headers"].get("origin"), f"http://127.0.0.1:{self.owner.port}")
        self.assertTrue(post["headers"].get("idempotency-key"))
        record = json.loads(out)
        self.assertEqual(record, {k: v for k, v in self.owner.commits[0].items() if k in LEGACY_KEYS})
        self.assertEqual((record["created"], record["updated_at"]), ("2001-02-03", "2001-02-04"))
        self.assertEqual(self.planning_bytes(), self.initial)

    def test_omitted_quarter_sends_empty_string_and_keeps_server_value(self):
        code, out, err = self.cli("Default")
        self.contacted()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.owner.posts[0]["body"]), {"objective": "Default", "quarter": ""})
        self.assertEqual(json.loads(out)["quarter"], "2001-Q1")

    def test_identical_invocations_have_distinct_keys(self):
        for _ in range(2):
            code, _out, err = self.cli("Identical")
            self.contacted()
            self.assertEqual(code, 0, err)
        posts = self.owner.posts
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0]["body"], posts[1]["body"])
        keys = [p["headers"].get("idempotency-key") for p in posts]
        self.assertTrue(all(keys))
        self.assertNotEqual(*keys)
        self.assertEqual(len(self.owner.commits), 2)

    def test_lost_after_commit_replays_same_bytes_and_key_once(self):
        self.owner.script = ["commit-drop"]
        code, out, err = self.cli("Lost response")
        self.contacted()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.owner.posts), 2)
        first, second = self.owner.posts
        self.assertEqual(first["body"], second["body"])
        self.assertTrue(first["headers"].get("idempotency-key"))
        self.assertEqual(first["headers"]["idempotency-key"], second["headers"]["idempotency-key"])
        self.assertEqual(len(self.owner.commits), 1)
        self.assertEqual(set(json.loads(out)), LEGACY_KEYS)
        self.assertEqual(self.planning_bytes(), self.initial)

    def test_second_loss_reports_objective_commit_unknown_without_local_write(self):
        self.owner.script = ["commit-drop", "drop"]
        result = self.cli("Uncertain")
        self.contacted()
        self.assert_refusal(result, self.initial)
        self.assertIn("objective", result[2].lower())
        self.assertIn("unknown", result[2].lower())
        self.assertEqual(len(self.owner.posts), 2)
        self.assertEqual(len(self.owner.commits), 1)

    def test_truncated_http_response_replays_and_then_reports_unknown(self):
        self.owner.script = ["commit-truncate", "commit-truncate"]
        result = self.cli("Truncated")
        self.contacted()
        self.assert_refusal(result, self.initial)
        self.assertIn("unknown", result[2].lower())
        self.assertEqual(len(self.owner.posts), 2)
        self.assertEqual(len(self.owner.commits), 1)

    def test_invalid_json_after_commit_is_replayed_once(self):
        self.owner.script = ["commit-invalid"]
        code, _out, err = self.cli("JSON loss")
        self.contacted()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.owner.posts), 2)
        self.assertEqual(len(self.owner.commits), 1)
        self.assertEqual(self.planning_bytes(), self.initial)

    def test_determinate_error_has_no_retry_and_redacts_server_details(self):
        self.owner.script = [500]
        result = self.cli("Determinate")
        self.contacted()
        self.assert_refusal(result, self.initial)
        self.assertEqual(len(self.owner.posts), 1)
        for value in (DIAGNOSTIC_CANARY, INTERNAL_PATH, str(self.paths["runtime"]), self.owner.payloads[SESSION]["data"]["csrf_token"]):
            self.assertNotIn(value, result[1] + result[2])

    def test_preflight_error_is_not_retried(self):
        self.owner.get_errors[SESSION] = 503
        result = self.cli("Preflight fails")
        self.contacted()
        self.assert_refusal(result, self.initial)
        self.assertEqual(len(self.owner.requests), 1)
        self.assertEqual(self.owner.posts, [])

    def test_workspace_mismatch_refuses_before_post(self):
        self.owner.payloads[STORAGE]["data"]["workspace_id"] = str(uuid.uuid4())
        result = self.cli("Mismatch")
        self.contacted()
        self.assert_refusal(result, self.initial)
        self.assertEqual(self.owner.posts, [])

    def test_not_in_sync_refuses_before_post(self):
        self.owner.payloads[SYNC]["data"]["state"] = "external-change-detected"
        result = self.cli("Not ready")
        self.contacted()
        self.assert_refusal(result, self.initial)
        self.assertEqual(self.owner.posts, [])

    def test_advertisement_disappearance_before_post_refuses(self):
        changed = []
        def remove(path):
            if path == SYNC:
                self.store.server_info_path.unlink()
                changed.append(True)
        self.owner.after_get = remove
        result = self.cli("Vanished")
        self.contacted()
        self.assertEqual(changed, [True])
        self.assert_refusal(result, self.initial)
        self.assertEqual(self.owner.posts, [])

    def test_advertisement_replacement_before_post_refuses(self):
        changed = []
        def replace(path):
            if path == SYNC:
                data = json.loads(self.store.server_info_path.read_bytes())
                data["replacement_marker"] = "different advertisement"
                self.store.server_info_path.write_text(json.dumps(data), encoding="utf-8")
                changed.append(True)
        self.owner.after_get = replace
        result = self.cli("Replaced")
        self.contacted()
        self.assertEqual(changed, [True])
        self.assert_refusal(result, self.initial)
        self.assertEqual(self.owner.posts, [])

    def test_advertisement_growth_before_post_refuses(self):
        changed = []
        def grow(path):
            if path == SYNC:
                data = json.loads(self.store.server_info_path.read_bytes())
                data["padding"] = "x" * 65536
                self.store.server_info_path.write_text(json.dumps(data), encoding="utf-8")
                changed.append(True)
        self.owner.after_get = grow
        result = self.cli("Grown")
        self.contacted()
        self.assertEqual(changed, [True])
        self.assert_refusal(result, self.initial)
        self.assertEqual(self.owner.posts, [])

    def test_empty_text_keeps_legacy_diagnostic_without_network(self):
        result = self.cli(" \t ")
        self.assert_refusal(result, self.initial)
        self.assertIn("objective is required", result[2])
        self.assertEqual(self.owner.requests, [])


class ObjectiveInvalidOwnerContract(_Case):
    def test_directory_advertisement_refuses_without_cleanup(self):
        info = self.store.server_info_path
        info.mkdir()
        before = self.planning_bytes()
        result = self.cli("Directory")
        self.assert_refusal(result, before)
        self.assertTrue(info.is_dir())

    def test_malformed_empty_and_oversized_advertisements_refuse(self):
        owner = self.wire_owner()
        for raw in (b"", b"{broken", json.dumps({"version": 1, "host": "127.0.0.1", "port": owner.port, "padding": "x" * 65536}).encode()):
            with self.subTest(size=len(raw)):
                self.store.server_info_path.write_bytes(raw)
                before = self.planning_bytes()
                result = self.cli("Invalid advertisement")
                self.assert_refusal(result, before)
                self.assertEqual(self.store.server_info_path.read_bytes(), raw)
                self.assertEqual(owner.requests, [], "invalid metadata must refuse before HTTP")

    def test_strict_metadata_types_and_nonloopback_refuse(self):
        owner = self.wire_owner()
        cases = [{"host": []}, {"host": {}}, {"host": "192.0.2.1"}, {"version": True}, {"version": 1.0}, {"port": True}, {"port": "1"}]
        for override in cases:
            with self.subTest(override=override):
                raw = json.dumps({"version": 1, "host": "127.0.0.1", "port": owner.port, **override}).encode()
                self.store.server_info_path.write_bytes(raw)
                before = self.planning_bytes()
                result = self.cli("Wrong types")
                self.assert_refusal(result, before)
                self.assertEqual(self.store.server_info_path.read_bytes(), raw)
                self.assertEqual(owner.requests, [], "wrong types must refuse before HTTP")

    def test_unreadable_advertisement_refuses_at_filesystem_boundary(self):
        owner = self.wire_owner()
        info = self.store.server_info_path
        original_bytes = info.read_bytes()
        before = self.planning_bytes()
        real_builtin_open, real_io_open = open, io.open

        def guarded(original):
            def read_fault(path, *args, **kwargs):
                if not isinstance(path, int) and Path(path).resolve() == info.resolve():
                    raise PermissionError("fixture metadata read denied")
                return original(path, *args, **kwargs)
            return read_fault

        # Deterministic read fault, without changing ACLs or patching a product helper.
        with mock.patch("builtins.open", guarded(real_builtin_open)), mock.patch("io.open", guarded(real_io_open)):
            result = self.cli("Unreadable")
        self.assert_refusal(result, before)
        self.assertEqual(owner.requests, [])
        self.assertEqual(info.read_bytes(), original_bytes)

    def test_stale_unreachable_owner_refuses_without_local_fallback(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.store.write_server_info("127.0.0.1", port)
        raw = self.store.server_info_path.read_bytes()
        before = self.planning_bytes()
        self.assert_refusal(self.cli("Unreachable"), before)
        self.assertEqual(self.store.server_info_path.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()

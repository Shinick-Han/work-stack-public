"""Independent behavioural conformance for the frozen T-0002 note-writer contract.

Scope is deliberately only ``work-stack [--data-dir PATH] note TEXT [--link LINK]...``.

Design notes for the reviewer:

* Every case drives the real public entry point :func:`workstack.cli.main` (and once a
  real out-of-process CLI). Command selection and argument grammar are never replaced.
* Owner-aware cases point the owner metadata at a **scripted loopback HTTP server** that
  records the actual serialized request bytes and headers. Nothing here patches or
  asserts a project-private helper, so the suite constrains the wire contract rather
  than one implementation's internal call graph.
* Preflight fixture payloads are captured once from a real
  :func:`workstack.server.create_server` owner, so identity and readiness responses have
  the complete real shape; individual tests alter only the one field under test.
* ``WORK_STACK_RUNTIME`` and ``TEMP``/``TMP`` are redirected **before** any ``Store`` is
  constructed, because :attr:`Store.server_info_path` otherwise resolves under
  ``%LOCALAPPDATA%\\WorkStack\\runtime``. Set ``WORK_STACK_TEST_RESULT_ROOT`` to confine
  every fixture to a chosen results directory.

Owner-aware routing is expected to be RED against the pre-implementation baseline: the
``note`` domain still takes the exclusive-local Store path, so it either writes locally
while an owner is advertised or fails closed on the writer lease.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SESSION_PATH = "/api/v1/session"
STORAGE_PATH = "/api/v1/storage"
SYNC_PATH = "/api/v1/sync/status"
NOTES_PATH = "/api/v1/notes"
NOTE_KEYS = {"id", "text", "links", "created"}
LEAKY_DIAGNOSTIC_CANARY = "scripted-owner-secret-must-not-be-printed"
LEAKY_PATH = r"C:\scripted-owner\internal\path\must-not-be-printed"


def _result_root() -> Path | None:
    """Fixture root; confined to the assigned results directory when provided."""

    configured = os.environ.get("WORK_STACK_TEST_RESULT_ROOT")
    if configured:
        root = Path(configured) / "note-writer-fixtures"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return None


def _read_notes(root: Path) -> list[dict[str, Any]]:
    """Read the notes document without taking the writer lease."""

    document = json.loads((root / "notes.json").read_text(encoding="utf-8"))
    notes = document["notes"]
    assert isinstance(notes, list)
    return notes


class _IsolatedRuntimeCase(unittest.TestCase):
    """Redirect runtime and temporary storage BEFORE constructing any Store."""

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

        # Imported after the redirection so no Store can resolve a real runtime path.
        from workstack.service import WorkStack
        from workstack.store import Store

        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        self.workspace_uid = self.store.load("workspace.json")["id"]

    def _restore_environment(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        from workstack import cli

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--data-dir", str(self.root), "note", *arguments])
        return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Real preflight payload templates, captured once from a real owner.
# ---------------------------------------------------------------------------


_TEMPLATES: dict[str, dict[str, Any]] | None = None


def _capture_preflight_templates() -> dict[str, dict[str, Any]]:
    """Capture complete real payloads for the three preflight endpoints."""

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


# ---------------------------------------------------------------------------
# Scripted loopback owner
# ---------------------------------------------------------------------------


class _RecordedRequest:
    def __init__(self, method: str, path: str, headers: dict[str, str], body: bytes) -> None:
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body

    @property
    def idempotency_key(self) -> str | None:
        return self.headers.get("idempotency-key")


class _ScriptedOwner:
    """A loopback HTTP owner that records exact wire bytes and follows a script."""

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
        self._templates = templates
        self._next_id = 0
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
                """Abort the connection without any response line."""

                self.close_connection = True
                with contextlib.suppress(OSError):
                    self.connection.close()

            def _truncate(self, payload: dict[str, Any]) -> None:
                """Promise more bytes than are sent, then abort: an HTTP protocol error."""

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

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
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
                self._send(404, {"error": {"code": "not_found", "message": "no route"}})

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                recorded = self._record(body)
                if self.path != NOTES_PATH:
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
                    self._send(200, replayed)
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

    # -- payloads -------------------------------------------------------

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

    @property
    def csrf_token(self) -> str:
        return str(self._templates[SESSION_PATH]["data"]["csrf_token"])

    def commit(self, body: bytes, key: str | None) -> dict[str, Any]:
        parsed = json.loads(body.decode("utf-8")) if body else {}
        self._next_id += 1
        note = {
            "id": "N-{:04d}".format(self._next_id),
            "text": parsed.get("text"),
            "links": parsed.get("links", []),
            "created": "2026-09-02",
        }
        self.committed.append(note)
        payload = {"data": note, "meta": {"replayed": False}}
        if key:
            self.replies_by_key[key] = payload
        return payload

    # -- lifecycle ------------------------------------------------------

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def posts(self) -> list[_RecordedRequest]:
        return [r for r in self.requests if r.method == "POST" and r.path == NOTES_PATH]

    def paths(self, path: str) -> list[_RecordedRequest]:
        return [r for r in self.requests if r.path.split("?")[0] == path]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)


class _ScriptedOwnerCase(_IsolatedRuntimeCase):
    """Owner metadata advertises a scripted loopback owner on an ephemeral port."""

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
        self.store.write_server_info("127.0.0.1", self.owner.port)

    def assert_owner_was_contacted(self) -> None:
        self.assertTrue(
            self.owner.requests,
            "the note command never contacted the advertised owner on 127.0.0.1:{}; "
            "owner-aware routing is not implemented".format(self.owner.port),
        )

    def assert_no_local_note(self) -> None:
        self.assertEqual(
            _read_notes(self.root), [], "a local Store write happened despite an owner"
        )


# ---------------------------------------------------------------------------
# Owner-absent behaviour (must not change)
# ---------------------------------------------------------------------------


class NoteGrammarAndLocalContract(_IsolatedRuntimeCase):
    def test_owner_absent_note_writes_locally_and_prints_one_raw_note(self) -> None:
        code, out, err = self.run_cli("Ship the writer seam")

        self.assertEqual(code, 0, msg=err)
        payload = json.loads(out)
        self.assertIsInstance(payload, dict)
        self.assertEqual(set(payload), NOTE_KEYS)
        self.assertNotIn("data", payload)
        self.assertNotIn("meta", payload)
        self.assertEqual(payload["text"], "Ship the writer seam")
        self.assertEqual(len(_read_notes(self.root)), 1)

    def test_link_normalisation_and_repeated_flag_are_preserved(self) -> None:
        code, out, err = self.run_cli(
            "Linked note", "--link", "t-0002", "--link", " t-0001 ", "--link", "T-0002",
            "--link", "   ",
        )

        self.assertEqual(code, 0, msg=err)
        self.assertEqual(json.loads(out)["links"], ["T-0001", "T-0002"])

    def test_empty_text_is_refused_without_writing_a_note(self) -> None:
        code, _out, err = self.run_cli("   ")

        self.assertEqual(code, 2)
        self.assertNotEqual(err.strip(), "")
        self.assertEqual(_read_notes(self.root), [])

    def test_real_out_of_process_cli_matches_the_in_process_shape(self) -> None:
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
                "note",
                "Out of process",
            ],
            cwd=str(REPOSITORY_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(set(payload), NOTE_KEYS)
        self.assertEqual(payload["text"], "Out of process")


# ---------------------------------------------------------------------------
# A real Work Stack owner holds the writer lease
# ---------------------------------------------------------------------------


class NoteWriterRealOwnerContract(_IsolatedRuntimeCase):
    def setUp(self) -> None:
        super().setUp()
        from workstack.server import create_server

        self.server = create_server(self.stack, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        # Release the writer lease before the temporary tree is removed; Windows
        # refuses to unlink .workstack.lock while it is held.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        super().tearDown()

    def test_real_owner_note_succeeds_and_prints_one_raw_note(self) -> None:
        code, out, err = self.run_cli("Routed through the owner")

        self.assertEqual(code, 0, msg=err)
        payload = json.loads(out)
        self.assertEqual(set(payload), NOTE_KEYS)
        self.assertNotIn("meta", payload)
        self.assertEqual(payload["text"], "Routed through the owner")

    def test_real_owner_note_creates_exactly_one_note(self) -> None:
        before = len(_read_notes(self.root))

        code, _out, err = self.run_cli("Exactly one")

        self.assertEqual(code, 0, msg=err)
        self.assertEqual(len(_read_notes(self.root)), before + 1)

    def test_two_fresh_invocations_with_identical_arguments_create_two_notes(self) -> None:
        before = len(_read_notes(self.root))

        first_code, first_out, first_err = self.run_cli("Same words", "--link", "T-0001")
        second_code, second_out, second_err = self.run_cli("Same words", "--link", "T-0001")

        self.assertEqual(first_code, 0, msg=first_err)
        self.assertEqual(second_code, 0, msg=second_err)
        self.assertNotEqual(json.loads(first_out)["id"], json.loads(second_out)["id"])
        self.assertEqual(len(_read_notes(self.root)), before + 2)


# ---------------------------------------------------------------------------
# Wire contract against a scripted owner
# ---------------------------------------------------------------------------


class NoteWriterWireContract(_ScriptedOwnerCase):
    def test_post_carries_exact_body_session_csrf_and_correct_origin(self) -> None:
        code, out, err = self.run_cli("Header contract", "--link", "T-0003")

        self.assert_owner_was_contacted()
        self.assertEqual(code, 0, msg=err)
        posts = self.owner.posts
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            json.loads(posts[0].body.decode("utf-8")),
            {"text": "Header contract", "links": ["T-0003"]},
        )
        self.assertEqual(posts[0].headers.get("x-workstack-csrf"), self.owner.csrf_token)
        self.assertEqual(
            posts[0].headers.get("origin"), "http://127.0.0.1:{}".format(self.owner.port)
        )
        self.assertTrue(posts[0].idempotency_key)
        payload = json.loads(out)
        self.assertEqual(set(payload), NOTE_KEYS)
        self.assert_no_local_note()

    def test_each_new_invocation_uses_a_distinct_nonempty_key(self) -> None:
        first_code, _first_out, first_err = self.run_cli("Identical arguments")
        second_code, _second_out, second_err = self.run_cli("Identical arguments")

        self.assert_owner_was_contacted()
        self.assertEqual(first_code, 0, msg=first_err)
        self.assertEqual(second_code, 0, msg=second_err)
        posts = self.owner.posts
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].body, posts[1].body)
        keys = [post.idempotency_key for post in posts]
        self.assertTrue(all(keys))
        self.assertNotEqual(keys[0], keys[1], "a content-derived key collapses two intents")

    def test_lost_after_commit_replays_identical_bytes_and_key_without_duplicating(self) -> None:
        self.owner.script = ["commit-then-drop", "replay"]

        code, out, err = self.run_cli("Replayed once", "--link", "T-0001")

        self.assert_owner_was_contacted()
        self.assertEqual(code, 0, msg=err)
        posts = self.owner.posts
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].body, posts[1].body)
        self.assertTrue(posts[0].idempotency_key)
        self.assertEqual(posts[0].idempotency_key, posts[1].idempotency_key)
        self.assertEqual(len(self.owner.committed), 1)
        self.assertEqual(set(json.loads(out)), NOTE_KEYS)
        self.assert_no_local_note()

    def test_lost_before_commit_second_attempt_commits_exactly_once(self) -> None:
        self.owner.script = ["drop"]

        code, out, err = self.run_cli("Committed on retry")

        self.assert_owner_was_contacted()
        self.assertEqual(code, 0, msg=err)
        posts = self.owner.posts
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].idempotency_key, posts[1].idempotency_key)
        self.assertEqual(len(self.owner.committed), 1)
        self.assertEqual(set(json.loads(out)), NOTE_KEYS)
        self.assert_no_local_note()

    def test_second_ambiguous_failure_exits_two_with_commit_unknown(self) -> None:
        self.owner.script = ["drop", "drop"]

        code, out, err = self.run_cli("Twice uncertain")

        self.assert_owner_was_contacted()
        self.assertEqual(code, 2)
        self.assertEqual(len(self.owner.posts), 2)
        self.assertIn("unknown", err.casefold())
        self.assertEqual(out.strip(), "")
        self.assert_no_local_note()

    def test_http_protocol_exception_is_treated_as_ambiguous_not_a_crash(self) -> None:
        self.owner.script = ["commit-then-truncate", "commit-then-truncate"]

        code, out, err = self.run_cli("Truncated response")

        self.assert_owner_was_contacted()
        self.assertEqual(
            code,
            2,
            "a truncated HTTP response must exit 2, not raise an unhandled exception",
        )
        self.assertIn("unknown", err.casefold())
        self.assertEqual(out.strip(), "")
        self.assert_no_local_note()

    def test_determinate_http_error_is_not_retried(self) -> None:
        self.owner.script = [
            (400, {"error": {"code": "invalid_body", "message": "note body rejected"}})
        ]

        code, _out, _err = self.run_cli("Determinate failure")

        self.assert_owner_was_contacted()
        self.assertEqual(code, 2)
        self.assertEqual(len(self.owner.posts), 1)
        self.assert_no_local_note()

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

        code, out, err = self.run_cli("Secret hygiene")
        combined = out + err

        self.assert_owner_was_contacted()
        self.assertEqual(len(self.owner.posts), 1)
        self.assertNotEqual(code, 0)
        self.assertNotIn(LEAKY_DIAGNOSTIC_CANARY, combined)
        self.assertNotIn(LEAKY_PATH, combined)
        self.assertNotIn(self.owner.csrf_token, combined)
        self.assertNotIn(str(self.runtime), combined)


class NoteWriterIdentityAndReadinessContract(_ScriptedOwnerCase):
    def test_workspace_mismatch_blocks_the_write_before_any_post(self) -> None:
        self.owner.workspace_uid = "00000000-0000-4000-8000-000000000000"

        code, _out, err = self.run_cli("Mismatched workspace")

        self.assert_owner_was_contacted()
        self.assertEqual(code, 2)
        self.assertEqual(self.owner.posts, [])
        self.assertNotEqual(err.strip(), "")
        self.assert_no_local_note()

    def test_preflight_failure_is_not_retried(self) -> None:
        self.owner.workspace_uid = "00000000-0000-4000-8000-000000000000"

        self.run_cli("No preflight retry")

        self.assert_owner_was_contacted()
        self.assertLessEqual(len(self.owner.paths(SESSION_PATH)), 1)
        self.assertLessEqual(len(self.owner.paths(STORAGE_PATH)), 1)


class NoteWriterNotReadyContract(_ScriptedOwnerCase):
    sync_state = "external-change-detected"

    def test_not_in_sync_owner_blocks_the_write(self) -> None:
        code, _out, err = self.run_cli("Store not in sync")

        self.assert_owner_was_contacted()
        self.assertEqual(code, 2)
        self.assertEqual(self.owner.posts, [])
        self.assertNotEqual(err.strip(), "")
        self.assert_no_local_note()


class NoteWriterVanishingOwnerContract(_ScriptedOwnerCase):
    def test_metadata_removed_after_it_was_observed_never_falls_back(self) -> None:
        info = self.store.server_info_path
        self.assertTrue(info.is_file())
        original = info.read_bytes()

        # The owner deletes its own advertisement while the command is running.
        removed = threading.Event()

        original_session = self.owner.session_payload

        def vanishing_session() -> dict[str, Any]:
            with contextlib.suppress(OSError):
                info.unlink()
                removed.set()
            return original_session()

        self.owner.session_payload = vanishing_session  # type: ignore[method-assign]

        code, _out, err = self.run_cli("Owner vanished mid-flight")

        self.assert_owner_was_contacted()
        self.assertTrue(removed.is_set(), "the advertisement was never observed")
        self.assertNotEqual(code, 0, msg=err)
        self.assert_no_local_note()
        # Restore so tearDown's cleanup is not confused by a missing fixture file.
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(original)


# ---------------------------------------------------------------------------
# Unusable owner metadata must fail closed and must not be cleaned up
# ---------------------------------------------------------------------------


class NoteWriterUnusableOwnerMetadataContract(_IsolatedRuntimeCase):
    def _info_path(self) -> Path:
        info = self.store.server_info_path
        info.parent.mkdir(parents=True, exist_ok=True)
        return info

    def test_metadata_that_is_a_directory_fails_closed(self) -> None:
        info = self._info_path()
        info.mkdir()

        code, _out, err = self.run_cli("Owner metadata is a directory")

        self.assertEqual(
            code, 2, "a non-regular advertisement must fail closed, not write locally"
        )
        self.assertNotEqual(err.strip(), "")
        self.assertEqual(_read_notes(self.root), [])
        self.assertTrue(info.is_dir(), "owner metadata must not be cleaned up")

    def test_empty_metadata_file_fails_closed(self) -> None:
        info = self._info_path()
        info.write_bytes(b"")

        code, _out, _err = self.run_cli("Owner metadata is empty")

        self.assertEqual(code, 2)
        self.assertEqual(_read_notes(self.root), [])
        self.assertTrue(info.is_file())
        self.assertEqual(info.read_bytes(), b"")

    def test_malformed_metadata_fails_closed_and_is_not_cleaned_up(self) -> None:
        info = self._info_path()
        info.write_text("{not json", encoding="utf-8")

        code, _out, err = self.run_cli("Malformed owner metadata")

        self.assertEqual(code, 2)
        self.assertNotEqual(err.strip(), "")
        self.assertEqual(_read_notes(self.root), [])
        self.assertTrue(info.is_file(), "owner metadata must not be cleaned up")
        self.assertEqual(info.read_text(encoding="utf-8"), "{not json")

    def test_structurally_invalid_metadata_fails_closed(self) -> None:
        info = self._info_path()
        info.write_text(
            json.dumps({"version": 1, "host": "10.0.0.5", "port": 8765}), encoding="utf-8"
        )

        code, _out, _err = self.run_cli("Non-loopback owner metadata")

        self.assertEqual(code, 2)
        self.assertEqual(_read_notes(self.root), [])
        self.assertTrue(info.is_file())

    def test_stale_unreachable_owner_fails_closed_without_local_write(self) -> None:
        # Bind and immediately release an ephemeral port so nothing is listening.
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        self.store.write_server_info("127.0.0.1", dead_port)

        code, _out, err = self.run_cli("Stale owner")

        self.assertEqual(code, 2)
        self.assertNotEqual(err.strip(), "")
        self.assertEqual(_read_notes(self.root), [])
        self.assertTrue(self.store.server_info_path.is_file())


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()

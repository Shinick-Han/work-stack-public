"""Actual command-line planning-v2 parity across a real loopback HTTP owner.

R25 shipped its running-owner planning-v2 comparison against an in-process
requester double. That double answers where ``_HttpJsonRequester`` would, so
the committed evidence never crossed the untrusted response-admission boundary
between a real ``WorkStackHTTPServer`` and the runtime requester, and never ran
the supported command line at all. An independent probe crossed that boundary
by hand and passed; this module makes the same crossing a durable regression.

One synthetic store is built here, seeded and read:

1. ``run_work_stack.py agent ... context --view planning-v2`` runs as a real
   child process while no owner exists, so the answer is exclusive-local.
2. A real ephemeral-loopback ``WorkStackHTTPServer`` then takes the writer
   lease and publishes its runtime metadata, exactly as ``graph serve`` does.
3. The same command line runs again and is answered over real HTTP by that
   owner, plus one planning-v1 run to hold the legacy shape closed.

Nothing here mocks a requester or an HTTP response, and no network target
beyond 127.0.0.1 on an ephemeral port is ever contacted.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workstack.capture_retrieval import validate_retrieval_extension  # noqa: E402
from workstack.server import create_server  # noqa: E402
from workstack.service import WorkStack  # noqa: E402
from workstack.store import DEFAULTS, Store  # noqa: E402

ENTRYPOINT = ROOT / "run_work_stack.py"
CAPTURE_PACKET = ROOT / "contracts" / "capture-packet-v1.fixture.json"
HOST = "127.0.0.1"
ENVELOPE_BYTE_LIMIT = 32768

# A bounded SYNTHETIC v1.1 retrieval seed. It is not a live answer, no engine
# is contacted, and every handle below is a made-up canary whose appearance in
# agent output would be a leak. The record is admitted by the shipped v1.1
# validator before it is stored, so the seed cannot drift from the contract.
SEED_REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SEED_RETRIEVAL = {
    "schema": "workstack.capture-retrieval.v1.1",
    "capture_schema_version": "1.1",
    "request_id": SEED_REQUEST_ID,
    "query_id": "engine-q-00194f5a",
    "answer_scope": "single_source",
    "confidence": {"level": "medium", "score": 0.62},
    "evidence": [
        {
            "source_type": "notion.page",
            "title": "Synthetic release decision",
            "document_ref": "od-page-7f3ba1d34f50c884600112ab",
            "chunk_ref": "chunk-0004abcd",
            "source_version": "od-version-14",
            "indexed_digest": "sha256:" + "a" * 64,
            "web_url": None,
        }
    ],
    "truncated": False,
}
# Document, chunk, query, version, digest and request identifiers, none of
# which the five-field evidence projection is allowed to carry outward.
CANARIES = (
    SEED_RETRIEVAL["query_id"],
    SEED_RETRIEVAL["request_id"],
    SEED_RETRIEVAL["evidence"][0]["document_ref"],
    SEED_RETRIEVAL["evidence"][0]["chunk_ref"],
    SEED_RETRIEVAL["evidence"][0]["source_version"],
    SEED_RETRIEVAL["evidence"][0]["indexed_digest"],
)
EXPECTED_EVIDENCE = {
    "answer_scope": "single_source",
    "attested": False,
    "confidence_level": "medium",
    "evidence_count": 1,
    "truncated": False,
}


def _document_hashes(root: Path) -> dict[str, str]:
    """Digest every store document that exists, runtime metadata excluded."""

    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in sorted(DEFAULTS)
        if (root / name).is_file()
    }


class ActualOwnerPlanningV2Test(unittest.TestCase):
    """One synthetic store, one real owner, the real command line three times.

    The whole crossing runs once in ``setUpClass``; each test then states one
    property of the captured envelopes, so a failure names the broken property
    without paying for another owner.
    """

    @classmethod
    def setUpClass(cls) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ws-r28-owner-v2-")
        # Registered first, so it runs last: the owner releases its lease and
        # its socket before the fixture-owned directory is removed.
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name).resolve()

        store = Store(cls.root)
        cls.workspace_uid = store.initialize().workspace_uid
        stack = WorkStack(store)
        cls.task_id = stack.add_task("R28 synthetic knowledge Task")["id"]

        packet = json.loads(CAPTURE_PACKET.read_text(encoding="utf-8"))
        capture = stack.ingest_capture(packet, "r28.owner.ingest")["body"]["data"]
        stack.link_capture(capture["id"], cls.task_id, "r28.owner.link")

        # The seed is admitted by the production v1.1 boundary before it is
        # persisted, then written through Store while no owner is running.
        validate_retrieval_extension(SEED_RETRIEVAL, request_id=SEED_REQUEST_ID)
        with store.transaction():
            captures = store.load("captures.json")
            record = next(
                item for item in captures["captures"] if item["id"] == capture["id"]
            )
            record["schema_version"] = "1.1"
            record["retrieval"] = json.loads(json.dumps(SEED_RETRIEVAL))
            store.save("captures.json", captures)

        cls.hashes_before = _document_hashes(cls.root)

        # No owner exists yet, so this run must take the exclusive-local route.
        cls.local_raw = cls._run_context("planning-v2")
        cls.local = json.loads(cls.local_raw)
        cls.local_json_flag = cls._run_context("planning-v2", output_format="json")
        cls.local_md = cls._run_context("planning-v2", output_format="markdown")

        server = create_server(stack, HOST, 0)
        cls.owner_port = server.actual_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)

        def stop_owner() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=30)

        # Registered after the directory, so it runs first on success or failure.
        cls.addClassCleanup(stop_owner)
        thread.start()

        cls.remote = cls._context("planning-v2")
        cls.legacy = cls._context("planning-v1")
        cls.remote_md = cls._run_context("planning-v2", output_format="markdown")
        cls.legacy_md = cls._run_context("planning-v1", output_format="markdown")
        cls.core_md = cls._run_context("core-v1", output_format="markdown")
        cls.hashes_after = _document_hashes(cls.root)
        cls.capture_id = capture["id"]

    @classmethod
    def _run_context(cls, view: str, *, output_format: str | None = None) -> str:
        argv = [
            sys.executable,
            "-B",
            str(ENTRYPOINT),
            "--data-dir",
            str(cls.root),
            "agent",
            "--workspace-uid",
            cls.workspace_uid,
            "context",
            "--task",
            cls.task_id,
            "--view",
            view,
        ]
        if output_format is not None:
            argv.extend(["--format", output_format])
        completed = subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        if completed.returncode != 0:
            raise AssertionError(
                "agent context --view {} format {} exited {}: {!r} {!r}".format(
                    view, output_format, completed.returncode, completed.stdout, completed.stderr
                )
            )
        return completed.stdout

    @classmethod
    def _context(cls, view: str) -> dict[str, object]:
        """Run one supported `agent context` command line as a real child."""

        return json.loads(cls._run_context(view))

    # -- the real boundary -------------------------------------------------
    def test_the_first_run_is_local_and_the_second_is_the_http_owner(self) -> None:
        self.assertIsNone(self.local.get("error"))
        self.assertIsNone(self.remote.get("error"))
        self.assertEqual("exclusive-local", self.local["meta"]["transport"])
        self.assertEqual("running-server", self.remote["meta"]["transport"])
        # An ephemeral loopback port, never the conventional fixed one.
        self.assertNotEqual(8765, self.owner_port)
        self.assertGreater(self.owner_port, 0)

    def test_the_owner_answer_equals_the_local_answer(self) -> None:
        self.assertEqual(self.local["data"], self.remote["data"])

    def test_both_routes_declare_the_planning_v2_view(self) -> None:
        self.assertEqual("planning-v2", self.local["data"]["view"])
        self.assertEqual("planning-v2", self.remote["data"]["view"])

    def test_the_one_linked_retrieval_becomes_one_source(self) -> None:
        self.assertEqual(1, len(self.local["data"]["sources"]))
        self.assertEqual(1, len(self.remote["data"]["sources"]))

    def test_evidence_is_the_five_expected_fields_with_literal_false(self) -> None:
        for label, envelope in (("local", self.local), ("remote", self.remote)):
            with self.subTest(route=label):
                evidence = envelope["data"]["sources"][0]["evidence"]
                self.assertEqual(EXPECTED_EVIDENCE, evidence)
                self.assertIs(False, evidence["attested"])

    def test_planning_v1_over_the_same_owner_gains_no_key(self) -> None:
        self.assertEqual("running-server", self.legacy["meta"]["transport"])
        self.assertNotIn("view", self.legacy["data"])
        self.assertNotIn("evidence", self.legacy["data"]["sources"][0])

    def test_no_document_chunk_query_or_version_handle_reaches_output(self) -> None:
        for label, envelope in (
            ("local", self.local),
            ("remote", self.remote),
            ("legacy", self.legacy),
        ):
            text = json.dumps(envelope, ensure_ascii=False)
            for canary in CANARIES:
                with self.subTest(route=label, canary=canary):
                    self.assertNotIn(canary, text)

    def test_the_owner_envelope_stays_inside_the_frozen_byte_bound(self) -> None:
        for label, envelope in (("local", self.local), ("remote", self.remote)):
            with self.subTest(route=label):
                serialized = json.dumps(
                    envelope, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                self.assertLessEqual(len(serialized), ENVELOPE_BYTE_LIMIT)

    def test_three_reads_leave_every_stored_document_byte_identical(self) -> None:
        self.assertEqual(self.hashes_before, self.hashes_after)
        self.assertIn("captures.json", self.hashes_before)

    def test_default_json_bytes_match_explicit_json_locally(self) -> None:
        self.assertEqual(self.local_raw, self.local_json_flag)

    def test_local_and_owner_markdown_are_identical_for_equal_data(self) -> None:
        self.assertEqual(self.local_md, self.remote_md)
        self.assertTrue(self.local_md.startswith("# Resume brief\n"))
        self.assertIn("## Recent worklog", self.local_md)
        self.assertIn("preceding 30 days", self.local_md)
        self.assertIn("not a selected or latest checkpoint", self.local_md)
        self.assertIn("### {}".format(self.capture_id), self.local_md)
        self.assertIn("- Evidence attested: false", self.local_md)
        self.assertNotIn("## Selected references", self.local_md)
        self.assertLessEqual(len(self.local_md.encode("utf-8")), ENVELOPE_BYTE_LIMIT)
        for canary in CANARIES:
            self.assertNotIn(canary, self.local_md)

    def test_all_three_views_emit_markdown_through_the_live_owner(self) -> None:
        self.assertIn("Capture sources are not included by this view.", self.core_md)
        self.assertNotIn("No linked Capture sources included.", self.core_md)
        self.assertNotIn("### {}".format(self.capture_id), self.core_md)
        self.assertIn("### {}".format(self.capture_id), self.legacy_md)
        self.assertNotIn("Evidence attested", self.legacy_md)
        self.assertIn("- Evidence attested: false", self.remote_md)
        self.assertNotIn("## Selected references", self.core_md)
        self.assertNotIn("## Selected references", self.legacy_md)


if __name__ == "__main__":  # pragma: no cover - direct invocation convenience
    unittest.main()

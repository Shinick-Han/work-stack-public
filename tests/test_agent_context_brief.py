"""Opt-in `agent context --format markdown` and shared source fixtures.

Synthetic authorities and Capture rows only. No live SSOT, provider or
vault. Real CLI entry for owner-alive Markdown lives in
``test_agent_context_owner_v2``.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workstack.agent_authority import admit_authority
from workstack.agent_cli_contract import AgentOutcome, RuntimeDependencies, render_outcome
from workstack.agent_context_brief import (
    BRIEF_MAX_BYTES,
    CORE_CAPTURE_OMITTED,
    EMPTY_CATALOG,
    SOURCES_OVERFLOW_COPY,
    WORKLOG_INTRO,
    BriefTooLarge,
    format_context_brief,
    render_context_brief,
)
from workstack.agent_context_pack import build_planning_blocks
from workstack.agent_local_backend import create_local_backend
from workstack.agent_runtime import run_agent_command
from workstack.agent_transport import create_running_server_backend
from workstack.cli import parser as build_parser
from workstack.service import WorkStack
from workstack.store import Store


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts" / "capture-brief-sources-v1" / "cases.json"
ENTRYPOINT = ROOT / "run_work_stack.py"
WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
TODAY = dt.date(2026, 9, 8)
# 8 characters, 20 UTF-8 bytes: a character-length bound cannot pass for a
# byte bound once this sits inside the document.
MULTIBYTE_DETAIL = "요약 ✅ 브리핑"
OVERSIZE_TITLE = "R39 oversize markdown Task"

_OWNER_HOLDER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from workstack.file_lease import _FileLease
lease = _FileLease(Path(sys.argv[1]))
lease.acquire()
print("held", flush=True)
sys.stdin.readline()
lease.release()
"""


def _fixtures() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _selected(task_id: str) -> dict[str, object]:
    return {
        "id": task_id,
        "status": "started",
        "title": "Fixture",
        "detail": "bounded",
        "due": None,
        "priority": "P1",
        "revision": 1,
        "uid": "22222222-2222-4222-8222-222222222222",
        "parent_id": None,
        "dependencies": [],
        "objective_ids": [],
    }


def _core_data(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "workspace_uid": WORKSPACE_UID,
        "task": {
            "detail": "bounded fixture detail",
            "due": None,
            "id": "T-0001",
            "priority": "P1",
            "revision": 4,
            "status": "started",
            "title": "Implement authority preflight",
            "uid": "22222222-2222-4222-8222-222222222222",
        },
        "recent_worklog": [
            {
                "blockers": [],
                "date": "2026-09-02",
                "done": ["Designed the admission flow."],
                "next": ["Wire the preflight into the runtime."],
            }
        ],
        "omitted": [
            "attachments",
            "captures",
            "objectives",
            "relationships",
            "work_sessions",
        ],
    }
    data.update(changes)
    return data


def _detail_data(detail: str) -> dict[str, object]:
    """`_core_data` with one trusted Task detail replaced."""

    data = _core_data()
    task = dict(data["task"])  # type: ignore[arg-type]
    task["detail"] = detail
    data["task"] = task
    return data


def _admitted_outcome(data: dict[str, object]) -> AgentOutcome:
    """The context success shape existing envelope validation admits."""

    return AgentOutcome(
        command="agent.context",
        commit_state=None,
        data=data,
        error_code=None,
        error_details={},
        error_message=None,
        intent_id=None,
        replayed=None,
        retryable=None,
        task_id="T-0001",
        transport="exclusive-local",
        workspace_uid=WORKSPACE_UID,
    )


def _detail_for_brief_bytes(target: int) -> str:
    """A multibyte detail whose rendered brief is exactly `target` UTF-8 bytes.

    The padding character is ASCII and is not a backtick, so each one adds
    exactly one byte and cannot widen a fence. The overhead is measured
    through the public formatter instead of restating its layout here.
    """

    overhead = len(format_context_brief(data=_detail_data(MULTIBYTE_DETAIL)).encode("utf-8"))
    padding = target - overhead
    if padding < 0:
        raise AssertionError("brief overhead already exceeds the target")
    return MULTIBYTE_DETAIL + "x" * padding


def _release_holder(holder: subprocess.Popen) -> None:
    """Stop an owned child and close its pipes on success and on failure.

    Registered as a cleanup so an assertion or exception between spawn and
    graceful release still reaps the process and closes every pipe wrapper.
    """

    try:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)
    finally:
        for stream in (holder.stdin, holder.stdout, holder.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class ParserTest(unittest.TestCase):
    def test_format_defaults_to_json_and_does_not_change_the_view(self) -> None:
        parsed = build_parser().parse_args(
            ["--data-dir", "/workstack-fixture/authority", "agent", "context", "--task", "T-0001"]
        )
        self.assertEqual(parsed.view, "core-v1")
        self.assertEqual(parsed.context_format, "json")

    def test_markdown_format_is_accepted_on_planning_v2(self) -> None:
        parsed = build_parser().parse_args(
            [
                "--data-dir", "/workstack-fixture/authority", "agent", "context",
                "--task", "T-0001", "--view", "planning-v2", "--format", "markdown",
            ]
        )
        self.assertEqual(parsed.view, "planning-v2")
        self.assertEqual(parsed.context_format, "markdown")

    def test_unknown_format_is_parser_exit_2(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(
                [
                    "--data-dir", "/workstack-fixture/authority", "agent", "context",
                    "--task", "T-0001", "--format", "html",
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_status_does_not_gain_the_format_flag(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(
                [
                    "--data-dir", "/workstack-fixture/authority", "agent", "status",
                    "--format", "markdown",
                ]
            )
        self.assertEqual(raised.exception.code, 2)


class FormatterTest(unittest.TestCase):
    def test_core_view_states_sources_are_omitted_and_does_not_claim_empty(self) -> None:
        markdown = format_context_brief(data=_core_data())
        self.assertTrue(markdown.startswith("# Resume brief\n"))
        self.assertIn(WORKLOG_INTRO, markdown)
        self.assertIn(CORE_CAPTURE_OMITTED, markdown)
        self.assertNotIn(EMPTY_CATALOG, markdown)
        self.assertNotIn("## Selected references", markdown)
        self.assertIn("Named omitted markers: attachments, captures, objectives, relationships, work_sessions.", markdown)

    def test_planning_v2_empty_sources_is_a_legitimate_empty_catalog(self) -> None:
        data = _core_data(
            omitted=[
                "actions",
                "attachments",
                "capture_bodies",
                "capture_locators",
                "notes",
                "provenance",
                "work_sessions",
            ],
            sources=[],
            view="planning-v2",
        )
        markdown = format_context_brief(data=data)
        self.assertIn(EMPTY_CATALOG, markdown)
        self.assertNotIn(CORE_CAPTURE_OMITTED, markdown)
        self.assertNotIn("Evidence attested", markdown)

    def test_planning_v1_does_not_invent_evidence(self) -> None:
        data = _core_data(
            omitted=["actions", "attachments", "capture_bodies", "capture_locators", "notes", "provenance", "work_sessions"],
            sources=[{
                "display_title": "Reviewed capture title",
                "id": "C-0001",
                "link_reasons": ["capture-link"],
                "provider": "manual",
                "resource_type": "message",
                "status": "linked",
            }],
        )
        markdown = format_context_brief(data=data)
        self.assertIn("### C-0001", markdown)
        self.assertNotIn("Evidence attested", markdown)

    def test_planning_v2_prints_literal_attested_false_and_overflow_without_a_count(self) -> None:
        data = _core_data(
            omitted=[
                "actions",
                "attachments",
                "capture_bodies",
                "capture_locators",
                "notes",
                "provenance",
                "work_sessions",
                "sources_overflow",
                "recent_worklog_overflow",
            ],
            sources=[{
                "display_title": "Reviewed capture title",
                "evidence": {
                    "answer_scope": "single_source",
                    "attested": False,
                    "confidence_level": "medium",
                    "evidence_count": 1,
                    "truncated": False,
                },
                "id": "C-0001",
                "link_reasons": ["capture-link"],
                "provider": "manual",
                "resource_type": "message",
                "status": "linked",
            }],
            view="planning-v2",
        )
        markdown = format_context_brief(data=data)
        self.assertIn("- Evidence attested: false", markdown)
        self.assertIn(SOURCES_OVERFLOW_COPY, markdown)
        self.assertNotIn("1 linked Capture source was omitted", markdown)
        self.assertIn("Additional worklog entries were omitted.", markdown)

    def test_hostile_title_stays_inside_a_widened_fence(self) -> None:
        hostile = "Safe title\n## Injected\n```\nspoof"
        markdown = format_context_brief(data=_core_data(task={
            "detail": "ok",
            "due": None,
            "id": "T-0001",
            "priority": "P1",
            "revision": 1,
            "status": "started",
            "title": hostile,
            "uid": "22222222-2222-4222-8222-222222222222",
        }))
        self.assertIn(hostile, markdown)
        self.assertGreater(markdown.index("## Injected"), markdown.index("````"))
        self.assertNotIn("## Injected\n", markdown.split("````", 1)[0])

    def test_oversize_markdown_refuses_instead_of_truncating(self) -> None:
        data = _core_data(task={
            "detail": "x" * 40000,
            "due": None,
            "id": "T-0001",
            "priority": "P1",
            "revision": 1,
            "status": "started",
            "title": "ok",
            "uid": "22222222-2222-4222-8222-222222222222",
        })
        with self.assertRaises(BriefTooLarge):
            render_context_brief(data=data)
        markdown = format_context_brief(data=data)
        self.assertGreater(len(markdown.encode("utf-8")), BRIEF_MAX_BYTES)


class BriefByteBoundaryTest(unittest.TestCase):
    """The frozen bound is 32768 UTF-8 bytes of document, not 32768 characters."""

    def test_exactly_the_bound_is_accepted_and_one_more_byte_is_refused(self) -> None:
        accepted = _detail_data(_detail_for_brief_bytes(BRIEF_MAX_BYTES))
        refused = _detail_data(_detail_for_brief_bytes(BRIEF_MAX_BYTES + 1))

        # Both payloads are contexts the existing envelope already admits, so a
        # refusal below is the Markdown bound and not an invalid context.
        for label, data in (("accepted", accepted), ("refused", refused)):
            with self.subTest(payload=label):
                envelope = render_outcome(outcome=_admitted_outcome(data))
                self.assertLessEqual(len(envelope), BRIEF_MAX_BYTES)
                self.assertEqual(json.loads(envelope)["data"], data)

        raw = render_context_brief(data=accepted)
        self.assertEqual(len(raw), BRIEF_MAX_BYTES)
        self.assertEqual(raw[-1:], b"\n")
        self.assertEqual(raw.decode("utf-8"), format_context_brief(data=accepted))

        with self.assertRaises(BriefTooLarge):
            render_context_brief(data=refused)

    def test_the_boundary_documents_would_pass_a_character_length_gate(self) -> None:
        # Regression guard: if the bound were measured in characters, both
        # documents above would be accepted, because the multibyte detail keeps
        # every character count strictly under the byte count.
        for target in (BRIEF_MAX_BYTES, BRIEF_MAX_BYTES + 1):
            with self.subTest(target=target):
                document = format_context_brief(data=_detail_data(_detail_for_brief_bytes(target)))
                self.assertEqual(len(document.encode("utf-8")), target)
                self.assertLess(len(document), len(document.encode("utf-8")))
                self.assertLess(len(document), BRIEF_MAX_BYTES)
                self.assertIn(MULTIBYTE_DETAIL, document)


class FixtureConformanceTest(unittest.TestCase):
    def test_python_planning_v2_sources_match_the_shared_fixtures(self) -> None:
        payload = _fixtures()
        task_id = payload["task_id"]
        for case in payload["cases"]:
            with self.subTest(case=case["id"]):
                blocks, overflowed = build_planning_blocks(
                    task_id=task_id,
                    objectives=[],
                    tasks=[_selected(task_id)],
                    context=case["context"],
                    include_evidence=True,
                )
                self.assertEqual(blocks["sources"], case["expected_sources"])
                self.assertEqual("sources" in overflowed, case["cli_overflow"])
                rendered = json.dumps(blocks["sources"], ensure_ascii=False)
                self.assertNotIn("LEAK-BODY", rendered)
                self.assertNotIn("LEAK-REF", rendered)
                self.assertNotIn("engine-q-LEAK", rendered)
                self.assertNotIn("https://leak.example", rendered)


class _NullRequester:
    def request(self, **kwargs):
        raise AssertionError("markdown formatting must not issue HTTP")


def _dependencies(*, today=lambda: TODAY) -> RuntimeDependencies:
    return RuntimeDependencies(
        admit_authority=admit_authority,
        create_local_backend=create_local_backend,
        create_running_server_backend=create_running_server_backend,
        request_json=_NullRequester(),
        store_factory=lambda *, root: Store(root),
        today=today,
    )


class RuntimeFormatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ws-r36-brief-")
        self.root = Path(self.temporary.name)
        store = Store(self.root)
        self.workspace_uid = store.initialize().workspace_uid
        stack = WorkStack(store)
        self.task_id = stack.add_task("R36 markdown Task", detail="bounded detail")["id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _argv(self, *extra: str, task_id: str | None = None) -> list[str]:
        return [
            "--data-dir", str(self.root),
            "agent", "--workspace-uid", self.workspace_uid,
            "context", "--task", task_id if task_id is not None else self.task_id, *extra,
        ]

    def _oversize_task_id(self) -> str:
        """Store a Task whose planning-v2 Markdown crosses the 32768-byte bound.

        The padding is derived from the real projected envelope of this same
        synthetic Store, so the case stays a genuine crossing if authored copy
        changes instead of freezing a magic detail length.
        """

        code, rendered = self._run(self._argv("--view", "planning-v2"))
        self.assertEqual(code, 0)
        baseline = json.loads(rendered)["data"]
        probe = dict(baseline)
        probe["task"] = dict(baseline["task"], detail="x", title=OVERSIZE_TITLE)
        overhead = len(format_context_brief(data=probe).encode("utf-8")) - 1
        padding = BRIEF_MAX_BYTES - overhead + 64
        detail = MULTIBYTE_DETAIL + "x" * (padding - len(MULTIBYTE_DETAIL.encode("utf-8")))
        return WorkStack(Store(self.root)).add_task(OVERSIZE_TITLE, detail=detail)["id"]

    def _run(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        code = run_agent_command(
            args=build_parser().parse_args(argv),
            stdout=stdout,
            stderr=io.StringIO(),
            dependencies=_dependencies(),
        )
        return code, stdout.getvalue()

    def test_default_json_bytes_match_explicit_json(self) -> None:
        default_code, default_out = self._run(self._argv("--view", "planning-v2"))
        explicit_code, explicit_out = self._run(
            self._argv("--view", "planning-v2", "--format", "json")
        )
        self.assertEqual(default_code, 0)
        self.assertEqual(explicit_code, 0)
        self.assertEqual(default_out, explicit_out)
        envelope = json.loads(default_out)
        self.assertEqual(envelope["contract"], "workstack.cli.v1")
        self.assertEqual(envelope["data"]["view"], "planning-v2")

    def test_markdown_replaces_json_after_validation_for_all_three_views(self) -> None:
        for view in ("core-v1", "planning-v1", "planning-v2"):
            with self.subTest(view=view):
                code, markdown = self._run(self._argv("--view", view, "--format", "markdown"))
                self.assertEqual(code, 0)
                self.assertTrue(markdown.startswith("# Resume brief\n"))
                self.assertIn(WORKLOG_INTRO, markdown)
                self.assertNotIn("## Selected references", markdown)
                if view == "core-v1":
                    self.assertIn(CORE_CAPTURE_OMITTED, markdown)
                    self.assertNotIn(EMPTY_CATALOG, markdown)
                else:
                    self.assertIn(EMPTY_CATALOG, markdown)
                    self.assertNotIn(CORE_CAPTURE_OMITTED, markdown)

    def test_oversize_markdown_is_context_too_large_json_with_no_partial_output(self) -> None:
        task_id = self._oversize_task_id()

        code, rendered = self._run(
            self._argv("--view", "planning-v2", "--format", "markdown", task_id=task_id)
        )
        self.assertEqual(code, 1)
        envelope = json.loads(rendered)
        self.assertEqual(envelope["contract"], "workstack.cli.v1")
        self.assertEqual(envelope["error"]["code"], "context_too_large")
        self.assertNotIn("data", envelope)
        self.assertNotIn("# Resume brief", rendered)
        self.assertNotIn("## Saved Task", rendered)
        self.assertNotIn(MULTIBYTE_DETAIL, rendered)

        json_code, json_rendered = self._run(self._argv("--view", "planning-v2", task_id=task_id))
        self.assertEqual(json_code, 0)
        data = json.loads(json_rendered)["data"]
        self.assertEqual(data["task"]["id"], task_id)
        self.assertLessEqual(len(json_rendered.encode("utf-8")), BRIEF_MAX_BYTES)
        self.assertGreater(len(format_context_brief(data=data).encode("utf-8")), BRIEF_MAX_BYTES)

    def test_owner_unavailable_markdown_stays_json_error_with_no_local_fallback(self) -> None:
        store = Store(self.root)
        store.write_server_info("127.0.0.1", 1)
        lock = self.root / ".workstack.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c", _OWNER_HOLDER, str(lock), str(ROOT)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(_release_holder, holder)
        line = holder.stdout.readline() if holder.stdout is not None else ""
        self.assertEqual(line.strip(), "held")
        hashes_before = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
            if path.is_file() and path.name != ".workstack.lock"
        }
        completed = subprocess.run(
            [
                sys.executable, "-B", str(ENTRYPOINT),
                "--data-dir", str(self.root),
                "agent", "--workspace-uid", self.workspace_uid,
                "context", "--task", self.task_id,
                "--view", "planning-v2", "--format", "markdown",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        if holder.stdin is not None:
            holder.stdin.write("\n")
            holder.stdin.close()
        holder.wait(timeout=30)
        self.assertEqual(completed.returncode, 1)
        envelope = json.loads(completed.stdout)
        self.assertEqual(envelope["error"]["code"], "owner_unavailable")
        self.assertNotIn("data", envelope)
        self.assertFalse(completed.stdout.startswith("# Resume brief"))
        hashes_after = {
            path.name: path.read_bytes()
            for path in self.root.iterdir()
            if path.is_file() and path.name != ".workstack.lock"
        }
        self.assertEqual(hashes_before, hashes_after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

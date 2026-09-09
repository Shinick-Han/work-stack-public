"""The pure checkpoint-facts core: selection, refusals and the frozen output.

The GUI is the oracle. ``contracts/checkpoint-facts-v1/cases.json`` holds the
hand-written expected documents, and
``frontend/src/features/tasks/checkpointFacts.conformance.test.ts`` runs the
same cases through ``selectTaskResumeFacts``, so a divergence between the two
implementations fails on one side or the other rather than shipping.

Everything else here is what only the Python side owns: what it refuses, and
the bytes it produces.
"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from workstack.checkpoint_facts import (
    CHECKPOINT_FACTS_CONTRACT,
    FACTS_MAX_BYTES,
    CheckpointFactsError,
    project_checkpoint_facts,
    render_checkpoint_facts,
    validate_checkpoint_request,
)
from workstack.checkpoint_facts_format import FACTS_FIELDS, PROVENANCE_FIELDS

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "contracts" / "checkpoint-facts-v1" / "cases.json"
CORE = ROOT / "workstack" / "checkpoint_facts.py"
FORMAT = ROOT / "workstack" / "checkpoint_facts_format.py"

WORKSPACE = "123e4567-e89b-42d3-a456-426614174000"
OTHER_WORKSPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TASK = "T-0033"
DIGEST = "sha256:" + "a" * 64
# U+0085 is whitespace to Python's str.strip() and not to the oracle's trim().
# Written as a code point so no editor can normalize it into something else.
NEXT_LINE = chr(0x85)

CASE_IDS = (
    "empty-no-record-for-task",
    "latest-day-wins",
    "same-day-ordinal-order",
    "superseded-latest-excluded-but-counted",
    "every-record-superseded-is-empty",
    "unreadable-latest-never-falls-back",
    "legacy-free-text-entry-is-unreadable",
    "opaque-payload-counts-without-presenting",
    "partial-mixed-slots-and-unknown-field",
    "recorded-task-mismatch-is-partial",
    "legacy-entry-payload-binding-declared",
    "legacy-row-naming-another-task-is-not-borrowed",
    "foreign-workspace-and-task-ignored",
    "checkpoint-only-revision-changes-identity",
    "fence-and-utf8-canaries-preserved",
)


def _document() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _cases() -> dict[str, dict]:
    return {case["id"]: case for case in _document()["cases"]}


def _project(case: dict) -> dict:
    return project_checkpoint_facts(
        case["audit"],
        workspace_uid=case["workspace_uid"],
        task_id=case["task_id"],
    )


def _facts_for(case_id: str) -> dict:
    return _project(_cases()[case_id])


def _locator(**overrides: object) -> dict:
    locator = {
        "workspace_uid": WORKSPACE,
        "task_id": TASK,
        "date": "2026-09-05",
        "ordinal": 0,
        "entry_digest": DIGEST,
    }
    locator.update(overrides)
    return locator


def _entry(**overrides: object) -> dict:
    entry = {
        "locator": _locator(),
        "checkpoint_id": "CP-" + "a" * 64,
        "entry": {"task_id": TASK, "next": ["ship"]},
        "recorded": None,
        "state": "active",
        "revision": 0,
        "transitions": [],
    }
    entry.update(overrides)
    return entry


def _audit(*entries: dict, workspace_uid: str = WORKSPACE) -> dict:
    return {"workspace_uid": workspace_uid, "entries": list(entries)}


class SharedConformanceTest(unittest.TestCase):
    """The cases both implementations read, and the shape they agree on."""

    def test_cases_are_committed_json_read_by_both_implementations(self) -> None:
        document = _document()
        self.assertEqual(document["contract"], CHECKPOINT_FACTS_CONTRACT)
        self.assertEqual(tuple(case["id"] for case in document["cases"]), CASE_IDS)
        # Every case states what it is for, so a later reader can tell whether a
        # change is a fix or a regression.
        self.assertTrue(all(case["purpose"] for case in document["cases"]))

    def test_projection_matches_every_shared_expected_document(self) -> None:
        for case in _document()["cases"]:
            with self.subTest(case=case["id"]):
                facts = _project(case)
                self.assertEqual(facts, case["expected"])
                self.assertEqual(sorted(facts), sorted(FACTS_FIELDS))
                provenance = facts["provenance"]
                if provenance is not None:
                    self.assertEqual(sorted(provenance), sorted(PROVENANCE_FIELDS))
                # The renamed progress-adapter statuses are never produced here.
                self.assertIn(
                    facts["status"], ("empty", "unreadable", "partial", "ready")
                )

    def test_the_same_snapshot_always_produces_the_same_facts(self) -> None:
        case = _cases()["latest-day-wins"]
        self.assertEqual(_project(case), _project(case))

    def test_blank_slot_handling_follows_the_oracle_not_python_strip(self) -> None:
        facts = _facts_for("partial-mixed-slots-and-unknown-field")
        # str.strip() would drop this slot and report it as a leftover instead.
        self.assertEqual(facts["done"], ["drafted", NEXT_LINE])
        self.assertEqual(facts["next"], ["review"])
        self.assertEqual(facts["reasons"], ["unpresented_values"])

    def test_an_opaque_entry_payload_is_never_itself_a_refusal(self) -> None:
        # A malformed payload is a real recorded state, so it maps through the
        # summary semantics instead of failing the read.
        for payload in (None, 7, "free text", [], {"unknown": {"deep": [1, 2]}}, True):
            with self.subTest(payload=repr(payload)):
                facts = project_checkpoint_facts(
                    _audit(_entry(entry=payload)),
                    workspace_uid=WORKSPACE,
                    task_id=TASK,
                )
                self.assertEqual(facts["status"], "unreadable")
                self.assertIn("no_readable_summary", facts["reasons"])


class JsonRenderingTest(unittest.TestCase):
    def test_json_is_canonical_utf8_with_exactly_one_trailing_newline(self) -> None:
        for case_id in CASE_IDS:
            with self.subTest(case=case_id):
                facts = _facts_for(case_id)
                text = render_checkpoint_facts(facts)
                self.assertTrue(text.endswith("\n"))
                self.assertEqual(text.count("\n"), 1)
                self.assertLessEqual(len(text.encode("utf-8")), FACTS_MAX_BYTES)
                self.assertEqual(json.loads(text), facts)
                self.assertEqual(
                    text[:-1],
                    json.dumps(
                        facts,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                )

    def test_json_is_the_bare_facts_and_never_an_agent_cli_envelope(self) -> None:
        text = render_checkpoint_facts(_facts_for("latest-day-wins"))
        document = json.loads(text)
        self.assertEqual(document["contract"], "workstack.checkpoint-facts.v1")
        self.assertEqual(set(document), set(FACTS_FIELDS))
        self.assertNotIn("workstack.cli.v1", text)
        self.assertNotIn("meta", document)
        self.assertNotIn("data", document)

    def test_no_unpresented_value_reaches_either_rendering(self) -> None:
        for case_id in ("opaque-payload-counts-without-presenting",
                        "partial-mixed-slots-and-unknown-field"):
            facts = _facts_for(case_id)
            self.assertIn("unpresented_values", facts["reasons"])
            for form in ("json", "markdown"):
                with self.subTest(case=case_id, format=form):
                    text = render_checkpoint_facts(facts, format=form)
                    self.assertNotIn("unknown_shape", text)
                    self.assertNotIn("reviewer", text)
                    self.assertNotIn("unnamed", text)


def _headings_outside_fences(text: str) -> list[str]:
    """Lines Markdown would read as headings: fenced content is not one.

    A fence opened by ``fence`` is closed only by an identical run of
    backticks, so a shorter run INSIDE the block never ends it early.
    """

    headings: list[str] = []
    opener: str | None = None
    for line in text.splitlines():
        if opener is None:
            if len(line) >= 3 and set(line) == {"`"}:
                opener = line
            elif line.startswith("#"):
                headings.append(line)
        elif line == opener:
            opener = None
    assert opener is None, "an untrusted value left a fence open"
    return headings


class MarkdownRenderingTest(unittest.TestCase):
    def test_it_states_the_selected_record_and_fences_untrusted_text(self) -> None:
        text = render_checkpoint_facts(
            _facts_for("fence-and-utf8-canaries-preserved"), format="markdown"
        )
        self.assertTrue(text.startswith("# Resume checkpoint\n"))
        self.assertIn("\n## Selected checkpoint\n", text)
        self.assertIn("\n## Recorded progress\n", text)
        self.assertIn("- Task binding: locator", text)
        self.assertIn("- Record state: active", text)
        self.assertIn("- Recorded date: 2026-09-05", text)
        # Recent worklog is a different view and is never substituted for this
        # one, and nothing here asserts freshness.
        self.assertNotIn("Recent worklog", text)
        self.assertIn("not an attestation", text)
        for value in ("```\n# Not a heading", "- [ ] not a list", "`````",
                      "https://leak.example/path typed by the user", "``` 요약 ✅"):
            with self.subTest(value=value):
                self.assertIn(value, text)
        # The heading inside the payload is FENCED, so the only real
        # headings are the document's own.
        self.assertEqual(
            _headings_outside_fences(text),
            [
                "# Resume checkpoint",
                "## Selected checkpoint",
                "## Recorded progress",
                "### Done",
                "### Next",
                "### Blockers",
            ],
        )
        # ...and the payload's heading really is inside one.
        self.assertIn("# Not a heading", text)
        self.assertNotIn("# Not a heading", _headings_outside_fences(text))

    def test_fences_close_over_the_backticks_they_wrap(self) -> None:
        text = render_checkpoint_facts(
            _facts_for("fence-and-utf8-canaries-preserved"), format="markdown"
        )
        # A five-backtick blocker needs a six-backtick fence; three would end
        # the block early and let the rest of the record escape as markup.
        self.assertIn("``````\n`````\n``````", text)
        self.assertIn("````\n```\n# Not a heading\n- [ ] not a list\n````", text)

    def test_notices_are_truthful_about_what_was_not_read(self) -> None:
        empty = render_checkpoint_facts(
            _facts_for("empty-no-record-for-task"), format="markdown"
        )
        self.assertIn("No active checkpoint record for this Task.", empty)
        self.assertNotIn("## Selected checkpoint", empty)
        self.assertIn("No items recorded.", empty)

        unreadable = render_checkpoint_facts(
            _facts_for("unreadable-latest-never-falls-back"), format="markdown"
        )
        self.assertIn("No older record is shown in its place.", unreadable)
        self.assertIn("Nothing human-facing could be read", unreadable)
        # The older readable record's own text never appears.
        self.assertNotIn("older but readable", unreadable)

    def test_reason_descriptions_are_fixed_text_in_the_frozen_order(self) -> None:
        facts = _facts_for("opaque-payload-counts-without-presenting")
        self.assertEqual(
            facts["reasons"], ["unpresented_values", "no_readable_summary"]
        )
        text = render_checkpoint_facts(facts, format="markdown")
        counted = text.index("counted here and not reproduced")
        read = text.index("Nothing human-facing could be read")
        self.assertLess(counted, read)


class RequestRefusalTest(unittest.TestCase):
    def test_a_noncanonical_workspace_uid_is_refused(self) -> None:
        for workspace_uid in (
            "123E4567-E89B-42D3-A456-426614174000",
            "{123e4567-e89b-42d3-a456-426614174000}",
            "urn:uuid:123e4567-e89b-42d3-a456-426614174000",
            "00000000-0000-0000-0000-000000000000",
            "123e4567e89b42d3a456426614174000",
            "123e4567-e89b-42d3-a456-426614174000\n",
            WORKSPACE + " ",
            None,
            1,
        ):
            with self.subTest(workspace_uid=repr(workspace_uid)):
                with self.assertRaises(CheckpointFactsError) as caught:
                    validate_checkpoint_request(
                        workspace_uid=workspace_uid, task_id=TASK
                    )
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_a_malformed_task_id_is_refused(self) -> None:
        for task_id in ("T-33", "t-0033", "T-0033 ", "T-٠٠٣٣", "0033", "",
                        None, ["T-0033"]):
            with self.subTest(task_id=repr(task_id)):
                with self.assertRaises(CheckpointFactsError) as caught:
                    validate_checkpoint_request(
                        workspace_uid=WORKSPACE, task_id=task_id
                    )
                self.assertEqual(caught.exception.code, "invalid_request")

    def test_a_valid_request_is_accepted_and_reads_nothing(self) -> None:
        self.assertIsNone(
            validate_checkpoint_request(workspace_uid=WORKSPACE, task_id=TASK)
        )

    def test_a_request_is_validated_before_the_snapshot_is_read(self) -> None:
        with self.assertRaises(CheckpointFactsError) as caught:
            project_checkpoint_facts(
                "not an audit", workspace_uid="nope", task_id=TASK
            )
        self.assertEqual(caught.exception.code, "invalid_request")


def _malformed_audits() -> dict[str, object]:
    return {
        "not a mapping": [],
        "entries not a list": {"workspace_uid": WORKSPACE, "entries": {}},
        "entry not a mapping": _audit("row"),
        "unknown state": _audit(_entry(state="withdrawn")),
        "missing state": _audit(_entry(state=None)),
        "ordinal is a boolean": _audit(_entry(locator=_locator(ordinal=True))),
        "impossible calendar day": _audit(_entry(locator=_locator(date="2026-02-30"))),
        "date is not a day": _audit(_entry(locator=_locator(date="2026-09"))),
        "locator missing a field": _audit(
            _entry(locator={"workspace_uid": WORKSPACE, "task_id": TASK,
                            "date": "2026-09-05", "ordinal": 0})
        ),
        "locator carries an unknown field": _audit(
            _entry(locator=_locator(source_path="C:/live/worklog.json"))
        ),
        "malformed checkpoint identifier": _audit(_entry(checkpoint_id="CP-nope")),
        "malformed entry digest": _audit(
            _entry(locator=_locator(entry_digest="sha1:" + "a" * 40))
        ),
        "malformed recorded Task in the locator": _audit(
            _entry(locator=_locator(task_id="T-33"))
        ),
        "negative revision": _audit(_entry(revision=-1)),
        "revision is a boolean": _audit(_entry(revision=True)),
        "recorded fact is not a mapping": _audit(_entry(recorded="agent-cli-v1")),
        "recorded origin is not text": _audit(_entry(recorded={"origin": 7})),
        "row locator names another workspace spelling": _audit(
            _entry(locator=_locator(workspace_uid=WORKSPACE.upper()))
        ),
    }


class AuditRefusalTest(unittest.TestCase):
    def test_metadata_selection_depends_on_is_refused_not_guessed(self) -> None:
        for label, audit in _malformed_audits().items():
            with self.subTest(audit=label):
                with self.assertRaises(CheckpointFactsError) as caught:
                    project_checkpoint_facts(
                        audit, workspace_uid=WORKSPACE, task_id=TASK
                    )
                self.assertEqual(caught.exception.code, "invalid_audit")

    def test_a_snapshot_taken_for_another_workspace_is_refused(self) -> None:
        audit = _audit(_entry(), workspace_uid=OTHER_WORKSPACE)
        with self.assertRaises(CheckpointFactsError) as caught:
            project_checkpoint_facts(audit, workspace_uid=WORKSPACE, task_id=TASK)
        self.assertEqual(caught.exception.code, "invalid_audit")

    def test_unrelated_top_level_audit_fields_are_ignored(self) -> None:
        audit = _audit(_entry())
        audit["generated_at"] = "2026-09-09T00:00:00Z"
        audit["source_path"] = "C:/workspaces/private/worklog.json"
        facts = project_checkpoint_facts(
            audit, workspace_uid=WORKSPACE, task_id=TASK
        )
        self.assertEqual(
            facts,
            project_checkpoint_facts(
                _audit(_entry()), workspace_uid=WORKSPACE, task_id=TASK
            ),
        )
        text = render_checkpoint_facts(facts, format="markdown")
        self.assertNotIn("generated_at", text)
        self.assertNotIn("private", text)

    def test_an_entry_level_field_selection_ignores_is_not_serialized(self) -> None:
        # Transition history is real audit metadata this view deliberately does
        # not carry.
        audit = _audit(_entry(transitions=[{"reason": {"explanation": "LEAK"}}]))
        facts = project_checkpoint_facts(
            audit, workspace_uid=WORKSPACE, task_id=TASK
        )
        for form in ("json", "markdown"):
            with self.subTest(format=form):
                self.assertNotIn("LEAK", render_checkpoint_facts(facts, format=form))


def _facts_with(items: int, filler: str) -> dict:
    payload = {"task_id": TASK, "done": [filler * 1000 for _ in range(items)]}
    return project_checkpoint_facts(
        _audit(_entry(entry=payload)), workspace_uid=WORKSPACE, task_id=TASK
    )


class OutputBoundTest(unittest.TestCase):
    def test_an_oversize_document_fails_entirely_rather_than_truncating(self) -> None:
        facts = _facts_with(40, "x")
        self.assertEqual(len(facts["done"]), 40)
        for form in ("json", "markdown"):
            with self.subTest(format=form):
                with self.assertRaises(CheckpointFactsError) as caught:
                    render_checkpoint_facts(facts, format=form)
                self.assertEqual(caught.exception.code, "output_too_large")
                # The refusal carries no fragment of what it refused to print.
                self.assertNotIn("x" * 20, str(caught.exception))
                self.assertEqual(
                    str(caught.exception),
                    "the checkpoint facts exceed the 32768 byte output bound",
                )

    def test_a_document_just_inside_the_bound_is_still_rendered(self) -> None:
        raw = render_checkpoint_facts(_facts_with(30, "y")).encode("utf-8")
        self.assertLess(30000, len(raw))
        self.assertLessEqual(len(raw), FACTS_MAX_BYTES)


def _mutations() -> dict[str, object]:
    return {
        "a missing key": lambda facts: facts.pop("reasons"),
        "an added key": lambda facts: facts.update({"extra": 1}),
        "a progress-adapter status": lambda facts: facts.update({"status": "none"}),
        "the Agent CLI contract": lambda facts: facts.update(
            {"contract": "workstack.cli.v1"}
        ),
        "a nonstring bullet": lambda facts: facts.update({"done": ["ok", 2]}),
        "reasons out of order": lambda facts: facts.update(
            {"reasons": ["no_readable_summary", "unpresented_values"]}
        ),
        "an invented reason": lambda facts: facts.update({"reasons": ["invented"]}),
        "a negative count": lambda facts: facts.update({"active_record_count": -1}),
        "an invented binding": lambda facts: facts["provenance"].update(
            {"binding": "guessed"}
        ),
        "a provenance on empty": lambda facts: facts.update({"status": "empty"}),
    }


class FactsShapeRefusalTest(unittest.TestCase):
    def test_a_mapping_that_is_not_the_frozen_shape_is_refused(self) -> None:
        for label, mutate in _mutations().items():
            for form in ("json", "markdown"):
                with self.subTest(mutation=label, format=form):
                    facts = _facts_for("latest-day-wins")
                    mutate(facts)
                    with self.assertRaises(CheckpointFactsError) as caught:
                        render_checkpoint_facts(facts, format=form)
                    self.assertEqual(caught.exception.code, "invalid_facts")

    def test_an_unknown_output_format_is_refused(self) -> None:
        facts = _facts_for("latest-day-wins")
        for form in ("JSON", "html", "", "markdown ", "text"):
            with self.subTest(format=form):
                with self.assertRaises(CheckpointFactsError) as caught:
                    render_checkpoint_facts(facts, format=form)
                self.assertEqual(caught.exception.code, "invalid_facts")

    def test_a_json_decoded_unpaired_surrogate_is_a_safe_invalid_facts(self) -> None:
        # JSON accepts an escaped unpaired surrogate; the decoder yields a
        # Python str that is still an allowlisted recorded value. Rendering
        # must refuse it as invalid_facts, never as a raw codec error.
        audit = json.loads(
            "{"
            f'"workspace_uid":"{WORKSPACE}",'
            '"entries":[{'
            f'"locator":{{"workspace_uid":"{WORKSPACE}","task_id":"{TASK}",'
            '"date":"2026-09-05","ordinal":0,'
            f'"entry_digest":"{DIGEST}"}},'
            f'"checkpoint_id":"CP-{"a" * 64}",'
            '"entry":{"task_id":"T-0033","task":"\\ud800","next":["\\ud800"]},'
            '"recorded":{"origin":"\\ud800"},'
            '"state":"active","revision":0,"transitions":[]'
            "}]}"
        )
        unpaired = json.loads('"\\ud800"')
        facts = project_checkpoint_facts(
            audit, workspace_uid=WORKSPACE, task_id=TASK
        )
        self.assertEqual(facts["status"], "ready")
        self.assertEqual(facts["next"], [unpaired])
        self.assertEqual(facts["provenance"]["origin"], unpaired)
        self.assertEqual(facts["provenance"]["recorded_task_title"], unpaired)
        for form in ("json", "markdown"):
            with self.subTest(format=form):
                with self.assertRaises(CheckpointFactsError) as caught:
                    render_checkpoint_facts(facts, format=form)
                self.assertEqual(caught.exception.code, "invalid_facts")
                self.assertEqual(
                    str(caught.exception),
                    "the checkpoint facts could not be rendered",
                )
                self.assertNotIn(unpaired, str(caught.exception))
                self.assertNotIn("ud800", str(caught.exception))
                self.assertNotIn("position", str(caught.exception))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


class PurityTest(unittest.TestCase):
    def test_the_core_reaches_nothing_that_could_perform_io(self) -> None:
        self.assertEqual(
            _imported_modules(CORE),
            {"__future__", "re", "datetime", "typing", ".checkpoint_facts_format"},
        )
        self.assertEqual(
            _imported_modules(FORMAT),
            {"__future__", "typing", ".agent_context_brief", ".storage.canonical"},
        )
        for path in (CORE, FORMAT):
            source = path.read_text(encoding="utf-8")
            for forbidden in ("open(", "Path(", "subprocess", "urllib", "socket",
                              "requests", "datetime.now", "random", "environ"):
                with self.subTest(module=path.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

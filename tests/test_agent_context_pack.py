"""Contract for the opt-in `agent context --view planning-v1` projection.

Every fixture here is synthetic: `/workstack-fixture` roots, `T-`/`O-`/`C-`
identifiers and opaque canaries. No personal path, address or live authority
appears, and no test performs a network or provider action.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from workstack import agent_cli_contract as contract
from workstack import agent_context_pack as pack
from workstack.agent_cli_contract import AuthorityAdmission, ContextRequest
from workstack.agent_command_context import handle_context
from workstack.agent_local_backend import create_local_backend
from workstack.agent_transport import create_running_server_backend
from workstack.cli import parser as build_parser
from workstack.service import WorkStack
from workstack.store import Store


WORKSPACE_UID = "11111111-1111-4111-8111-111111111111"
TODAY = dt.date(2026, 9, 6)
HOST = "127.0.0.1"
PORT = 8765
# Opaque markers. If any of these reaches an envelope, an excluded field leaked.
CANARIES = (
    "CANARY-RAW-BODY-DO-NOT-STORE",
    "CANARY-LOCATOR-DO-NOT-STORE",
    "CANARY-PROVENANCE-DO-NOT-STORE",
    "CANARY-RECIPIENT-DO-NOT-STORE",
)


def _task(
    identifier: str,
    *,
    title: str = "Fixture Task",
    uid: str = "22222222-2222-4222-8222-222222222222",
    revision: int = 3,
    status: str = "started",
    parent_id: str | None = None,
    dependencies: tuple[str, ...] = (),
    objective_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "detail": "bounded fixture detail",
        "dependencies": list(dependencies),
        "due": None,
        "id": identifier,
        "objective_ids": list(objective_ids),
        "parent_id": parent_id,
        "priority": "P1",
        "revision": revision,
        "status": status,
        "title": title,
        "uid": uid,
    }


def _objective(
    identifier: str,
    *,
    title: str = "Fixture Objective",
    quarter: str | None = "2026-Q3",
    status: str = "active",
) -> dict[str, object]:
    return {
        "id": identifier,
        "objective": title,
        "quarter": quarter,
        "status": status,
        # Never rendered; present so an extra field cannot leak.
        "key_results": [{"id": "KR-1", "text": CANARIES[0]}],
    }


def _capture(
    identifier: str,
    *,
    task_id: str,
    reasons: tuple[str, ...] = ("capture-link",),
    resource_type: object = "message",
    display_title: str = "Fixture source title",
    provider: str = "manual",
    status: str = "linked",
) -> dict:
    return {
        "connections": [
            {"target": {"kind": "task", "id": task_id}, "reasons": list(reasons)},
        ],
        "id": identifier,
        # Every excluded region carries a canary.
        "normalized": {"summary": CANARIES[0], "context": CANARIES[0]},
        "provenance": {"capture_mode": "manual", "note": CANARIES[2]},
        "ref": {"kind": "capture", "id": identifier},
        "source": {
            "connection_ref": CANARIES[1],
            "container_ref": CANARIES[1],
            "display_title": display_title,
            "object_ref": CANARIES[1],
            "provider": provider,
            "resource_type": resource_type,
            "retrieved_at": "2026-09-01T00:00:00Z",
            "web_url": CANARIES[1],
        },
        "status": status,
        "task_hints": {"recipients": [CANARIES[3]]},
    }


class _Backend:
    """A minimal AgentBackend returning exactly the material handed to it."""

    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.requests: list[ContextRequest] = []

    def context(self, *, request: ContextRequest, today: dt.date) -> dict[str, object]:
        self.requests.append(request)
        return copy.deepcopy(self.result)

    def status(self, *, request):  # pragma: no cover - unused
        raise AssertionError("status is not part of this contract")

    def checkpoint(self, *, request):  # pragma: no cover - unused
        raise AssertionError("checkpoint is not part of this contract")


def _material(
    *,
    task_id: str = "T-0001",
    objectives: list | None = None,
    tasks: list | None = None,
    context: list | None = None,
) -> dict[str, object]:
    selected = _task(task_id, objective_ids=("O-1",))
    return {
        "entries": [],
        "planning": {
            "context": [_capture("C-0001", task_id=task_id)] if context is None else context,
            "objectives": [_objective("O-1")] if objectives is None else objectives,
            "tasks": [selected] if tasks is None else tasks,
        },
        "task": selected,
        "transport": "exclusive-local",
        "workspace_uid": WORKSPACE_UID,
    }


def _rendered(outcome) -> str:
    return json.dumps(outcome.data, ensure_ascii=False, sort_keys=True)


class PlanningProjectionTest(unittest.TestCase):
    """The pure projection: allowlists, resolution, order and caps."""

    def test_blocks_are_allowlisted_and_carry_no_excluded_region(self) -> None:
        blocks, overflowed = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[_objective("O-1")],
            tasks=[_task("T-0001", objective_ids=("O-1",))],
            context=[_capture("C-0001", task_id="T-0001")],
        )
        self.assertEqual(overflowed, ())
        self.assertEqual(set(blocks["objectives"][0]), {"id", "quarter", "status", "title"})
        self.assertEqual(
            set(blocks["sources"][0]),
            {"display_title", "id", "link_reasons", "provider", "resource_type", "status"},
        )
        rendered = json.dumps(blocks, ensure_ascii=False, sort_keys=True)
        for canary in CANARIES:
            self.assertNotIn(canary, rendered)

    def test_every_relationship_kind_resolves_and_sorts_deterministically(self) -> None:
        tasks = [
            _task("T-0001", parent_id="T-0002", dependencies=("T-0003",)),
            _task("T-0002", title="Parent"),
            _task("T-0003", title="Dependency"),
            _task("T-0004", title="Child", parent_id="T-0001"),
            _task("T-0005", title="Dependent", dependencies=("T-0001",)),
        ]
        blocks, _ = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[],
            tasks=tasks,
            context=[],
        )
        self.assertEqual(
            [(item["kind"], item["id"]) for item in blocks["relationships"]],
            [
                ("parent", "T-0002"),
                ("dependency", "T-0003"),
                ("child", "T-0004"),
                ("dependent", "T-0005"),
            ],
        )

    def test_unresolved_references_are_dropped_never_fabricated(self) -> None:
        blocks, _ = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[_objective("O-1")],
            tasks=[
                _task("T-0001", parent_id="T-9999", dependencies=("T-8888",),
                      objective_ids=("O-1", "O-404")),
            ],
            context=[],
        )
        self.assertEqual(blocks["relationships"], [])
        self.assertEqual([item["id"] for item in blocks["objectives"]], ["O-1"])

    def test_a_capture_linked_to_another_task_is_not_a_source(self) -> None:
        blocks, _ = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[],
            tasks=[_task("T-0001")],
            context=[_capture("C-0001", task_id="T-0002")],
        )
        self.assertEqual(blocks["sources"], [])

    def test_each_block_caps_and_names_its_overflow(self) -> None:
        tasks = [_task("T-0001", dependencies=tuple("T-%04d" % n for n in range(10, 30)))]
        tasks.extend(_task("T-%04d" % n) for n in range(10, 30))
        blocks, overflowed = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[_objective("O-%d" % n) for n in range(1, 9)],
            tasks=tasks,
            context=[_capture("C-%04d" % n, task_id="T-0001") for n in range(1, 9)],
        )
        self.assertEqual(len(blocks["objectives"]), 0)  # the Task links to none
        self.assertEqual(len(blocks["relationships"]), pack.RELATIONSHIPS_CAP)
        self.assertEqual(len(blocks["sources"]), pack.SOURCES_CAP)
        self.assertEqual(overflowed, ("relationships", "sources"))
        markers = pack.planning_omitted(overflowed=overflowed)
        self.assertIn("relationships_overflow", markers)
        self.assertIn("sources_overflow", markers)
        self.assertNotIn("objectives_overflow", markers)

    def test_objectives_cap_is_five_and_sorted_by_id(self) -> None:
        ids = tuple("O-%d" % n for n in range(9, 0, -1))
        blocks, overflowed = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[_objective(identifier) for identifier in ids],
            tasks=[_task("T-0001", objective_ids=ids)],
            context=[],
        )
        rendered = [item["id"] for item in blocks["objectives"]]
        self.assertEqual(len(rendered), pack.OBJECTIVES_CAP)
        self.assertEqual(rendered, sorted(rendered))
        self.assertIn("objectives", overflowed)

    def test_malformed_records_refuse_rather_than_render(self) -> None:
        for label, kwargs in {
            "objective without a title": {
                "objectives": [{"id": "O-1", "status": "active", "quarter": "2026-Q3"}],
            },
            "capture without a source": {"context": [{"id": "CAP-1", "ref": {"kind": "capture"}}]},
            "objectives not a list": {"objectives": {"id": "O-1"}},
        }.items():
            with self.subTest(case=label):
                arguments = {
                    "task_id": "T-0001",
                    "objectives": [_objective("O-1")],
                    "tasks": [_task("T-0001", objective_ids=("O-1",))],
                    "context": [],
                }
                arguments.update(kwargs)
                with self.assertRaises(ValueError):
                    pack.build_planning_blocks(**arguments)

    def test_constants_agree_with_the_frozen_contract_validator(self) -> None:
        """The frozen renderer imports this helper; the two stay pinned together."""

        self.assertEqual(pack.PLANNING_VIEW, contract._PLANNING_VIEW)
        self.assertEqual(pack.CORE_VIEW, contract._CORE_VIEW)
        self.assertEqual(set(pack.PLANNING_BLOCKS), set(contract._PLANNING_BLOCKS))
        self.assertEqual(
            set(contract._PLANNING_CONTEXT_DATA_FIELDS), set(pack.PLANNING_DATA_FIELDS)
        )

    def test_manifest_owned_paths_register_this_helper_and_this_test(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads(
            (root / "quality" / "agent-p0-oracle" / "manifest.v1.json").read_text(
                encoding="utf-8"
            )
        )
        lanes = {item["lane"]: item for item in manifest["ownership"]["lanes"]}
        self.assertEqual(
            lanes["C2"]["owned_paths"],
            [
                "workstack/agent_command_context.py",
                "workstack/agent_context_pack.py",
            ],
        )
        self.assertEqual(
            lanes["C2"]["paired_conformance"],
            "tests/test_agent_command_context_contract.py",
        )
        self.assertEqual(
            lanes["TE"]["owned_paths"],
            [
                "tests/test_agent_cli_e2e_contract.py",
                "tests/test_agent_context_pack.py",
            ],
        )

    def test_a_duplicate_identifier_fails_closed(self) -> None:
        for label, kwargs in {
            "duplicate Task id": {
                "tasks": [_task("T-0001"), _task("T-0001", title="Shadow")],
            },
            "duplicate Objective id": {
                "objectives": [_objective("O-1"), _objective("O-1", title="Shadow")],
            },
        }.items():
            with self.subTest(case=label):
                arguments = {
                    "task_id": "T-0001",
                    "objectives": [_objective("O-1")],
                    "tasks": [_task("T-0001", objective_ids=("O-1",))],
                    "context": [],
                }
                arguments.update(kwargs)
                with self.assertRaises(ValueError):
                    pack.build_planning_blocks(**arguments)

    def test_same_task_may_be_parent_and_dependency(self) -> None:
        blocks, overflowed = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[],
            tasks=[
                _task("T-0001", parent_id="T-0002", dependencies=("T-0002",)),
                _task("T-0002", title="Related"),
            ],
            context=[],
        )
        self.assertEqual(overflowed, ())
        self.assertEqual(
            [(item["kind"], item["id"]) for item in blocks["relationships"]],
            [("parent", "T-0002"), ("dependency", "T-0002")],
        )
        pack.validate_planning_data(
            _envelope_from_blocks(blocks),
            core_overflow_marker="recent_worklog_overflow",
        )

    def test_an_unknown_link_reason_is_refused_not_rendered(self) -> None:
        with self.assertRaises(ValueError):
            pack.build_planning_blocks(
                task_id="T-0001",
                objectives=[],
                tasks=[_task("T-0001")],
                context=[
                    _capture(
                        "C-0001",
                        task_id="T-0001",
                        reasons=("CANARY-ARBITRARY-REASON",),
                    )
                ],
            )

    def test_link_reasons_are_sorted_distinct_capture_enums(self) -> None:
        blocks, _ = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[],
            tasks=[_task("T-0001")],
            context=[
                _capture(
                    "C-0001",
                    task_id="T-0001",
                    reasons=("capture-link", "capture-conversion"),
                )
            ],
        )
        self.assertEqual(
            blocks["sources"][0]["link_reasons"],
            ["capture-conversion", "capture-link"],
        )

    def test_resource_type_is_required_and_allows_the_service_1024_bound(self) -> None:
        for length in (501, 1024):
            with self.subTest(length=length):
                blocks, _ = pack.build_planning_blocks(
                    task_id="T-0001",
                    objectives=[],
                    tasks=[_task("T-0001")],
                    context=[
                        _capture(
                            "C-0001",
                            task_id="T-0001",
                            resource_type="r" * length,
                        )
                    ],
                )
                self.assertEqual(len(blocks["sources"][0]["resource_type"]), length)
        with self.assertRaises(ValueError):
            pack.build_planning_blocks(
                task_id="T-0001",
                objectives=[],
                tasks=[_task("T-0001")],
                context=[
                    _capture("C-0001", task_id="T-0001", resource_type="r" * 1025)
                ],
            )
        with self.assertRaises(ValueError):
            pack.build_planning_blocks(
                task_id="T-0001",
                objectives=[],
                tasks=[_task("T-0001")],
                context=[_capture("C-0001", task_id="T-0001", resource_type=None)],
            )

    def test_display_title_keeps_the_service_500_bound(self) -> None:
        blocks, _ = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[],
            tasks=[_task("T-0001")],
            context=[
                _capture("C-0001", task_id="T-0001", display_title="t" * 500)
            ],
        )
        self.assertEqual(len(blocks["sources"][0]["display_title"]), 500)
        with self.assertRaises(ValueError):
            pack.build_planning_blocks(
                task_id="T-0001",
                objectives=[],
                tasks=[_task("T-0001")],
                context=[
                    _capture("C-0001", task_id="T-0001", display_title="t" * 501)
                ],
            )

    def test_long_objective_and_relationship_titles_are_not_refused_per_field(self) -> None:
        title = "g" * 800
        blocks, _ = pack.build_planning_blocks(
            task_id="T-0001",
            objectives=[_objective("O-1", title=title, quarter=None)],
            tasks=[
                _task("T-0001", parent_id="T-0002", objective_ids=("O-1",)),
                _task("T-0002", title=title),
            ],
            context=[],
        )
        self.assertEqual(blocks["objectives"][0]["title"], title)
        self.assertIsNone(blocks["objectives"][0]["quarter"])
        self.assertEqual(blocks["relationships"][0]["title"], title)
        pack.validate_planning_data(
            _envelope_from_blocks(blocks),
            core_overflow_marker="recent_worklog_overflow",
        )

    def test_capture_ids_must_be_c_prefixed_service_ids(self) -> None:
        with self.assertRaises(ValueError):
            pack.build_planning_blocks(
                task_id="T-0001",
                objectives=[],
                tasks=[_task("T-0001")],
                context=[_capture("CAP-0001", task_id="T-0001")],
            )


def _envelope_from_blocks(blocks: dict) -> dict:
    return {
        "omitted": list(pack.PLANNING_OMITTED_CATEGORIES),
        "recent_worklog": [],
        "task": {},
        "workspace_uid": WORKSPACE_UID,
        **blocks,
    }


class DefaultViewIsUnchangedTest(unittest.TestCase):
    def test_no_flag_and_core_v1_render_the_same_default_answer(self) -> None:
        backend = _Backend(_material())
        default = handle_context(
            request=ContextRequest(task_id="T-0001"), backend=backend, today=TODAY
        )
        explicit = handle_context(
            request=ContextRequest(task_id="T-0001", view=pack.CORE_VIEW),
            backend=backend,
            today=TODAY,
        )
        self.assertEqual(default.data, explicit.data)
        self.assertEqual(
            set(default.data), {"omitted", "recent_worklog", "task", "workspace_uid"}
        )
        self.assertEqual(
            default.data["omitted"],
            ["attachments", "captures", "objectives", "relationships", "work_sessions"],
        )

    def test_the_default_answer_ignores_planning_material_entirely(self) -> None:
        with_material = handle_context(
            request=ContextRequest(task_id="T-0001"),
            backend=_Backend(_material()),
            today=TODAY,
        )
        without = _material()
        del without["planning"]
        bare = handle_context(
            request=ContextRequest(task_id="T-0001"),
            backend=_Backend(without),
            today=TODAY,
        )
        self.assertEqual(with_material.data, bare.data)


class PlanningEnvelopeTest(unittest.TestCase):
    def _planning(self, material: dict[str, object]):
        return handle_context(
            request=ContextRequest(task_id="T-0001", view=pack.PLANNING_VIEW),
            backend=_Backend(material),
            today=TODAY,
        )

    def test_planning_adds_exactly_three_blocks_and_validates(self) -> None:
        outcome = self._planning(_material())
        self.assertIsNone(outcome.error_code)
        self.assertEqual(
            set(outcome.data),
            {
                "objectives",
                "omitted",
                "recent_worklog",
                "relationships",
                "sources",
                "task",
                "workspace_uid",
            },
        )
        self.assertEqual(
            outcome.data["omitted"], list(pack.PLANNING_OMITTED_CATEGORIES)
        )

    def test_no_canary_reaches_the_planning_envelope(self) -> None:
        rendered = _rendered(self._planning(_material()))
        for canary in CANARIES:
            self.assertNotIn(canary, rendered)

    def test_a_content_free_refusal_replaces_an_oversized_projection(self) -> None:
        # A Task detail alone larger than the bound cannot be trimmed into range.
        material = _material()
        oversized = _task("T-0001")
        oversized["detail"] = "d" * 40000
        material["task"] = oversized
        material["planning"]["tasks"] = [oversized]
        outcome = self._planning(material)
        self.assertEqual(outcome.error_code, "context_too_large")
        self.assertIsNone(outcome.data)

    def test_a_large_projection_is_trimmed_and_says_so(self) -> None:
        long_title = "t" * 480
        tasks = [_task("T-0001", dependencies=tuple("T-%04d" % n for n in range(10, 30)))]
        tasks.extend(_task("T-%04d" % n, title=long_title) for n in range(10, 30))
        material = _material(tasks=tasks)
        material["task"] = tasks[0]
        material["planning"]["context"] = [
            _capture("C-%04d" % n, task_id="T-0001") for n in range(1, 9)
        ]
        outcome = self._planning(material)
        self.assertIsNone(outcome.error_code)
        rendered = json.dumps(
            {"contract": "workstack.cli.v1", "data": outcome.data},
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        self.assertLessEqual(len(rendered.encode("utf-8")), 32768)
        self.assertIn("sources_overflow", outcome.data["omitted"])

    def test_an_unknown_view_refuses_before_the_backend_is_touched(self) -> None:
        backend = _Backend(_material())
        outcome = handle_context(
            request=ContextRequest(task_id="T-0001", view="planning-v2"),
            backend=backend,
            today=TODAY,
        )
        # Refused, not silently downgraded to the core answer.
        self.assertEqual(outcome.error_code, "internal_error")
        self.assertIsNone(outcome.data)
        self.assertEqual(backend.requests, [])

    def test_missing_planning_material_is_a_content_free_refusal(self) -> None:
        material = _material()
        del material["planning"]
        outcome = self._planning(material)
        self.assertEqual(outcome.error_code, "internal_error")
        self.assertIsNone(outcome.data)

    def test_an_unknown_link_reason_is_a_content_free_refusal(self) -> None:
        canary = "CANARY-ARBITRARY-REASON"
        outcome = self._planning(
            _material(context=[_capture("C-0001", task_id="T-0001", reasons=(canary,))])
        )
        self.assertEqual(outcome.error_code, "internal_error")
        self.assertIsNone(outcome.data)
        rendered = contract.render_outcome(outcome=outcome)
        self.assertNotIn(canary.encode("utf-8"), rendered)
        parsed = json.loads(rendered)
        self.assertNotIn("data", parsed)
        self.assertEqual(parsed["error"]["code"], "internal_error")


class LocalAndRunningParityTest(unittest.TestCase):
    """The two transports must project the same planning answer."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workstack-fixture-authority"
        self.root.mkdir()
        self.store = Store(self.root)
        self.stack = WorkStack(self.store)
        workspace = self.store.load("workspace.json")
        workspace["id"] = WORKSPACE_UID
        self.store.save("workspace.json", workspace)
        objective = self.stack.add_objective("Fixture Objective", quarter="2026-Q3")
        self.parent = self.stack.add_task("Parent Task")
        self.dependency = self.stack.add_task("Dependency Task")
        self.task = self.stack.add_task("Primary Task")
        self.stack.patch_task(
            self.task["id"],
            {
                "dependencies": [self.dependency["id"]],
                "objective_ids": [objective["id"]],
                "parent_id": self.parent["id"],
                "revision": self.task["revision"],
            },
        )
        self.admission = AuthorityAdmission(
            data_dir=self.root,
            workspace_uid=WORKSPACE_UID,
            storage_format="v3",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _local(self):
        return create_local_backend(
            admission=self.admission, store_factory=lambda *, root: self.store
        )

    def test_local_planning_projects_a_real_objective_parent_and_dependency(self) -> None:
        outcome = handle_context(
            request=ContextRequest(task_id=self.task["id"], view=pack.PLANNING_VIEW),
            backend=self._local(),
            today=TODAY,
        )
        self.assertIsNone(outcome.error_code)
        self.assertEqual(
            [item["title"] for item in outcome.data["objectives"]], ["Fixture Objective"]
        )
        self.assertEqual(
            [(item["kind"], item["id"]) for item in outcome.data["relationships"]],
            [("parent", self.parent["id"]), ("dependency", self.dependency["id"])],
        )
        # The rendered core Task keeps its own allowlist and gains no link field.
        self.assertEqual(
            set(outcome.data["task"]),
            {"detail", "due", "id", "priority", "revision", "status", "title", "uid"},
        )
        # No Capture is linked in this fixture, and none is invented.
        self.assertEqual(outcome.data["sources"], [])

    def test_local_same_related_task_as_parent_and_dependency(self) -> None:
        related = self.stack.add_task("Related Task")
        task = self.stack.add_task("Dual-linked Task")
        self.stack.patch_task(
            task["id"],
            {
                "dependencies": [related["id"]],
                "parent_id": related["id"],
                "revision": task["revision"],
            },
        )
        outcome = handle_context(
            request=ContextRequest(task_id=task["id"], view=pack.PLANNING_VIEW),
            backend=self._local(),
            today=TODAY,
        )
        self.assertIsNone(outcome.error_code)
        self.assertEqual(
            [(item["kind"], item["id"]) for item in outcome.data["relationships"]],
            [("parent", related["id"]), ("dependency", related["id"])],
        )

    def test_local_default_view_still_omits_the_planning_categories(self) -> None:
        outcome = handle_context(
            request=ContextRequest(task_id=self.task["id"]),
            backend=self._local(),
            today=TODAY,
        )
        self.assertEqual(
            set(outcome.data), {"omitted", "recent_worklog", "task", "workspace_uid"}
        )
        self.assertIn("objectives", outcome.data["omitted"])

    def _running(self, requester):
        info = self.store.server_info_path
        info.write_text(
            json.dumps({"host": HOST, "port": PORT, "version": 1}), encoding="utf-8"
        )
        return create_running_server_backend(
            server_info_path=info,
            expected_workspace_uid=WORKSPACE_UID,
            request_json=requester,
        )

    def test_running_owner_projects_the_same_planning_answer(self) -> None:
        local = handle_context(
            request=ContextRequest(task_id=self.task["id"], view=pack.PLANNING_VIEW),
            backend=self._local(),
            today=TODAY,
        )
        remote = handle_context(
            request=ContextRequest(task_id=self.task["id"], view=pack.PLANNING_VIEW),
            backend=self._running(_OwnerRequester(self.stack, self.task["id"])),
            today=TODAY,
        )
        self.assertIsNone(remote.error_code)
        for block in pack.PLANNING_BLOCKS:
            self.assertEqual(local.data[block], remote.data[block])
        self.assertEqual(local.data["task"], remote.data["task"])

    def test_a_task_identity_change_between_the_two_gets_is_refused(self) -> None:
        for field, value in (("revision", 99), ("uid", WORKSPACE_UID), ("id", "T-9999")):
            with self.subTest(field=field):
                requester = _OwnerRequester(self.stack, self.task["id"])
                requester.workspace_task_override = {field: value}
                outcome = handle_context(
                    request=ContextRequest(
                        task_id=self.task["id"], view=pack.PLANNING_VIEW
                    ),
                    backend=self._running(requester),
                    today=TODAY,
                )
                # Fail closed and content-free; never a local answer instead.
                self.assertEqual(outcome.error_code, "internal_error")
                self.assertIsNone(outcome.data)
                self.assertNotIn("/api/v1/review", "".join(requester.paths[-1:]))

    def test_a_task_change_during_the_worklog_reads_is_refused(self) -> None:
        requester = _OwnerRequester(self.stack, self.task["id"])
        # The final selected-Task read, after the review GETs, sees a new revision.
        requester.late_task_override = {"revision": 99}
        outcome = handle_context(
            request=ContextRequest(task_id=self.task["id"], view=pack.PLANNING_VIEW),
            backend=self._running(requester),
            today=TODAY,
        )
        self.assertEqual(outcome.error_code, "internal_error")
        self.assertIsNone(outcome.data)
        self.assertGreater(requester.task_reads, 1)


class _OwnerRequester:
    """A scripted loopback owner backed by the same synthetic authority."""

    def __init__(self, stack: WorkStack, task_id: str) -> None:
        self.stack = stack
        self.task_id = task_id
        self.paths: list[str] = []
        self.task_reads = 0
        self.workspace_task_override: dict[str, object] | None = None
        self.late_task_override: dict[str, object] | None = None

    def request(self, *, host, port, method, path, body=None, headers=None):
        self.paths.append(path)
        if path == "/api/v1/session":
            return 200, {"data": {"csrf_token": "fixture-csrf"}}
        if path == "/api/v1/storage":
            return 200, {
                "data": {"store_schema_version": 3, "workspace_id": WORKSPACE_UID}
            }
        if path.startswith("/api/v1/tasks/"):
            self.task_reads += 1
            detail = self.stack.task_detail(self.task_id)
            if self.late_task_override is not None and self.task_reads > 1:
                detail["task"].update(self.late_task_override)
            return 200, {"data": detail}
        if path == "/api/v1/workspace":
            projection = self.stack.workspace_projection()
            if self.workspace_task_override is not None:
                for item in projection["tasks"]:
                    if item["id"] == self.task_id:
                        item.update(self.workspace_task_override)
            return 200, {"data": projection}
        if path.startswith("/api/v1/review"):
            return 200, {"data": {"day": {"entries": []}}}
        raise AssertionError("unexpected owner path: %s" % path)


class ParserTest(unittest.TestCase):
    def test_the_flag_is_optional_and_defaults_to_the_core_view(self) -> None:
        parsed = build_parser().parse_args(
            ["--data-dir", "/workstack-fixture/authority", "agent", "context", "--task", "T-0001"]
        )
        self.assertEqual(parsed.view, "core-v1")

    def test_the_planning_view_is_accepted(self) -> None:
        parsed = build_parser().parse_args(
            [
                "--data-dir", "/workstack-fixture/authority", "agent", "context",
                "--task", "T-0001", "--view", "planning-v1",
            ]
        )
        self.assertEqual(parsed.view, "planning-v1")

    def test_an_unknown_view_is_refused_by_the_parser(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            build_parser().parse_args(
                [
                    "--data-dir", "/workstack-fixture/authority", "agent", "context",
                    "--task", "T-0001", "--view", "planning-v2",
                ]
            )
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

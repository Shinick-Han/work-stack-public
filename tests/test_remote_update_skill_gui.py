"""The optional agent-Skill offer, from the native click down to the helper.

Every test here drives the real modules.  The flow tests build an actual
``RemoteUpdateFlow`` over the flow suite's own fakes; the caller-chain tests
drive an actual ``RemoteUpdateViewSession``, ``remote_update_projection`` and
``RemoteUpdateController`` over that flow; the port tests build an actual
``RemoteUpdateInstallPorts``, run a real preparation through it, and then run
the real ``RemoteUpdateSkillPort`` over the same faked subprocess factory every
other port suite uses.

Two things here are deliberately stand-ins, and neither is claimed as more.

*   ``SkillCommandBuilder`` stands in for ``build_ssh_skill_command``, the
    separately reviewed transport seam that is not on this branch yet.  It is
    called with, and asserts, that seam's exact frozen signature.
*   The remote process is faked, as it is in every other port suite here.  A
    real ssh run against the real helper is integration proof and is not
    claimed by anything below.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
TESTS = ROOT / "tests"
for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import remote_update_controller as CONTROLLER  # noqa: E402
import remote_update_install_ports_transport as TRANSPORT  # noqa: E402
import remote_update_presentation_actions as ACTIONS  # noqa: E402
import remote_update_projection as PROJECT  # noqa: E402
import remote_update_skill as SKILL  # noqa: E402
import remote_update_skill_copy as COPY  # noqa: E402
import remote_update_skill_offer as OFFER  # noqa: E402
import remote_update_skill_port as PORT  # noqa: E402
from remote_update_flow import RemoteUpdateFlow  # noqa: E402
from remote_update_flow_contract import (  # noqa: E402
    RemoteUpdatePorts,
    RemoteUpdateRefused,
)
from remote_update_presentation import present_remote_update  # noqa: E402


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FLOW_TESTS = load_module(TESTS / "test_remote_update_flow.py", "skill_gui_flow_fakes")
CTRL_TESTS = load_module(TESTS / "test_remote_update_controller.py", "skill_gui_ctrl_fakes")
PORT_TESTS = load_module(TESTS / "test_remote_update_install_ports.py", "skill_gui_port_fakes")
HELPER_TESTS = load_module(TESTS / "test_remote_skill_install.py", "skill_gui_helper_fixtures")
DRIVER_TESTS = PORT_TESTS.DRIVER_TESTS

MAX_FILE_LINES = 800
MAX_FUNCTION_LINES = 100
MAX_CCN = 15
PRODUCTION = (
    SHELL / "remote_update_skill.py",
    SHELL / "remote_update_skill_copy.py",
    SHELL / "remote_update_skill_offer.py",
    SHELL / "remote_update_skill_port.py",
)
#: The two files the offer was extracted out of stay inside the repo's own
#: production file-length budget; the extraction is what keeps them there.
UNCHANGED_BUDGET = (
    SHELL / "remote_update_flow.py",
    SHELL / "remote_update_presentation.py",
    SHELL / "remote_update_flow_contract.py",
    SHELL / "remote_update_projection.py",
    SHELL / "remote_update_presentation_actions.py",
)

VERSION = DRIVER_TESTS.make_artifact().product_version
DIGESTS = {
    "SKILL.md": PORT.DIGEST_PREFIX + "1" * 64,
    "references/commands.md": PORT.DIGEST_PREFIX + "2" * 64,
    "references/journal-policy.md": PORT.DIGEST_PREFIX + "3" * 64,
}


def helper_files() -> dict[str, object]:
    return {name: {"sha256": DIGESTS[name], "size": 64} for name in PORT.SKILL_FILES}


def helper_document(
    action: str,
    outcome: str,
    *,
    code: str | None = None,
    version: str = VERSION,
    **overrides: object,
) -> dict[str, object]:
    """One document in exactly the shape ``remote_skill_install`` prints."""

    document: dict[str, object] = {
        "action": action,
        "atomic_directory_publish": False,
        "destination": PORT.HELPER_DESTINATION,
        "files": helper_files(),
        "operation": "install-skill",
        "outcome": outcome,
        "product_version": version,
        "schema_version": 1,
    }
    if code is not None:
        document["code"] = code
    document.update(overrides)
    return document


def skill_process(document: object, returncode: int = 0) -> object:
    payload = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return DRIVER_TESTS.FakeProcess(stdout=payload.encode("ascii") + b"\n", returncode=returncode)


class SkillCommandBuilder:
    """The reviewed transport seam's exact signature, in process.

    ``build_ssh_skill_command(profile, ssh_executable, *, apply=False)`` is what
    ``m14-skill-transport`` exports; this records what it was asked for so the
    tests can assert that ``--apply`` appears only for the second click.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, profile: object, ssh_executable: str, *, apply: bool = False) -> list[str]:
        app = getattr(profile, "remote_app_dir")
        tokens = [
            getattr(profile, "remote_python"),
            "-I",
            "-B",
            f"{app}/desktop/python-webview-shell/remote_skill_install.py",
            "--install-root",
            app,
        ]
        if apply:
            tokens.append("--apply")
        self.calls.append({"apply": apply, "tokens": tuple(tokens)})
        return [
            ssh_executable,
            *TRANSPORT.OPENSSH_BATCH,
            getattr(profile, "ssh_host_alias"),
            " ".join(tokens),
        ]


class ScriptedSkillPort:
    """A ``SkillPort`` that answers from a script and records each call."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def _take(self, name: str) -> PORT.SkillOutcome:
        self.calls.append(name)
        if not self.answers:
            raise AssertionError(f"unscripted skill call {name}")
        return self.answers.pop(0)

    def inspect_skill(self) -> PORT.SkillOutcome:
        return self._take("inspect_skill")

    def install_skill(self) -> PORT.SkillOutcome:
        return self._take("install_skill")


class SkillHarness(FLOW_TESTS.Harness):
    """The flow suite's harness with the optional Skill port injected."""

    def __init__(self, skill: object = None, **scripts: object) -> None:
        super().__init__(**scripts)
        self.skill = skill
        self.flow = RemoteUpdateFlow(
            RemoteUpdatePorts(
                preview=self.preview,
                owner=self.owner,
                backup=self.backup,
                prepare=self.prepare,
                probe=self.probe,
                activation=self.activation,
                verification=self.verification,
                skill=skill,
            ),
            operation_ids=self._operation_id,
            journal=self.journal,
        )


class SkillFlow(CTRL_TESTS.Flow):
    """The controller suite's flow with the optional Skill port injected.

    The ports are the controller suite's own recording ports, rebuilt here only
    because ``RemoteUpdatePorts`` is frozen and the skill member has to be set
    at construction.
    """

    def __init__(self, skill: object = None, **scripts: object) -> None:
        super().__init__(**scripts)
        self.skill = skill
        self.calls.clear()
        self.issued.clear()
        self.flow = RemoteUpdateFlow(
            RemoteUpdatePorts(
                preview=CTRL_TESTS.PreviewPort(
                    self.calls, "preview", describe=scripts.get("preview", (CTRL_TESTS.PREVIEW_OK,))
                ),
                owner=CTRL_TESTS.OwnerPort(
                    self.calls, "owner", stop=scripts.get("stop", (CTRL_TESTS.STOP_OK,))
                ),
                backup=CTRL_TESTS.BackupPort(
                    self.calls,
                    "backup",
                    create_verified=scripts.get("backup", (CTRL_TESTS.BACKUP_OK,)),
                ),
                prepare=CTRL_TESTS.PreparePort(
                    self.calls, "prepare", prepare=scripts.get("prepare", (CTRL_TESTS.PREPARE_OK,))
                ),
                probe=CTRL_TESTS.ProbePort(
                    self.calls, "probe", probe=scripts.get("probe", (CTRL_TESTS.PROBE_OK,))
                ),
                activation=CTRL_TESTS.ActivationPort(
                    self.calls,
                    "activation",
                    activate=scripts.get("activate", (CTRL_TESTS.ACTIVATE_OK,)),
                    confirm=scripts.get("confirm", (CTRL_TESTS.CONFIRM_OK,)),
                ),
                verification=CTRL_TESTS.VerificationPort(
                    self.calls, "verification", verify=scripts.get("verify", (CTRL_TESTS.VERIFY_OK,))
                ),
                skill=skill,
            ),
            operation_ids=self._operation_id,
            journal=None,
        )


def outcome(status: str, **fields: object) -> PORT.SkillOutcome:
    return PORT.SkillOutcome(status=status, **fields)  # type: ignore[arg-type]


def ready(skill: object = None, **scripts: object) -> SkillHarness:
    """A harness whose update has actually reached a verified ``ready``."""

    harness = SkillHarness(skill, **scripts)
    harness.run(6)
    harness.resume()
    harness.flow.advance()
    assert harness.flow.snapshot().stage == "ready"
    return harness


class MappingTests(unittest.TestCase):
    """The one named mapping, and that nothing invents a status beside it."""

    def test_every_helper_disposition_maps_to_one_published_code(self) -> None:
        seen = set()
        for (action, raw), status in PORT.STATUS_BY_DISPOSITION.items():
            code = PORT.skill_flow_code(outcome(status))
            self.assertIn(code, PORT.SKILL_FLOW_CODES, (action, raw))
            seen.add(code)
        self.assertEqual(
            seen,
            {
                "skill_absent", "skill_outdated", "skill_current",
                "skill_installed", "skill_updated", "skill_refused_failed",
            },
        )

    def test_the_helpers_own_refusals_keep_their_own_codes(self) -> None:
        self.assertEqual(
            PORT.skill_flow_code(outcome("refused", code="APP_NOT_VERIFIED")),
            "skill_refused_app_not_verified",
        )
        self.assertEqual(
            PORT.skill_flow_code(outcome("refused", code="SKILL_DEST_MODIFIED")),
            "skill_refused_destination_modified",
        )
        # A refusal code this build has never heard of is still a refusal.
        self.assertEqual(
            PORT.skill_flow_code(outcome("refused", code="SKILL_SOMETHING_NEW")),
            "skill_refused_failed",
        )

    def test_updated_is_a_real_outcome_and_only_a_read_offers_a_write(self) -> None:
        admitted = PORT.admit_skill_outcome(
            helper_document("update", "updated"), 0, expected_version=VERSION, mutating=True
        )
        assert admitted is not None
        self.assertEqual(admitted.status, "updated")
        self.assertEqual(PORT.skill_flow_code(admitted), "skill_updated")
        self.assertFalse(admitted.offers_install)
        read = PORT.admit_skill_outcome(
            helper_document("update", "planned"), 0, expected_version=VERSION, mutating=False
        )
        assert read is not None
        self.assertTrue(read.offers_install)

    def test_every_published_skill_code_is_projected_to_e_and_f(self) -> None:
        for code in PORT.SKILL_FLOW_CODES:
            self.assertIn(code, PROJECT.D_TO_E_CODE, code)
            self.assertIn(code, PROJECT.D_TO_F_CODE, code)


class AdmissionTests(unittest.TestCase):
    """A process that exited zero is not evidence; the document has to be."""

    def admitted(self, document: object, returncode: int = 0) -> object:
        return PORT.admit_skill_outcome(
            document, returncode, expected_version=VERSION, mutating=False
        )

    def test_the_exact_document_is_admitted(self) -> None:
        result = self.admitted(helper_document("install", "planned"))
        assert result is not None
        self.assertEqual((result.status, result.action, result.outcome), ("absent", "install", "planned"))
        self.assertEqual(len(result.files), 3)
        self.assertEqual({item.name for item in result.files}, set(PORT.SKILL_FILES))

    def test_a_malformed_or_mismatched_document_is_refused(self) -> None:
        cases = {
            "wrong schema": helper_document("install", "planned", schema_version=2),
            "wrong operation": helper_document("install", "planned", operation="install-app"),
            "wrong destination": helper_document("install", "planned", destination="/tmp/skills"),
            "atomic publish claimed": helper_document(
                "install", "planned", atomic_directory_publish=True
            ),
            "unknown disposition": helper_document("install", "noop"),
            "refusal without a code": helper_document("refuse", "refused"),
            "code on a success": helper_document("install", "planned", code="SKILL_DEST_FOREIGN"),
            "two files": helper_document(
                "install", "planned", files={"SKILL.md": {"sha256": DIGESTS["SKILL.md"], "size": 1}}
            ),
            "bad digest": helper_document(
                "install",
                "planned",
                files={
                    name: {"sha256": PORT.DIGEST_PREFIX + "zz" + "0" * 62, "size": 1}
                    for name in PORT.SKILL_FILES
                },
            ),
            "unprefixed digest": helper_document(
                "install",
                "planned",
                files={name: {"sha256": "0" * 64, "size": 1} for name in PORT.SKILL_FILES},
            ),
            "extra key": helper_document("install", "planned", home="/home/someone"),
            "not an object": ["install"],
        }
        for label, document in cases.items():
            with self.subTest(label):
                self.assertIsNone(self.admitted(document))

    def test_a_document_for_another_build_is_refused(self) -> None:
        self.assertIsNone(self.admitted(helper_document("install", "planned", version="0.0.1")))

    def test_the_exit_code_has_to_agree_with_the_document(self) -> None:
        # Success documents come from an exit of zero, refusals from two.
        self.assertIsNone(self.admitted(helper_document("install", "planned"), 2))
        self.assertIsNone(
            self.admitted(helper_document("refuse", "refused", code="APP_NOT_VERIFIED"), 0)
        )
        refused = self.admitted(
            helper_document("refuse", "refused", code="APP_NOT_VERIFIED"), 2
        )
        assert refused is not None
        self.assertEqual(refused.status, "refused")


class SkillPortTests(unittest.TestCase):
    """The real port over a real preparation and one faked remote process."""

    def prepared(self, *extra: object) -> tuple[object, object, SkillCommandBuilder]:
        artifact = DRIVER_TESTS.make_artifact()
        absent = PORT_TESTS.facts(
            capability=PORT_TESTS.capability_block(
                "available", "transactional", scratch="measured", commit="renameat2_noreplace"
            )
        )
        factory = DRIVER_TESTS.RecordingFactory(
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.probe_process(absent),
            DRIVER_TESTS.FakeProcess(
                stdout=DRIVER_TESTS.success_line(
                    digest=artifact.digest, manifest=artifact.manifest_digest
                ),
                returncode=0,
            ),
            DRIVER_TESTS.probe_process(PORT_TESTS.current_facts()),
            *extra,
        )
        ports = PORT_TESTS.make_ports(factory, artifact=artifact)
        self.assertEqual(ports.prepare("prepare-skill").status, "verified")
        self.assertTrue(ports.preparation_is_verified())
        builder = SkillCommandBuilder()
        return ports, factory, builder

    def test_no_ssh_is_started_before_a_verified_preparation(self) -> None:
        factory = DRIVER_TESTS.RecordingFactory()
        ports = PORT_TESTS.make_ports(factory)
        self.assertFalse(ports.preparation_is_verified())
        builder = SkillCommandBuilder()
        port = SKILL.RemoteUpdateSkillPort(ports, build_command=builder)
        for answer in (port.inspect_skill(), port.install_skill()):
            self.assertEqual(answer.status, "refused")
            self.assertEqual(answer.code, "APP_NOT_VERIFIED")
        self.assertEqual(factory.called, 0)
        self.assertEqual(builder.calls, [])

    def test_inspect_sends_no_apply_and_install_sends_exactly_one(self) -> None:
        ports, factory, builder = self.prepared(
            skill_process(helper_document("install", "planned")),
            skill_process(helper_document("install", "installed")),
        )
        port = SKILL.RemoteUpdateSkillPort(ports, build_command=builder)
        read = port.inspect_skill()
        written = port.install_skill()
        self.assertEqual(read.status, "absent")
        self.assertTrue(read.offers_install)
        self.assertEqual(written.status, "installed")
        self.assertTrue(written.mutating)
        self.assertEqual([call["apply"] for call in builder.calls], [False, True])
        self.assertNotIn("--apply", builder.calls[0]["tokens"])
        self.assertEqual(builder.calls[1]["tokens"][-1], "--apply")
        # The argv names the helper inside the app the update just verified,
        # and carries no owner, no data root and no session token.
        tokens = builder.calls[0]["tokens"]
        self.assertIn("--install-root", tokens)
        self.assertNotIn("--owner", tokens)
        self.assertNotIn("--data-root", tokens)
        self.assertEqual(factory.commands[-1][0], DRIVER_TESTS.SSH)

    def test_a_current_install_is_a_noop_and_offers_no_write(self) -> None:
        ports, _factory, builder = self.prepared(
            skill_process(helper_document("noop", "noop"))
        )
        port = SKILL.RemoteUpdateSkillPort(ports, build_command=builder)
        answer = port.inspect_skill()
        self.assertEqual(answer.status, "current")
        self.assertFalse(answer.offers_install)
        self.assertEqual(PORT.skill_flow_code(answer), "skill_current")

    def test_an_edited_destination_is_refused_with_its_own_code(self) -> None:
        ports, _factory, builder = self.prepared(
            skill_process(helper_document("refuse", "refused", code="SKILL_DEST_MODIFIED"), 2)
        )
        port = SKILL.RemoteUpdateSkillPort(ports, build_command=builder)
        answer = port.inspect_skill()
        self.assertEqual(answer.status, "refused")
        self.assertEqual(answer.code, "SKILL_DEST_MODIFIED")
        self.assertEqual(PORT.skill_flow_code(answer), "skill_refused_destination_modified")
        # The evidence the helper measured is kept, not flattened away.
        self.assertEqual(len(answer.files), 3)

    def test_a_stalled_channel_is_unknown_and_not_a_quiet_nothing(self) -> None:
        ports, _factory, builder = self.prepared(
            DRIVER_TESTS.FakeProcess(stall_stdout=True)
        )
        port = SKILL.RemoteUpdateSkillPort(ports, build_command=builder)
        answer = port.install_skill()
        self.assertEqual(answer.status, PORT.STATUS_UNKNOWN)
        self.assertEqual(answer.code, PORT.CHANNEL_LOST)
        self.assertTrue(answer.mutating)
        self.assertEqual(PORT.skill_flow_code(answer), "skill_unknown")

    def test_unrecognised_stdout_is_unknown(self) -> None:
        ports, _factory, builder = self.prepared(
            DRIVER_TESTS.FakeProcess(stdout=b"not json at all\n", returncode=0)
        )
        port = SKILL.RemoteUpdateSkillPort(ports, build_command=builder)
        answer = port.inspect_skill()
        self.assertEqual(answer.status, PORT.STATUS_UNKNOWN)
        self.assertEqual(answer.code, PORT.DOCUMENT_UNRECOGNISED)

    @unittest.skipUnless(
        SKILL.skill_transport_available(), "the reviewed skill transport is not in this build"
    )
    def test_the_reviewed_transport_builds_the_argv_this_port_sends(self) -> None:
        """No stand-in: the real ``build_ssh_skill_command`` drives this one."""

        ports, factory, _builder = self.prepared(
            skill_process(helper_document("install", "planned")),
            skill_process(helper_document("install", "installed")),
        )
        port = SKILL.RemoteUpdateSkillPort(ports)
        self.assertEqual(port.inspect_skill().status, "absent")
        self.assertEqual(port.install_skill().status, "installed")
        read, written = factory.commands[-2], factory.commands[-1]
        self.assertEqual(read[0], DRIVER_TESTS.SSH)
        self.assertIn("--install-root", read[-1])
        self.assertIn("remote_skill_install.py", read[-1])
        self.assertNotIn("--apply", read[-1])
        self.assertTrue(written[-1].endswith("--apply"))
        for token in ("--owner", "--data-root", "--expected-workspace-uid"):
            self.assertNotIn(token, read[-1])
            self.assertNotIn(token, written[-1])

    def test_no_port_is_built_where_this_build_carries_no_command_builder(self) -> None:
        factory = DRIVER_TESTS.RecordingFactory()
        ports = PORT_TESTS.make_ports(factory)
        if SKILL.skill_transport_available():
            self.assertIsNotNone(SKILL.build_skill_port(ports))
        else:
            self.assertIsNone(SKILL.build_skill_port(ports))
        self.assertIsNotNone(SKILL.build_skill_port(ports, build_command=SkillCommandBuilder()))


class FlowOfferTests(unittest.TestCase):
    """What the flow offers, and refuses, around the optional Skill step."""

    def test_an_old_bundle_without_the_port_is_unchanged(self) -> None:
        harness = ready()
        snapshot = harness.flow.snapshot()
        self.assertEqual(snapshot.stage, "ready")
        self.assertEqual(snapshot.code, "update_ready")
        self.assertEqual(snapshot.actions, ("finish",))
        with self.assertRaises(RemoteUpdateRefused) as raised:
            harness.flow.inspect_skill()
        self.assertEqual(raised.exception.code, "skill_refused_unavailable")

    def test_nothing_is_offered_before_the_update_is_ready(self) -> None:
        port = ScriptedSkillPort()
        harness = SkillHarness(port)
        harness.run(2)
        snapshot = harness.flow.snapshot()
        self.assertNotIn("run_skill_inspect", snapshot.actions)
        for call in (harness.flow.inspect_skill, harness.flow.install_skill):
            with self.assertRaises(RemoteUpdateRefused) as raised:
                call()
            self.assertEqual(raised.exception.code, "skill_refused_not_ready")
        self.assertEqual(port.calls, [])

    def test_a_ready_update_offers_the_read_first_and_only_the_read(self) -> None:
        harness = ready(ScriptedSkillPort())
        snapshot = harness.flow.snapshot()
        self.assertEqual(snapshot.code, "update_ready")
        self.assertEqual(snapshot.actions, ("run_skill_inspect", "finish"))
        with self.assertRaises(RemoteUpdateRefused) as raised:
            harness.flow.install_skill()
        self.assertEqual(raised.exception.code, "skill_refused_inspect_required")

    def test_inspect_then_the_explicit_write_then_the_read_again(self) -> None:
        port = ScriptedSkillPort(
            outcome("absent", action="install", outcome="planned"),
            outcome("installed", action="install", outcome="installed", mutating=True),
        )
        harness = ready(port)
        first = harness.flow.inspect_skill()
        self.assertEqual(first.code, "skill_absent")
        self.assertEqual(first.stage, "ready")
        self.assertEqual(first.actions, ("run_skill_install", "finish"))
        second = harness.flow.install_skill()
        self.assertEqual(second.code, "skill_installed")
        self.assertEqual(second.actions, ("run_skill_inspect", "finish"))
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])
        # The update itself never moved and never lost what it measured.
        self.assertEqual(second.stage, "ready")
        self.assertEqual(second.versions["remote"], harness.flow.snapshot().versions["remote"])

    def test_an_older_skill_offers_the_update_labelled_action(self) -> None:
        port = ScriptedSkillPort(
            outcome("outdated", action="update", outcome="planned"),
            outcome("updated", action="update", outcome="updated", mutating=True),
        )
        harness = ready(port)
        self.assertEqual(harness.flow.inspect_skill().actions[0], "run_skill_update")
        self.assertEqual(harness.flow.install_skill().code, "skill_updated")

    def test_a_current_skill_leaves_only_the_read_and_close(self) -> None:
        port = ScriptedSkillPort(outcome("current", action="noop", outcome="noop"))
        harness = ready(port)
        snapshot = harness.flow.inspect_skill()
        self.assertEqual(snapshot.code, "skill_current")
        self.assertEqual(snapshot.actions, ("run_skill_inspect", "finish"))
        with self.assertRaises(RemoteUpdateRefused):
            harness.flow.install_skill()

    def test_a_lost_write_is_never_reissued_and_only_the_read_is_left(self) -> None:
        port = ScriptedSkillPort(
            outcome("absent", action="install", outcome="planned"),
            outcome(PORT.STATUS_UNKNOWN, code=PORT.CHANNEL_LOST, mutating=True),
            outcome("current", action="noop", outcome="noop"),
        )
        harness = ready(port)
        harness.flow.inspect_skill()
        lost = harness.flow.install_skill()
        self.assertEqual(lost.code, "skill_unknown")
        self.assertEqual(lost.actions, ("run_skill_inspect", "finish"))
        with self.assertRaises(RemoteUpdateRefused) as raised:
            harness.flow.install_skill()
        self.assertEqual(raised.exception.code, "skill_refused_inspect_required")
        # The one allowed next step reads what is really there.
        reconciled = harness.flow.inspect_skill()
        self.assertEqual(reconciled.code, "skill_current")
        self.assertEqual(port.calls, ["inspect_skill", "install_skill", "inspect_skill"])

    def test_a_refusal_says_so_and_does_not_claim_an_install(self) -> None:
        port = ScriptedSkillPort(
            outcome("refused", action="refuse", outcome="refused", code="SKILL_DEST_MODIFIED")
        )
        harness = ready(port)
        snapshot = harness.flow.inspect_skill()
        self.assertEqual(snapshot.code, "skill_refused_destination_modified")
        presented = present_remote_update(
            PROJECT.project_remote_update(
                harness.flow,
                session_id=CTRL_TESTS.SESSION,
                workspace_id=CTRL_TESTS.WORKSPACE,
            ).snapshot
        )
        self.assertIn("agent Skill was not installed", presented.headline)
        self.assertIn("update is ready", presented.headline)
        self.assertIn("Nothing was written", presented.detail)


class AdmissibilityTests(unittest.TestCase):
    """The action vocabulary the page admits a Skill click through."""

    def test_the_write_is_not_a_new_remote_update_mutation(self) -> None:
        for action in ("install_skill", "update_skill"):
            self.assertIn(action, ACTIONS.REMOTE_ACTIONS)
            self.assertNotIn(action, ACTIONS.NEW_REMOTE_MUTATIONS)
            self.assertNotIn(action, ACTIONS.THIS_PC_ACTIONS)
            self.assertNotIn(action, ACTIONS.READ_ONLY_ACTIONS)
        self.assertIn("inspect_skill", ACTIONS.READ_ONLY_ACTIONS)

    def test_a_skill_action_is_never_admissible_away_from_ready(self) -> None:
        harness = ready(ScriptedSkillPort())
        projection = PROJECT.project_remote_update(
            harness.flow, session_id=CTRL_TESTS.SESSION, workspace_id=CTRL_TESTS.WORKSPACE
        )
        snapshot = projection.snapshot
        self.assertEqual(ACTIONS.next_admissible_action(snapshot), "inspect_skill")
        moved = replace(snapshot, stage="verify")
        self.assertIsNone(ACTIONS.next_admissible_action(moved))
        self.assertFalse(ACTIONS.action_is_admissible(moved, "inspect_skill"))

    def test_a_write_is_admissible_only_from_the_inspect_that_earned_it(self) -> None:
        harness = ready(
            ScriptedSkillPort(outcome("absent", action="install", outcome="planned"))
        )
        harness.flow.inspect_skill()
        snapshot = PROJECT.project_remote_update(
            harness.flow, session_id=CTRL_TESTS.SESSION, workspace_id=CTRL_TESTS.WORKSPACE
        ).snapshot
        self.assertTrue(ACTIONS.action_is_admissible(snapshot, "install_skill"))
        # The same offer under any other published code is not admissible.
        for code in ("skill_unknown", "skill_outdated", "ready"):
            forged = replace(snapshot, code=code)
            self.assertFalse(
                ACTIONS.action_is_admissible(forged, "install_skill"), code
            )

    def test_the_page_still_renders_at_most_the_action_budget(self) -> None:
        harness = ready(ScriptedSkillPort())
        snapshot = PROJECT.project_remote_update(
            harness.flow, session_id=CTRL_TESTS.SESSION, workspace_id=CTRL_TESTS.WORKSPACE
        ).snapshot
        self.assertLessEqual(len(snapshot.actions), ACTIONS.MAX_ACTIONS)
        self.assertEqual(snapshot.actions, ("inspect_skill", "close"))


class CallerChainTests(unittest.TestCase):
    """One native click, through the real controller, to the real flow call."""

    def fixture(self, port: object) -> object:
        flow = SkillFlow(port)
        flow.run(6)
        flow.flow.resume_after_restart(
            CTRL_TESTS.StartupEvidence(
                selected_activation_id=flow.flow.retained_activation().operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        flow.flow.advance()
        self.assertEqual(flow.flow.snapshot().stage, "ready")
        fixture = CTRL_TESTS.Fixture(flow)
        self.addCleanup(fixture.close)
        self.assertTrue(fixture.controller.open())
        return fixture

    def test_the_offer_is_absent_without_the_port(self) -> None:
        fixture = self.fixture(None)
        self.assertIsNone(fixture.next_action())
        self.assertNotIn("Check agent Skill", fixture.host.pages[-1])
        self.assertIn("Close", fixture.host.pages[-1])

    def test_a_real_click_reaches_inspect_then_the_explicit_install(self) -> None:
        port = ScriptedSkillPort(
            outcome("absent", action="install", outcome="planned"),
            outcome("installed", action="install", outcome="installed", mutating=True),
        )
        fixture = self.fixture(port)
        self.assertEqual(fixture.next_action(), "inspect_skill")
        self.assertIn("Check agent Skill", fixture.host.pages[-1])
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertEqual(port.calls, ["inspect_skill"])
        self.assertEqual(fixture.next_action(), "install_skill")
        self.assertIn("Install agent Skill", fixture.host.pages[-1])
        self.assertTrue(fixture.click_next(request_id=CTRL_TESTS.OTHER_REQUEST_ID))
        fixture.settle()
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])
        self.assertEqual(fixture.views[-1].snapshot.code, "skill_installed")
        self.assertIn("agent Skill was installed", fixture.host.pages[-1])
        self.assertEqual(fixture.next_action(), "inspect_skill")

    def test_a_second_click_on_the_same_page_issues_nothing(self) -> None:
        port = ScriptedSkillPort(
            outcome("absent", action="install", outcome="planned"),
            outcome("installed", action="install", outcome="installed", mutating=True),
        )
        fixture = self.fixture(port)
        fixture.click_next()
        fixture.settle()
        self.assertEqual(fixture.next_action(), "install_skill")
        message = CTRL_TESTS.click("install_skill", fixture.capability())
        self.assertTrue(fixture.controller.handle_web_message(message))
        fixture.settle()
        # The view armed itself for the first click, so the repeat is not a
        # second write: the port saw exactly one install.
        self.assertTrue(fixture.controller.handle_web_message(message))
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])

    def test_a_click_for_another_selected_session_is_ignored(self) -> None:
        port = ScriptedSkillPort()
        fixture = self.fixture(port)
        fixture.host.selection = CONTROLLER.RemoteUpdateSelection(
            CTRL_TESTS.OTHER_SESSION, CTRL_TESTS.WORKSPACE, fixture.flow.flow
        )
        self.assertTrue(
            fixture.controller.handle_web_message(
                CTRL_TESTS.click("inspect_skill", fixture.capability())
            )
        )
        self.assertEqual(port.calls, [])
        self.assertEqual(fixture.host.retired, 1)

    def test_an_existing_update_journey_is_untouched(self) -> None:
        flow = SkillFlow(ScriptedSkillPort())
        fixture = CTRL_TESTS.Fixture(flow)
        self.addCleanup(fixture.close)
        fixture.controller.open()
        self.assertEqual(fixture.next_action(), "preview_server_update")
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertEqual(flow.names(), ["preview.describe"])
        self.assertEqual(fixture.views[-1].snapshot.code, "unknown")


class RealHelperDocumentTests(unittest.TestCase):
    """The admission against the real helper's real bytes, no ssh involved.

    This runs ``remote_skill_install.py`` as its own process with the exact
    flags the transport sends, against a planted verified application and a
    temporary home directory, and feeds the process's actual stdout and exit
    code through the admission.  It proves the frozen document shape is the
    helper's and not a transcription of it.  It is not an end-to-end proof:
    nothing here goes over ssh.
    """

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="skill-gui-"))
        self.addCleanup(self._clean)
        self.app = HELPER_TESTS.plant_verified_app(self.base / "app")
        self.home = self.base / "home"
        self.home.mkdir()

    def _clean(self) -> None:
        import shutil

        shutil.rmtree(self.base, ignore_errors=True)

    def run_helper(self, *flags: str) -> tuple[object, int]:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(self.app / "desktop" / "python-webview-shell" / "remote_skill_install.py"),
                "--install-root",
                str(self.app),
                *flags,
            ],
            capture_output=True,
            timeout=120,
            check=False,
            env={**os.environ, "HOME": str(self.home)},
        )
        return json.loads(completed.stdout.decode("ascii")), completed.returncode

    def test_the_real_inspect_install_and_reinspect_are_all_admitted(self) -> None:
        version = HELPER_TESTS.V2
        planned, code = self.run_helper()
        admitted = PORT.admit_skill_outcome(
            planned, code, expected_version=version, mutating=False
        )
        assert admitted is not None, planned
        self.assertEqual(admitted.status, "absent")
        self.assertTrue(admitted.offers_install)

        written, code = self.run_helper("--apply")
        installed = PORT.admit_skill_outcome(
            written, code, expected_version=version, mutating=True
        )
        assert installed is not None, written
        self.assertEqual(installed.status, "installed")
        self.assertEqual(PORT.skill_flow_code(installed), "skill_installed")
        self.assertTrue((self.home / ".agents" / "skills" / "work-stack" / "SKILL.md").is_file())

        again, code = self.run_helper()
        current = PORT.admit_skill_outcome(
            again, code, expected_version=version, mutating=False
        )
        assert current is not None, again
        self.assertEqual(current.status, "current")
        self.assertFalse(current.offers_install)

    def test_a_hand_edited_destination_is_refused_and_the_edit_is_kept(self) -> None:
        self.run_helper("--apply")
        edited = self.home / ".agents" / "skills" / "work-stack" / "SKILL.md"
        edited.write_bytes(b"the operator edited this\n")
        document, code = self.run_helper("--apply")
        refused = PORT.admit_skill_outcome(
            document, code, expected_version=HELPER_TESTS.V2, mutating=True
        )
        assert refused is not None, document
        self.assertEqual(refused.status, "refused")
        self.assertEqual(
            PORT.skill_flow_code(refused), "skill_refused_destination_modified"
        )
        self.assertEqual(edited.read_bytes(), b"the operator edited this\n")

    def test_no_home_file_outside_the_skill_directory_is_written(self) -> None:
        (self.home / ".bashrc").write_bytes(b"# operator profile\n")
        before = (self.home / ".bashrc").read_bytes()
        self.run_helper()
        self.run_helper("--apply")
        self.assertEqual((self.home / ".bashrc").read_bytes(), before)
        self.assertEqual(
            sorted(item.name for item in self.home.iterdir()), [".agents", ".bashrc"]
        )


class OfferStateTests(unittest.TestCase):
    """The extracted offer seam, tested once on its own terms."""

    def test_the_gate_names_one_refusal_per_illegal_condition(self) -> None:
        state = OFFER.SkillState()
        port = ScriptedSkillPort()
        self.assertEqual(
            OFFER.admit_skill_call(state, None, ready=True, mutating=False),
            OFFER.REFUSED_UNAVAILABLE,
        )
        self.assertEqual(
            OFFER.admit_skill_call(state, port, ready=False, mutating=False),
            OFFER.REFUSED_NOT_READY,
        )
        self.assertEqual(
            OFFER.admit_skill_call(state.opened(), port, ready=True, mutating=False),
            OFFER.REFUSED_IN_FLIGHT,
        )
        self.assertEqual(
            OFFER.admit_skill_call(state, port, ready=True, mutating=True),
            OFFER.REFUSED_INSPECT_REQUIRED,
        )
        self.assertEqual(OFFER.admit_skill_call(state, port, ready=True, mutating=False), "")

    def test_arming_withdraws_the_offer_until_an_answer_settles_it(self) -> None:
        armed = OFFER.SkillState().settled(
            outcome("absent", action="install", outcome="planned")
        ).opened()
        self.assertEqual(armed.actions(available=True), ())
        settled = armed.settled(outcome(PORT.STATUS_UNKNOWN, mutating=True))
        self.assertEqual(settled.actions(available=True), (OFFER.ACTION_INSPECT,))
        self.assertEqual(settled.actions(available=False), ())

    def test_every_skill_code_has_copy_that_keeps_the_update_successful(self) -> None:
        self.assertEqual(COPY.SKILL_CODES, PORT.SKILL_FLOW_CODES)
        for code in COPY.SKILL_CODES:
            headline = COPY.skill_headline(code)
            assert headline is not None
            self.assertTrue(headline.startswith(COPY.READY_SENTENCE), code)
            self.assertIn("agent Skill", headline)
        self.assertIsNone(COPY.skill_headline("update_ready"))
        for action in ("inspect_skill", "install_skill", "update_skill"):
            self.assertIn("agent Skill", COPY.skill_action_detail(action))
        self.assertEqual(COPY.skill_action_detail("continue_flow"), "")

    def test_a_stale_origin_epoch_does_not_run_the_port(self) -> None:
        port = ScriptedSkillPort(
            outcome("absent", action="install", outcome="planned"),
            outcome("installed", action="install", outcome="installed", mutating=True),
        )
        offer = OFFER.SkillOffer(port)
        origin = offer.epoch
        offer.reopened()
        self.assertEqual(
            offer.run(
                mutating=False, ready=True, published="update_ready", origin_epoch=origin
            ).code,
            "",
        )
        self.assertEqual(port.calls, [])
        self.assertEqual(offer.actions(), (OFFER.ACTION_INSPECT,))
        self.assertEqual(
            offer.run(
                mutating=False, ready=True, published="update_ready", origin_epoch=offer.epoch
            ).code,
            "skill_absent",
        )
        origin = offer.epoch
        self.assertEqual(offer.reopened(), "update_ready")
        self.assertEqual(
            offer.run(
                mutating=True, ready=True, published="update_ready", origin_epoch=origin
            ).code,
            "",
        )
        self.assertEqual(port.calls, ["inspect_skill"])
        self.assertFalse(offer.state.in_flight)
        self.assertEqual(offer.actions(), (OFFER.ACTION_INSPECT,))


class GatedSkillPort:
    """A ``SkillPort`` whose gated call blocks until the test releases it.

    ``inspect`` is an optional immediate inspect answer so a write can be the
    call that is held; without it every call waits on ``release``.
    """

    def __init__(self, answer: object, *, inspect: object | None = None) -> None:
        self.answer = answer
        self.inspect_answer = inspect
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def _take(self, name: str) -> object:
        self.calls.append(name)
        self.entered.set()
        assert self.release.wait(CTRL_TESTS.WAIT_SECONDS), "the gated call was never released"
        return self.answer

    def inspect_skill(self) -> object:
        if self.inspect_answer is not None:
            self.calls.append("inspect_skill")
            return self.inspect_answer
        return self._take("inspect_skill")

    def install_skill(self) -> object:
        return self._take("install_skill")


class SettledPause:
    """Hold one Skill call between settling its answer and publishing it.

    The port has already replied and ``SkillOffer`` has already taken that
    reply for the page that asked; what has not happened yet is the flow
    publishing it.  That is the window a retire-and-open lands in, and the
    only way to reach it is from inside the call itself.
    """

    def __init__(self, offer: object) -> None:
        self._offer = offer
        self._run = offer.run
        self.entered = threading.Event()
        self.release = threading.Event()
        offer.run = self._paused

    def _paused(self, **kwargs: object) -> object:
        reply = self._run(**kwargs)
        self.entered.set()
        assert self.release.wait(CTRL_TESTS.WAIT_SECONDS), "the settled reply was never released"
        return reply


class HeldWorker:
    """A ``worker_factory`` stand-in: tokens stay queued until the test runs them."""

    def __init__(self, handler: object, deliver: object, **_ignored: object) -> None:
        self._handler = handler
        self._deliver = deliver
        self.pending: list[str] = []

    def start(self) -> bool:
        return True

    def submit(self, token: str) -> bool:
        self.pending.append(token)
        return True

    def run_one(self) -> None:
        token = self.pending.pop(0)
        self._deliver(self._handler(token))

    def stop(self, _timeout: float | None = None) -> None:
        return None


class PausedSubmitController(CONTROLLER.RemoteUpdateController):
    """Stop after the old view is accepted, before submit binds the call."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.entered_submit = threading.Event()
        self.resume_submit = threading.Event()
        super().__init__(*args, **kwargs)

    def _submit_flow_call(
        self,
        kind: str | None,
        bound: object,
        generation: int,
        origin_epoch: int,
    ) -> bool:
        self.entered_submit.set()
        assert self.resume_submit.wait(CTRL_TESTS.WAIT_SECONDS), "submit was never resumed"
        return super()._submit_flow_call(kind, bound, generation, origin_epoch)


class QueuedFixture(CTRL_TESTS.Fixture):
    """The controller suite fixture, with the worker held at submit."""

    def __init__(
        self,
        flow: object,
        pc_actions: tuple[str, ...] = (),
        *,
        controller_cls: type = CONTROLLER.RemoteUpdateController,
    ) -> None:
        self.flow = flow
        self.views: list = []
        self.selection = CONTROLLER.RemoteUpdateSelection(
            CTRL_TESTS.SESSION, CTRL_TESTS.WORKSPACE, flow.flow
        )
        self.host = CTRL_TESTS.Host(self.selection, pc_actions)
        self.held: HeldWorker

        def factory(handler: object, deliver: object, **kwargs: object) -> HeldWorker:
            self.held = HeldWorker(handler, deliver, **kwargs)
            return self.held

        self.controller = controller_cls(
            self.host.hooks(),
            view_factory=self._view,
            worker_factory=factory,
            trace=self.host.traces.append,
        )


class ReopenTests(unittest.TestCase):
    """A new page asks again; it never inherits the closed page's answer.

    The host keeps one composed flow for an unchanged binding, so every test
    here reopens the real controller over the very same ``RemoteUpdateFlow``
    object the first page spoke for -- which is exactly the condition in which
    a cached offer or a cached success would otherwise survive a close.
    """

    def fixture(self, port: object) -> object:
        return CallerChainTests.fixture(self, port)

    def queued_fixture(
        self,
        port: object,
        *,
        controller_cls: type = CONTROLLER.RemoteUpdateController,
    ) -> QueuedFixture:
        flow = SkillFlow(port)
        flow.run(6)
        flow.flow.resume_after_restart(
            CTRL_TESTS.StartupEvidence(
                selected_activation_id=flow.flow.retained_activation().operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        flow.flow.advance()
        self.assertEqual(flow.flow.snapshot().stage, "ready")
        fixture = QueuedFixture(flow, controller_cls=controller_cls)
        self.addCleanup(fixture.close)
        self.assertTrue(fixture.controller.open())
        return fixture

    def run_queued(self, fixture: QueuedFixture) -> None:
        self.assertEqual(len(fixture.held.pending), 1)
        fixture.held.run_one()
        if fixture.host.queued():
            fixture.host.pump()

    def reopen(self, fixture: object) -> None:
        """Close the page and mount a new one over the same flow."""

        flow = fixture.flow.flow
        fixture.controller.retire(closed=True)
        self.assertFalse(fixture.controller.is_open)
        self.assertTrue(fixture.controller.open())
        self.assertIs(fixture.host.selection.flow, flow)

    def open_before_release(self, fixture: object, port: GatedSkillPort) -> None:
        """Mount a new page while the gated call is still out."""

        mounted = threading.Event()
        result: list[object] = []

        def mount() -> None:
            result.append(fixture.controller.open())
            mounted.set()

        thread = threading.Thread(target=mount)
        thread.start()
        self.addCleanup(port.release.set)
        self.assertTrue(
            mounted.wait(CTRL_TESTS.WAIT_SECONDS),
            "open blocked on the in-flight Skill call",
        )
        thread.join(CTRL_TESTS.WAIT_SECONDS)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [True])

    def assert_no_write_offer(self, fixture: object, *forbidden: str) -> None:
        page = fixture.host.pages[-1]
        self.assertNotIn("Install agent Skill", page)
        self.assertNotIn("Update agent Skill", page)
        for text in forbidden:
            self.assertNotIn(text, page)

    def assert_pending_protections(self, fixture: object, port: GatedSkillPort) -> None:
        snapshot = fixture.flow.flow.snapshot()
        self.assertNotIn("run_skill_install", snapshot.actions)
        self.assertNotIn("run_skill_update", snapshot.actions)
        self.assertNotIn("finish", snapshot.actions)
        self.assertNotIn("cancel", snapshot.actions)
        self.assert_no_write_offer(fixture)
        calls = list(port.calls)
        for operation in ("install_skill", "update_skill", "cancel"):
            self.assertTrue(
                fixture.controller.handle_web_message(
                    CTRL_TESTS.click(operation, fixture.capability())
                )
            )
        fixture.host.pump()
        self.assertEqual(port.calls, calls)

    def settle_stale(self, fixture: object, port: GatedSkillPort) -> None:
        port.release.set()
        CTRL_TESTS.wait_for(fixture.host.queued, "the stale completion to be dispatched")
        fixture.host.pump()

    def test_a_reopened_page_cannot_install_from_the_closed_pages_read(self) -> None:
        # The read says absent, the page is closed, the backing directory
        # changes out of band, and the reopened page must not still offer
        # that install.
        port = ScriptedSkillPort(
            outcome("absent", action="install", outcome="planned"),
            outcome("current", action="noop", outcome="noop"),
        )
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertEqual(fixture.next_action(), "install_skill")
        self.reopen(fixture)
        self.assertEqual(fixture.next_action(), "inspect_skill")
        self.assertIn("Check agent Skill", fixture.host.pages[-1])
        self.assertNotIn("Install agent Skill", fixture.host.pages[-1])
        # A click carrying the closed page's action is resolved against the
        # flow as it is now, so no write is dispatched.
        self.assertTrue(
            fixture.controller.handle_web_message(
                CTRL_TESTS.click("install_skill", fixture.capability())
            )
        )
        fixture.host.pump()
        self.assertEqual(port.calls, ["inspect_skill"])
        # Only a fresh read settles what may be written, and it reads the
        # directory as it is now.
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertEqual(port.calls, ["inspect_skill", "inspect_skill"])
        self.assertEqual(fixture.views[-1].snapshot.code, "skill_current")
        self.assertEqual(fixture.next_action(), "inspect_skill")

    def test_a_finished_write_is_not_headlined_again_on_the_next_page(self) -> None:
        for status, action, disposition, code in (
            ("installed", "install", "installed", "skill_installed"),
            ("updated", "update", "updated", "skill_updated"),
        ):
            with self.subTest(status):
                read = "absent" if status == "installed" else "outdated"
                port = ScriptedSkillPort(
                    outcome(read, action=action, outcome="planned"),
                    outcome(status, action=action, outcome=disposition, mutating=True),
                )
                fixture = self.fixture(port)
                self.assertTrue(fixture.click_next())
                fixture.settle()
                self.assertTrue(fixture.click_next(request_id=CTRL_TESTS.OTHER_REQUEST_ID))
                fixture.settle()
                self.assertEqual(fixture.views[-1].snapshot.code, code)
                self.reopen(fixture)
                # The update itself is still the successful one it was; only
                # the Skill sentence, which nothing has re-measured, is gone.
                snapshot = fixture.views[-1].snapshot
                self.assertEqual(snapshot.stage, "ready")
                self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
                self.assertIsNone(COPY.skill_headline("update_ready"))
                self.assertNotIn("agent Skill was", fixture.host.pages[-1])
                self.assertEqual(fixture.next_action(), "inspect_skill")
                self.assertEqual(port.calls, ["inspect_skill", "install_skill"])

    def test_an_unknown_write_stays_unknown_and_stays_read_only(self) -> None:
        port = ScriptedSkillPort(
            outcome("absent", action="install", outcome="planned"),
            outcome(PORT.STATUS_UNKNOWN, code=PORT.CHANNEL_LOST, mutating=True),
        )
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertTrue(fixture.click_next(request_id=CTRL_TESTS.OTHER_REQUEST_ID))
        fixture.settle()
        self.assertEqual(fixture.views[-1].snapshot.code, "skill_unknown")
        self.reopen(fixture)
        # Withdrawing narrows: a write whose answer never arrived is never
        # reopened as a write.
        self.assertEqual(fixture.next_action(), "inspect_skill")
        self.assertTrue(
            fixture.controller.handle_web_message(
                CTRL_TESTS.click("install_skill", fixture.capability())
            )
        )
        fixture.host.pump()
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])

    def test_the_closed_pages_late_reply_cannot_republish_its_action(self) -> None:
        port = GatedSkillPort(outcome("absent", action="install", outcome="planned"))
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        self.assertTrue(port.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        fixture.controller.retire(closed=True)
        pages = len(fixture.host.pages)
        port.release.set()
        # The reply arrives for a page that is gone, so it paints nothing...
        CTRL_TESTS.wait_for(fixture.host.queued, "the late completion to be dispatched")
        fixture.host.pump()
        self.assertEqual(len(fixture.host.pages), pages)
        self.assertEqual(fixture.flow.flow.snapshot().code, "skill_absent")
        # ...and the page mounted next is a new question, not that answer.
        self.assertTrue(fixture.controller.open())
        self.assertEqual(fixture.next_action(), "inspect_skill")
        self.assertNotIn("Install agent Skill", fixture.host.pages[-1])
        self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
        self.assertEqual(port.calls, ["inspect_skill"])

    def test_open_before_an_old_inspect_replies_does_not_offer_install(self) -> None:
        port = GatedSkillPort(outcome("absent", action="install", outcome="planned"))
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        self.assertTrue(port.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        fixture.controller.retire(closed=True)
        self.open_before_release(fixture, port)
        self.assertEqual(port.calls, ["inspect_skill"])
        self.assert_pending_protections(fixture, port)
        self.assertEqual(port.calls, ["inspect_skill"])
        self.settle_stale(fixture, port)
        self.assert_no_write_offer(fixture)
        self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
        self.assertEqual(fixture.next_action(), "inspect_skill")
        self.assertEqual(port.calls, ["inspect_skill"])
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertEqual(port.calls, ["inspect_skill", "inspect_skill"])

    def test_open_before_an_old_install_replies_does_not_reassert_success(self) -> None:
        port = GatedSkillPort(
            outcome("installed", action="install", outcome="installed", mutating=True),
            inspect=outcome("absent", action="install", outcome="planned"),
        )
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertTrue(fixture.click_next(request_id=CTRL_TESTS.OTHER_REQUEST_ID))
        self.assertTrue(port.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        fixture.controller.retire(closed=True)
        self.open_before_release(fixture, port)
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])
        self.assert_pending_protections(fixture, port)
        self.settle_stale(fixture, port)
        self.assert_no_write_offer(fixture, "agent Skill was installed")
        self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
        self.assertEqual(fixture.next_action(), "inspect_skill")
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])

    def test_open_before_an_unknown_install_replies_stays_read_only(self) -> None:
        port = GatedSkillPort(
            outcome(PORT.STATUS_UNKNOWN, code=PORT.CHANNEL_LOST, mutating=True),
            inspect=outcome("absent", action="install", outcome="planned"),
        )
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertTrue(fixture.click_next(request_id=CTRL_TESTS.OTHER_REQUEST_ID))
        self.assertTrue(port.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        fixture.controller.retire(closed=True)
        self.open_before_release(fixture, port)
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])
        self.assert_pending_protections(fixture, port)
        self.settle_stale(fixture, port)
        self.assert_no_write_offer(fixture, "agent Skill was installed")
        self.assertEqual(fixture.views[-1].snapshot.code, "skill_unknown")
        self.assertEqual(fixture.next_action(), "inspect_skill")
        self.assertTrue(
            fixture.controller.handle_web_message(
                CTRL_TESTS.click("install_skill", fixture.capability())
            )
        )
        fixture.host.pump()
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])

    def pause_before_publish(self, fixture: object) -> SettledPause:
        """Hold the next Skill call after it settles, before it publishes."""

        pause = SettledPause(fixture.flow.flow._skill)
        self.addCleanup(pause.release.set)
        return pause

    def release_and_repaint(self, fixture: object, pause: SettledPause) -> None:
        """Let the held call publish, then repaint the page that is mounted."""

        pause.release.set()
        CTRL_TESTS.wait_for(fixture.host.queued, "the released completion to be dispatched")
        fixture.host.pump()
        fixture.controller._request_paint(fixture.controller._generation)
        fixture.host.pump()

    def test_an_answer_settled_before_the_reopen_still_publishes_nothing(self) -> None:
        # The reply is in: the page that asked for it has it and the offer has
        # settled it.  Only publishing is left, and the page closes first.
        for status, action, disposition, code in (
            ("installed", "install", "installed", "skill_installed"),
            ("updated", "update", "updated", "skill_updated"),
            (PORT.STATUS_UNKNOWN, "install", "unknown", "skill_unknown"),
        ):
            with self.subTest(status):
                read = "outdated" if status == "updated" else "absent"
                port = ScriptedSkillPort(
                    outcome(read, action=action, outcome="planned"),
                    outcome(status, action=action, outcome=disposition, mutating=True),
                )
                fixture = self.fixture(port)
                self.assertTrue(fixture.click_next())
                fixture.settle()
                pause = self.pause_before_publish(fixture)
                self.assertTrue(fixture.click_next(request_id=CTRL_TESTS.OTHER_REQUEST_ID))
                self.assertTrue(pause.entered.wait(CTRL_TESTS.WAIT_SECONDS))
                self.reopen(fixture)
                self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
                self.release_and_repaint(fixture, pause)
                # The write happened and is not repeated; what the new page
                # must not have is the sentence or the offer it never earned.
                self.assertEqual(port.calls, ["inspect_skill", "install_skill"])
                self.assertNotEqual(fixture.flow.flow.snapshot().code, code)
                self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
                self.assert_no_write_offer(fixture, "agent Skill was")
                self.assertEqual(fixture.next_action(), "inspect_skill")
                self.assertTrue(
                    fixture.controller.handle_web_message(
                        CTRL_TESTS.click("install_skill", fixture.capability())
                    )
                )
                fixture.host.pump()
                self.assertEqual(port.calls, ["inspect_skill", "install_skill"])

    def test_a_read_settled_before_the_reopen_offers_no_write(self) -> None:
        # The same window on the read: an absent settled for the closed page
        # cannot earn the reopened page an Install it never asked for.
        port = ScriptedSkillPort(
            outcome("absent", action="install", outcome="planned"),
            outcome("absent", action="install", outcome="planned"),
        )
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.reopen(fixture)
        pause = self.pause_before_publish(fixture)
        self.assertTrue(fixture.click_next())
        self.assertTrue(pause.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        self.reopen(fixture)
        self.release_and_repaint(fixture, pause)
        self.assertEqual(port.calls, ["inspect_skill", "inspect_skill"])
        self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
        self.assert_no_write_offer(fixture)
        self.assertEqual(fixture.next_action(), "inspect_skill")

    def test_the_page_that_asked_still_gets_its_own_answer(self) -> None:
        # The same publication boundary with no reopen in the window: the
        # ordinary current-view read publishes exactly what it settled.
        port = ScriptedSkillPort(
            outcome("current", action="noop", outcome="noop"),
            outcome("absent", action="install", outcome="planned"),
        )
        fixture = self.fixture(port)
        pause = self.pause_before_publish(fixture)
        self.assertTrue(fixture.click_next())
        self.assertTrue(pause.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        self.release_and_repaint(fixture, pause)
        self.assertEqual(fixture.flow.flow.snapshot().code, "skill_current")
        self.assertEqual(fixture.views[-1].snapshot.code, "skill_current")
        self.assertEqual(fixture.next_action(), "inspect_skill")
        # ...and a second read on that same page still earns its Install.
        self.assertTrue(fixture.click_next(request_id=CTRL_TESTS.OTHER_REQUEST_ID))
        fixture.settle()
        self.assertEqual(fixture.next_action(), "install_skill")
        self.assertIn("Install agent Skill", fixture.host.pages[-1])
        self.assertEqual(port.calls, ["inspect_skill", "inspect_skill"])

    def test_a_late_reply_does_not_paint_a_different_selection(self) -> None:
        port = GatedSkillPort(outcome("absent", action="install", outcome="planned"))
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        self.assertTrue(port.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        fixture.controller.retire(closed=True)
        other = SkillFlow(ScriptedSkillPort())
        other.run(6)
        other.flow.resume_after_restart(
            CTRL_TESTS.StartupEvidence(
                selected_activation_id=other.flow.retained_activation().operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        other.flow.advance()
        fixture.host.selection = CONTROLLER.RemoteUpdateSelection(
            CTRL_TESTS.OTHER_SESSION, CTRL_TESTS.WORKSPACE, other.flow
        )
        self.assertTrue(fixture.controller.open())
        pages = len(fixture.host.pages)
        self.assertEqual(fixture.next_action(), "inspect_skill")
        port.release.set()
        CTRL_TESTS.wait_for(fixture.host.queued, "the other selection's completion")
        fixture.host.pump()
        self.assertEqual(len(fixture.host.pages), pages)
        self.assertNotIn("Install agent Skill", fixture.host.pages[-1])
        self.assertEqual(other.flow.snapshot().code, "update_ready")
        self.assertEqual(port.calls, ["inspect_skill"])

    def test_a_call_still_out_is_preserved_and_never_waited_on(self) -> None:
        port = GatedSkillPort(outcome("absent", action="install", outcome="planned"))
        harness = ready(port)
        flow = harness.flow
        worker = threading.Thread(target=flow.inspect_skill)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(port.release.set)
        self.assertTrue(port.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        self.assertTrue(worker.is_alive())
        flow.reopen_skill_view()
        self.assertTrue(worker.is_alive())
        self.assertTrue(flow._skill.state.in_flight)
        snapshot = flow.snapshot()
        self.assertNotIn("run_skill_install", snapshot.actions)
        self.assertNotIn("finish", snapshot.actions)
        self.assertNotIn("cancel", snapshot.actions)
        port.release.set()
        worker.join(CTRL_TESTS.WAIT_SECONDS)
        self.assertFalse(worker.is_alive())
        snapshot = flow.snapshot()
        self.assertEqual(snapshot.code, "update_ready")
        self.assertEqual(snapshot.actions, ("run_skill_inspect", "finish"))
        self.assertEqual(port.calls, ["inspect_skill"])

    def test_a_build_without_the_offer_reopens_exactly_as_it_did(self) -> None:
        fixture = self.fixture(None)
        self.assertIsNone(fixture.next_action())
        self.reopen(fixture)
        self.assertIsNone(fixture.next_action())
        self.assertNotIn("Check agent Skill", fixture.host.pages[-1])
        self.assertIn("Close", fixture.host.pages[-1])
        self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")

    def test_the_page_on_screen_keeps_what_it_published(self) -> None:
        port = ScriptedSkillPort(outcome("absent", action="install", outcome="planned"))
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertEqual(fixture.next_action(), "install_skill")
        # An ordinary repaint of the mounted page is not a new question.
        fixture.controller._request_paint(fixture.controller._generation)
        fixture.host.pump()
        self.assertEqual(fixture.next_action(), "install_skill")
        self.assertIn("Install agent Skill", fixture.host.pages[-1])

    def test_the_offer_seam_withdraws_an_answer_but_never_a_live_call(self) -> None:
        port = ScriptedSkillPort(outcome("absent", action="install", outcome="planned"))
        offer = OFFER.SkillOffer(port)
        self.assertEqual(offer.reopened(), "")
        offer.run(mutating=False, ready=True, published="update_ready")
        self.assertEqual(offer.state.offer, OFFER.ACTION_INSTALL)
        self.assertEqual(offer.reopened(), "update_ready")
        self.assertEqual(offer.state, OFFER.SkillState())
        self.assertEqual(offer.actions(), (OFFER.ACTION_INSPECT,))
        # A second withdrawal has nothing left to withdraw.
        self.assertEqual(offer.reopened(), "")
        armed = OFFER.SkillState(code="skill_absent", offer=OFFER.ACTION_INSTALL).opened()
        self.assertIs(armed.withdrawn(), armed)
        self.assertEqual(
            OFFER.SkillState(code="skill_installed").withdrawn(), OFFER.SkillState()
        )
        gated = GatedSkillPort(outcome("absent", action="install", outcome="planned"))
        live = OFFER.SkillOffer(gated)
        worker = threading.Thread(
            target=lambda: live.run(mutating=False, ready=True, published="update_ready")
        )
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(gated.release.set)
        self.assertTrue(gated.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        self.assertEqual(live.reopened(), "update_ready")
        self.assertTrue(live.state.in_flight)
        gated.release.set()
        worker.join(CTRL_TESTS.WAIT_SECONDS)
        self.assertFalse(worker.is_alive())
        self.assertEqual(live.state, OFFER.SkillState())
        self.assertEqual(live.actions(), (OFFER.ACTION_INSPECT,))

    def test_a_queued_inspect_does_not_offer_install_after_reopen(self) -> None:
        port = ScriptedSkillPort(outcome("absent", action="install", outcome="planned"))
        fixture = self.queued_fixture(port)
        self.assertTrue(fixture.click_next())
        self.assertEqual(len(fixture.held.pending), 1)
        self.reopen(fixture)
        self.run_queued(fixture)
        self.assertEqual(port.calls, [])
        self.assert_no_write_offer(fixture)
        self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
        self.assertEqual(fixture.next_action(), "inspect_skill")
        self.assertTrue(fixture.click_next())
        self.run_queued(fixture)
        self.assertEqual(port.calls, ["inspect_skill"])
        self.assertEqual(fixture.next_action(), "install_skill")

    def test_a_queued_install_does_not_mutate_after_reopen(self) -> None:
        port = ScriptedSkillPort(
            outcome("absent", action="install", outcome="planned"),
            outcome("installed", action="install", outcome="installed", mutating=True),
        )
        fixture = self.queued_fixture(port)
        self.assertTrue(fixture.click_next())
        self.run_queued(fixture)
        self.assertEqual(fixture.next_action(), "install_skill")
        self.assertTrue(fixture.click_next(request_id=CTRL_TESTS.OTHER_REQUEST_ID))
        self.assertEqual(len(fixture.held.pending), 1)
        self.reopen(fixture)
        self.run_queued(fixture)
        self.assertEqual(port.calls, ["inspect_skill"])
        self.assert_no_write_offer(fixture, "agent Skill was installed")
        self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
        self.assertEqual(fixture.next_action(), "inspect_skill")

    def test_a_paused_submit_cannot_adopt_a_new_page_epoch(self) -> None:
        port = ScriptedSkillPort(outcome("absent", action="install", outcome="planned"))
        fixture = self.queued_fixture(port, controller_cls=PausedSubmitController)
        clicker = threading.Thread(target=fixture.click_next)
        clicker.start()
        self.addCleanup(clicker.join)
        self.addCleanup(fixture.controller.resume_submit.set)
        self.assertTrue(
            fixture.controller.entered_submit.wait(CTRL_TESTS.WAIT_SECONDS),
            "the accepted click never reached submit",
        )
        self.reopen(fixture)
        fixture.controller.resume_submit.set()
        clicker.join(CTRL_TESTS.WAIT_SECONDS)
        self.assertFalse(clicker.is_alive())
        if fixture.held.pending:
            self.run_queued(fixture)
        self.assertEqual(port.calls, [])
        self.assert_no_write_offer(fixture)
        self.assertEqual(fixture.flow.flow.snapshot().code, "update_ready")
        self.assertEqual(fixture.next_action(), "inspect_skill")

    def test_a_queued_call_does_not_act_on_a_different_selection(self) -> None:
        port = ScriptedSkillPort(outcome("absent", action="install", outcome="planned"))
        fixture = self.queued_fixture(port)
        self.assertTrue(fixture.click_next())
        other_port = ScriptedSkillPort()
        other = SkillFlow(other_port)
        other.run(6)
        other.flow.resume_after_restart(
            CTRL_TESTS.StartupEvidence(
                selected_activation_id=other.flow.retained_activation().operation_id,
                authority_selected="verified",
                workspace_match="verified",
            )
        )
        other.flow.advance()
        fixture.host.selection = CONTROLLER.RemoteUpdateSelection(
            CTRL_TESTS.OTHER_SESSION, CTRL_TESTS.WORKSPACE, other.flow
        )
        self.assertTrue(fixture.controller.open())
        pages = len(fixture.host.pages)
        self.run_queued(fixture)
        self.assertEqual(len(fixture.host.pages), pages)
        self.assertNotIn("Install agent Skill", fixture.host.pages[-1])
        self.assertEqual(other.flow.snapshot().code, "update_ready")
        self.assertEqual(other_port.calls, [])
        self.assertEqual(port.calls, [])
        self.assertEqual(fixture.next_action(), "inspect_skill")

    def test_a_live_install_refuses_a_new_write_until_it_settles(self) -> None:
        port = GatedSkillPort(
            outcome("installed", action="install", outcome="installed", mutating=True),
            inspect=outcome("absent", action="install", outcome="planned"),
        )
        fixture = self.fixture(port)
        self.assertTrue(fixture.click_next())
        fixture.settle()
        self.assertTrue(fixture.click_next(request_id=CTRL_TESTS.OTHER_REQUEST_ID))
        self.assertTrue(port.entered.wait(CTRL_TESTS.WAIT_SECONDS))
        self.assertTrue(fixture.flow.flow._skill.state.in_flight)
        self.assertTrue(
            fixture.controller.handle_web_message(
                CTRL_TESTS.click("install_skill", fixture.capability())
            )
        )
        fixture.host.pump()
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])
        self.reopen(fixture)
        self.assert_pending_protections(fixture, port)
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])
        self.settle_stale(fixture, port)
        self.assertEqual(port.calls, ["inspect_skill", "install_skill"])

    def test_a_queued_inspect_on_the_current_page_still_offers_install(self) -> None:
        port = ScriptedSkillPort(outcome("absent", action="install", outcome="planned"))
        fixture = self.queued_fixture(port)
        self.assertTrue(fixture.click_next())
        self.run_queued(fixture)
        self.assertEqual(port.calls, ["inspect_skill"])
        self.assertEqual(fixture.next_action(), "install_skill")
        self.assertIn("Install agent Skill", fixture.host.pages[-1])


class StructuralTests(unittest.TestCase):
    def test_the_files_this_packet_touched_stay_inside_the_budget(self) -> None:
        for path in PRODUCTION + UNCHANGED_BUDGET:
            with self.subTest(path.name):
                self.assertLessEqual(
                    len(path.read_text(encoding="utf-8").splitlines()), MAX_FILE_LINES
                )

    def test_new_production_stays_inside_repo_metrics(self) -> None:
        for path in PRODUCTION:
            source = path.read_text(encoding="utf-8")
            self.assertLessEqual(len(source.splitlines()), MAX_FILE_LINES, path.name)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                with self.subTest(file=path.name, function=node.name):
                    self.assertLessEqual(length, MAX_FUNCTION_LINES)
                    self.assertLessEqual(PORT_TESTS.complexity(node), MAX_CCN)


if __name__ == "__main__":
    unittest.main()

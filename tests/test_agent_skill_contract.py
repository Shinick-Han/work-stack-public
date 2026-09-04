from __future__ import annotations

import importlib.util
import re
import shlex
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path("integrations/agent-skill/work-stack")
VALIDATOR_PATH = Path("quality/agent-p0-oracle/validate_skill.py")
REQUIRED_FILES = {
    "SKILL.md",
    "references/commands.md",
    "references/journal-policy.md",
}
TEXT_FENCE = re.compile(
    r"^```(?P<label>[^\r\n]*)\r?\n(?P<body>.*?)^```[ \t]*\r?$",
    re.MULTILINE | re.DOTALL,
)


def _validator_module():
    spec = importlib.util.spec_from_file_location("pinned_agent_skill_validator", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_tree(root: Path) -> dict[str, str]:
    return {
        name: (root / name).read_bytes().decode("utf-8")
        for name in sorted(REQUIRED_FILES)
    }


def _normalized_prose(text: str) -> str:
    return " ".join(text.replace("`", " ").casefold().split())


def _executable_examples(commands_text: str) -> list[str]:
    """Return commands only from fences explicitly labelled executable text."""

    commands: list[str] = []
    for match in TEXT_FENCE.finditer(commands_text):
        if match.group("label").strip().casefold() != "text":
            continue
        for raw_line in match.group("body").splitlines():
            line = raw_line.strip()
            if line:
                commands.append(line)
    return commands


def _command_kind(command: str) -> str:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as error:
        raise AssertionError("command example is not shell-tokenizable") from error
    if not tokens or tokens[0] != "<pfx>":
        raise AssertionError("command must use the configurable <pfx> placeholder")
    if tokens.count("--data-dir") != 1:
        raise AssertionError("command must contain exactly one --data-dir")
    data_index = tokens.index("--data-dir")
    if data_index + 1 >= len(tokens) or tokens[data_index + 1] != "<data-dir>":
        raise AssertionError("--data-dir must use the explicit <data-dir> placeholder")

    if "agent" in tokens:
        agent_index = tokens.index("agent")
        if tokens.count("--workspace-uid") != 1:
            raise AssertionError("agent command must contain exactly one --workspace-uid")
        uid_index = tokens.index("--workspace-uid")
        if uid_index + 1 >= len(tokens) or tokens[uid_index + 1] != "<ws-uid>":
            raise AssertionError("--workspace-uid must use the explicit <ws-uid> placeholder")
        actions = [item for item in ("status", "context", "checkpoint") if item in tokens]
        if len(actions) != 1 or tokens.index(actions[0]) <= agent_index:
            raise AssertionError("only status, context, and checkpoint are agent commands")
        action = actions[0]
        if action == "context" and not _has_flag_value(tokens, "--task", "T-0001"):
            raise AssertionError("context must select one explicit Task")
        if action == "checkpoint":
            if "--stdin" not in tokens or tokens.count("--stdin") != 1:
                raise AssertionError("checkpoint must consume the packet through --stdin")
            if not _has_flag(tokens, "--intent-id"):
                raise AssertionError("checkpoint must carry a stable caller intent ID")
        return "agent " + action

    if _contains_contiguous(tokens, ("worklog", "list")):
        # Legacy worklog has no workspace-UID argument. Identity is established
        # by the mandatory preceding agent status command; data-dir remains explicit.
        if "--workspace-uid" in tokens:
            raise AssertionError("legacy worklog list does not parse --workspace-uid")
        return "worklog list"
    raise AssertionError("command is outside the P0 allowlist")


def _has_flag(tokens: list[str], flag: str) -> bool:
    return tokens.count(flag) == 1 and tokens.index(flag) + 1 < len(tokens)


def _has_flag_value(tokens: list[str], flag: str, expected: str) -> bool:
    return _has_flag(tokens, flag) and tokens[tokens.index(flag) + 1] == expected


def _contains_contiguous(tokens: list[str], values: tuple[str, ...]) -> bool:
    width = len(values)
    return any(tuple(tokens[index : index + width]) == values for index in range(len(tokens) - width + 1))


def _semantic_violations(root: Path) -> list[str]:
    violations: list[str] = []
    try:
        texts = _read_tree(root)
    except (OSError, UnicodeDecodeError):
        return ["unreadable-tree"]
    skill = texts["SKILL.md"]
    commands = texts["references/commands.md"]
    journal = texts["references/journal-policy.md"]

    for reference in ("references/commands.md", "references/journal-policy.md"):
        if reference not in skill:
            violations.append("missing-link:" + reference)

    examples = _executable_examples(commands)
    kinds: list[str] = []
    for index, example in enumerate(examples):
        try:
            kinds.append(_command_kind(example))
        except AssertionError:
            violations.append("invalid-command:{}".format(index))
    expected = {"agent status", "agent context", "agent checkpoint", "worklog list"}
    if set(kinds) != expected or len(kinds) != len(expected):
        violations.append("command-set")

    workflow_anchors = (
        "agent status",
        "select or confirm exactly one existing task",
        "agent context",
        "agent checkpoint",
    )
    lowered_skill = _normalized_prose(skill)
    positions = [lowered_skill.find(anchor) for anchor in workflow_anchors]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        violations.append("workflow-order")
    if "meaningful milestone" not in lowered_skill or "stable intent id" not in lowered_skill:
        violations.append("checkpoint-policy")

    all_text = _normalized_prose("\n".join(texts.values()))
    if "commit_unknown" not in all_text:
        violations.append("missing-commit-unknown")
    if "stop" not in all_text or "retain the same intent id" not in all_text:
        violations.append("unknown-does-not-preserve-intent")
    positive_unknown = re.compile(
        r"(?:on|after|if)[^\n.]{0,30}commit_unknown[^\n.]{0,100}"
        r"(?:retry|continue|proceed|discard|new intent|change (?:the )?(?:key|intent))",
        re.IGNORECASE,
    )
    if positive_unknown.search(all_text):
        violations.append("unknown-continued")

    lowered_journal = _normalized_prose(journal)
    if not all(item in lowered_journal for item in ("done", "next", "blockers")):
        violations.append("journal-fields")
    if not all(
        item in lowered_journal
        for item in ("raw prompts", "command transcripts", "environment dumps", "credentials", "secrets")
    ):
        violations.append("journal-prohibitions")
    if not all(item in lowered_journal for item in ("json", "ndjson", "database", "ssot")):
        violations.append("direct-edit-prohibitions")
    return violations


GOOD_SKILL = """# Work Stack Agent Skill

Read [references/commands.md](references/commands.md) and
[references/journal-policy.md](references/journal-policy.md).

Workflow: run agent status; select or confirm exactly one existing Task; run
agent context; at a meaningful milestone use agent checkpoint with one stable
intent ID. On commit_unknown, stop and retain the same intent ID.
"""

GOOD_COMMANDS = """# Commands

`<pfx>` is configured by the user.

```text
<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> status
```
```text
<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> context --task T-0001
```
```text
<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> checkpoint --intent-id stable-0001 --stdin
```
```text
<pfx> --data-dir <data-dir> worklog list --date 2026-09-02
```
"""

GOOD_JOURNAL = """# Journal policy

Record bounded observable done, next, and blockers facts. Never include raw
prompts, command transcripts, environment dumps, credentials, or secrets.
Never directly edit JSON, NDJSON, database, or SSOT authority files.
"""


def _write_skill(
    root: Path,
    *,
    skill: str = GOOD_SKILL,
    commands: str = GOOD_COMMANDS,
    journal: str = GOOD_JOURNAL,
) -> Path:
    target = root / "work-stack"
    (target / "references").mkdir(parents=True)
    (target / "SKILL.md").write_text(skill, encoding="utf-8")
    (target / "references" / "commands.md").write_text(commands, encoding="utf-8")
    (target / "references" / "journal-policy.md").write_text(journal, encoding="utf-8")
    return target


class AgentSkillContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = _validator_module()

    def test_canonical_tree_is_exact_documentation_only_utf8(self) -> None:
        present = {
            path.relative_to(SKILL_ROOT).as_posix()
            for path in SKILL_ROOT.rglob("*")
            if path.is_file()
        }
        self.assertEqual(present, REQUIRED_FILES)
        for name in sorted(REQUIRED_FILES):
            with self.subTest(name=name):
                (SKILL_ROOT / name).read_bytes().decode("utf-8")
        report = self.validator.validate_skill(SKILL_ROOT)
        self.assertTrue(report["valid"], report["violations"])

    def test_canonical_commands_and_workflow_are_parser_valid(self) -> None:
        self.assertEqual(_semantic_violations(SKILL_ROOT), [])
        commands = _executable_examples(
            (SKILL_ROOT / "references" / "commands.md").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [_command_kind(command) for command in commands],
            ["agent status", "agent context", "agent checkpoint", "worklog list"],
        )

    def test_negative_safety_policy_is_valid_instead_of_being_treated_as_a_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-skill-negative-") as temporary:
            skill = _write_skill(Path(temporary))
            self.assertEqual(_semantic_violations(skill), [])

    def test_extra_file_and_missing_progressive_link_are_killed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-skill-tree-") as temporary:
            skill = _write_skill(Path(temporary))
            (skill / "retry.py").write_text("print('retry')\n", encoding="utf-8")
            report = self.validator.validate_skill(skill)
            identifiers = {item["id"] for item in report["violations"]}
            self.assertIn("P0-SKILL-UNEXPECTED-FILE", identifiers)
            self.assertIn("P0-SKILL-SCRIPT-PRESENT", identifiers)

            (skill / "retry.py").unlink()
            (skill / "SKILL.md").write_text(
                GOOD_SKILL.replace("references/commands.md", "commands omitted"),
                encoding="utf-8",
            )
            report = self.validator.validate_skill(skill)
            self.assertTrue(any(item["id"] == "P0-SKILL-DISCLOSURE-GAP" for item in report["violations"]))

    def test_missing_flags_forbidden_command_and_deceptive_fence_are_killed(self) -> None:
        fixtures = {
            "missing-data-dir": GOOD_COMMANDS.replace(
                "<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> context",
                "<pfx> agent --workspace-uid <ws-uid> context",
            ),
            "missing-workspace": GOOD_COMMANDS.replace(
                "agent --workspace-uid <ws-uid> status",
                "agent status",
            ),
            "forbidden-command": GOOD_COMMANDS
            + "\n```text\n<pfx> --data-dir <data-dir> task complete T-0001\n```\n",
            "deceptive-json-fence": """# Commands
`<pfx>` is configured.
```json
{"example":"<pfx> --data-dir <data-dir> agent --workspace-uid <ws-uid> status"}
```
```text
<pfx> --data-dir <data-dir> worklog list
```
""",
        }
        for label, commands in fixtures.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix="agent-skill-command-"
            ) as temporary:
                skill = _write_skill(Path(temporary), commands=commands)
                self.assertNotEqual(_semantic_violations(skill), [])

    def test_unknown_retry_direct_edit_collection_and_personal_path_are_killed(self) -> None:
        malicious = {
            "retry-unknown": (
                GOOD_SKILL.replace(
                    "On commit_unknown, stop and retain the same intent ID.",
                    "On commit_unknown, retry and continue with a new intent ID.",
                ),
                GOOD_COMMANDS,
                GOOD_JOURNAL,
                "P0-SKILL-CONTINUE-AFTER-UNKNOWN",
            ),
            "direct-edit": (
                GOOD_SKILL,
                GOOD_COMMANDS,
                GOOD_JOURNAL + "\nIf blocked, update authority.json directly.\n",
                "P0-SKILL-DIRECT-SSOT-EDIT",
            ),
            "environment-collection": (
                GOOD_SKILL,
                GOOD_COMMANDS,
                GOOD_JOURNAL + "\nPaste environment variables and credentials into the checkpoint.\n",
                "P0-SKILL-SECRET-ENV-COLLECTION",
            ),
            "personal-path": (
                GOOD_SKILL + "\nRun C:\\Users\\demo\\bin\\work-stack.cmd.\n",
                GOOD_COMMANDS,
                GOOD_JOURNAL,
                "P0-SKILL-USER-PATH-WINDOWS",
            ),
        }
        for label, (skill_text, commands, journal, expected) in malicious.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix="agent-skill-malicious-"
            ) as temporary:
                skill = _write_skill(
                    Path(temporary),
                    skill=skill_text,
                    commands=commands,
                    journal=journal,
                )
                report = self.validator.validate_skill(skill)
                identifiers = {item["id"] for item in report["violations"]}
                self.assertIn(expected, identifiers)
                self.assertFalse(report["valid"])


if __name__ == "__main__":
    unittest.main()

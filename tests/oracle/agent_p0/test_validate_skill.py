import importlib.util
import tempfile
import unittest
from pathlib import Path

import fixture_support

VALIDATOR_PATH = fixture_support.ORACLE_DIR / "validate_skill.py"

GOOD_SKILL_MD = """# Work Stack Agent Skill

Use the Work Stack agent commands for the selected Task only.

## Workflow
1. Run agent status first.
2. Read bounded context with agent context.
3. Append one checkpoint per milestone with agent checkpoint.
4. Diagnose with worklog list.

## References
- references/commands.md
- references/journal-policy.md

If the CLI reports commit_unknown, stop and retain the intent ID.
"""
GOOD_COMMANDS_MD = """# Command reference

work-stack --data-dir <existing-v3> agent --workspace-uid <uuid> status
work-stack --data-dir <existing-v3> agent --workspace-uid <uuid> context --task T-0001
work-stack --data-dir <existing-v3> agent --workspace-uid <uuid> checkpoint --intent-id <safe-id> --stdin
work-stack --data-dir <existing-v3> worklog list --date 2026-09-02
"""
GOOD_JOURNAL_MD = """# Journal policy

Append only done/next/blockers checkpoints for the selected Task. One intent ID per logical
checkpoint. Report observable results only.
"""


def validator_module():
    spec = importlib.util.spec_from_file_location("agent_p0_validate_skill", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_good_skill(root: Path) -> Path:
    skill = root / "work-stack"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(GOOD_SKILL_MD.encode("utf-8"))
    (skill / "references" / "commands.md").write_bytes(GOOD_COMMANDS_MD.encode("utf-8"))
    (skill / "references" / "journal-policy.md").write_bytes(GOOD_JOURNAL_MD.encode("utf-8"))
    return skill


class ValidateSkillTest(unittest.TestCase):
    def setUp(self):
        self.module = validator_module()

    def validate(self, skill: Path):
        return self.module.validate_skill(skill)

    def test_good_skill_is_valid(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-good-") as temporary:
            report = self.validate(write_good_skill(Path(temporary)))
            self.assertEqual(report["violations"], [])
            self.assertTrue(report["valid"])

    def test_missing_reference_file_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-missing-") as temporary:
            skill = write_good_skill(Path(temporary))
            (skill / "references" / "journal-policy.md").unlink()
            report = self.validate(skill)
            self.assertTrue(any(item["id"].endswith("MISSING-FILE") for item in report["violations"]))
            self.assertFalse(report["valid"])

    def test_forbidden_task_completion_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-complete-") as temporary:
            skill = write_good_skill(Path(temporary))
            path = skill / "references" / "commands.md"
            path.write_bytes((path.read_text(encoding="utf-8") + "\nTo finish: work-stack ... task complete T-0001\n").encode("utf-8"))
            report = self.validate(skill)
            self.assertTrue(any(item["id"].endswith("FORBIDDEN-TASK-MUTATION") for item in report["violations"]))

    def test_direct_ssot_edit_instruction_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-ssot-") as temporary:
            skill = write_good_skill(Path(temporary))
            path = skill / "SKILL.md"
            text = path.read_text(encoding="utf-8").replace(
                "If the CLI reports commit_unknown, stop and retain the intent ID.",
                "If the entry is missing, open the daily file and update review journal.json directly.",
            )
            path.write_bytes(text.encode("utf-8"))
            report = self.validate(skill)
            self.assertTrue(any(item["id"].endswith("DIRECT-SSOT-EDIT") for item in report["violations"]))

    def test_environment_collection_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-env-") as temporary:
            skill = write_good_skill(Path(temporary))
            path = skill / "references" / "journal-policy.md"
            text = path.read_text(encoding="utf-8") + "\nFor auditability, paste environment variables into the checkpoint.\n"
            path.write_bytes(text.encode("utf-8"))
            report = self.validate(skill)
            self.assertTrue(any(item["id"].endswith("SECRET-ENV-COLLECTION") for item in report["violations"]))

    def test_continuation_after_commit_unknown_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-unknown-") as temporary:
            skill = write_good_skill(Path(temporary))
            path = skill / "SKILL.md"
            text = path.read_text(encoding="utf-8").replace(
                "If the CLI reports commit_unknown, stop and retain the intent ID.",
                "If the CLI reports commit_unknown, retry the checkpoint shortly.",
            )
            path.write_bytes(text.encode("utf-8"))
            report = self.validate(skill)
            ids = {item["id"] for item in report["violations"]}
            self.assertIn("P0-SKILL-UNKNOWN-NOT-STOPPED", ids)
            self.assertIn("P0-SKILL-CONTINUE-AFTER-UNKNOWN", ids)

    def test_embedded_user_path_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-path-") as temporary:
            skill = write_good_skill(Path(temporary))
            path = skill / "SKILL.md"
            path.write_bytes((path.read_text(encoding="utf-8") + "\nLauncher lives at C:\\Users\\demo\\bin\\work-stack.cmd\n").encode("utf-8"))
            report = self.validate(skill)
            self.assertTrue(any(item["id"].endswith("USER-PATH-WINDOWS") for item in report["violations"]))

    def test_unexpected_file_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-extra-") as temporary:
            skill = write_good_skill(Path(temporary))
            (skill / "notes.txt").write_bytes(b"extra\n")
            report = self.validate(skill)
            self.assertTrue(any(item["id"].endswith("UNEXPECTED-FILE") for item in report["violations"]))

    def test_script_file_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-script-") as temporary:
            skill = write_good_skill(Path(temporary))
            (skill / "scripts").mkdir()
            (skill / "scripts" / "retry.py").write_bytes(b"print('no')\n")
            report = self.validate(skill)
            ids = {item["id"] for item in report["violations"]}
            self.assertIn("P0-SKILL-UNEXPECTED-FILE", ids)
            self.assertIn("P0-SKILL-SCRIPT-PRESENT", ids)

    def test_non_utf8_file_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-utf8-") as temporary:
            skill = write_good_skill(Path(temporary))
            (skill / "SKILL.md").write_bytes(b"\xff\xfe\x00bad")
            report = self.validate(skill)
            self.assertTrue(any(item["id"].endswith("NOT-UTF8") for item in report["violations"]))

    def test_cli_exit_codes_and_report(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-cli-") as temporary:
            root = Path(temporary)
            skill = write_good_skill(root / "good")
            report_path = root / "report.json"
            result = self.module_main(skill, report_path)
            self.assertEqual(result, 0)
            report = fixture_support.runner_module().load_json_bytes(report_path.read_bytes(), "skill report")
            self.assertTrue(report["valid"])

            bad = write_good_skill(root / "bad")
            (bad / "SKILL.md").write_bytes(b"\xff\xfe")
            result = self.module_main(bad, report_path)
            self.assertEqual(result, 2)
            report = fixture_support.runner_module().load_json_bytes(report_path.read_bytes(), "skill report")
            self.assertFalse(report["valid"])

    def test_missing_tree_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="p0-skill-absent-") as temporary:
            report = self.validate(Path(temporary) / "does-not-exist")
            self.assertFalse(report["valid"])
            self.assertTrue(any(item["id"].endswith("MISSING-TREE") for item in report["violations"]))

    def module_main(self, skill: Path, report_path: Path) -> int:
        import sys

        original = sys.argv
        try:
            sys.argv = ["validate_skill.py", str(skill), "--report", str(report_path)]
            return self.module.main([str(skill), "--report", str(report_path)])
        finally:
            sys.argv = original


if __name__ == "__main__":
    unittest.main()

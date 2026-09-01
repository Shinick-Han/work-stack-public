from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    def test_accepts_explicit_candidate_identity_and_builds_once(self) -> None:
        self.assertIn("candidate_sha:", self.workflow)
        self.assertIn("version:", self.workflow)
        self.assertIn("previous_version:", self.workflow)
        self.assertIn("release_base_sha:", self.workflow)
        self.assertIn("release-build:", self.workflow)
        self.assertEqual(self.workflow.count("Build immutable Windows release bundle"), 1)
        self.assertIn("Assert clean absent release outputs", self.workflow)
        self.assertIn("Verify frozen frontend tree", self.workflow)

    def test_every_release_exercises_the_previous_installer_upgrade_and_rollback(self) -> None:
        body = self._job_body("windows-extended")

        self.assertIn("Test-WorkStackUpgrade.ps1", body)
        self.assertIn("inputs.previous_version", body)
        self.assertIn("work-stack-public/releases/download", body)

    def test_browser_compatibility_installs_locked_python_runtime_dependencies(self) -> None:
        body = self._job_body("browser-compat")

        self.assertIn("python -m pip install --require-hashes -r requirements.txt", body)
        self.assertLess(
            body.index("python -m pip install --require-hashes -r requirements.txt"),
            body.index("npm --prefix frontend run test:e2e:compat"),
        )

    def test_downstream_jobs_consume_numeric_artifact_id_and_never_rebuild(self) -> None:
        self.assertGreaterEqual(
            self.workflow.count("artifact-ids: ${{ needs.release-build.outputs.artifact_id }}"), 2
        )
        self.assertNotRegex(self.workflow, r"(?m)^\s+name:\s+work-stack-release-")
        for job in ("windows-first-launch", "publish"):
            body = self._job_body(job)
            self.assertNotIn("npm run build", body)
            self.assertNotIn("Build-WindowsInstaller", body)
            self.assertNotIn("actions/checkout", body)

    def test_release_policy_has_explicit_required_needs_and_publish_is_guarded(self) -> None:
        body = self._job_body("release-policy")
        for required in (
            "release-build",
            "quality",
            "chromium-smoke",
            "windows-first-launch",
            "browser-compat",
            "targeted-mutation",
            "windows-extended",
        ):
            self.assertIn(required, body)
        publish = self._job_body("publish")
        self.assertIn("needs.release-policy.outputs.allow_publish == 'true'", publish)
        self.assertIn("Shinick-Han/work-stack-public", publish)

    def test_all_external_actions_are_pinned_to_full_commit_sha(self) -> None:
        all_workflows = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        )
        action_lines = re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", all_workflows)
        self.assertTrue(action_lines)
        for action in action_lines:
            if action.startswith("./"):
                continue
            self.assertRegex(action, r"@[0-9a-f]{40}\Z", action)

    def _job_body(self, name: str) -> str:
        match = re.search(
            rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
            self.workflow,
        )
        self.assertIsNotNone(match, name)
        return match.group("body") if match else ""


if __name__ == "__main__":
    unittest.main()

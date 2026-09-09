from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release.yml"
GUIDE_PATH = ROOT / "docs" / "IMMUTABLE-RELEASE-GUIDE.md"
LINUX_TARGET = "cp312-manylinux_2_17_x86_64"
LINUX_PLATFORM = "manylinux_2_17_x86_64"
LINUX_BUILDER = "scripts/build_linux_remote_artifact.py"
WINDOWS_BUILDER = "scripts/windows/Build-WindowsInstaller.ps1"
BUILDER_PATH = ROOT / "scripts" / "windows" / "Build-WindowsInstaller.ps1"


class ReleaseRemotePayloadWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.guide = GUIDE_PATH.read_text(encoding="utf-8")
        cls.release_build = cls._job_body(cls.workflow, "release-build")

    def test_release_build_cannot_silently_omit_the_linux_pair(self) -> None:
        body = self.release_build
        installer = self._windows_installer_step(body)

        self.assertEqual(1, body.count(WINDOWS_BUILDER))
        self.assertIn("-LinuxArtifactArchivePath $archive", installer)
        self.assertIn("-LinuxArtifactSidecarPath $sidecar", installer)
        self.assertIn("WorkStack-Linux-${{ inputs.version }}-" + LINUX_TARGET, installer)
        self.assertIn(
            "Linux remote pair is missing; refusing a local-only Windows release artifact.",
            installer,
        )
        self.assertLess(
            installer.index("Linux remote pair is missing"),
            installer.index(WINDOWS_BUILDER),
        )

    def test_wheel_linux_build_or_missing_pair_stops_the_windows_artifact(self) -> None:
        body = self.release_build

        self.assertIn("Locked Linux wheel download failed.", body)
        self.assertIn("Linux remote artifact build failed.", body)
        self.assertIn("Windows installer build failed.", body)
        self.assertLess(
            body.index("Locked Linux wheel download failed."),
            body.index(LINUX_BUILDER),
        )
        self.assertLess(
            body.index("Linux remote artifact build failed."),
            body.index(WINDOWS_BUILDER),
        )
        self.assertLess(body.index(LINUX_BUILDER), body.index(WINDOWS_BUILDER))
        self.assertNotIn("continue-on-error", body)

    def test_linux_wheels_are_the_locked_cp312_manylinux_set_not_a_resolver(self) -> None:
        body = self.release_build
        download = body[
            body.index("Download locked Linux remote wheels") : body.index(
                "Build Linux remote artifact pair"
            )
        ]

        self.assertIn("python -m pip download", download)
        self.assertIn("--require-hashes", download)
        self.assertIn("--only-binary=:all:", download)
        self.assertIn("--no-deps", download)
        self.assertIn("--python-version 312", download)
        self.assertIn("--implementation cp", download)
        self.assertIn("--abi cp312", download)
        self.assertIn("--abi none", download)
        self.assertIn("--platform " + LINUX_PLATFORM, download)
        self.assertIn("requirements.txt", download)
        self.assertNotIn("pip install", download)
        self.assertNotIn("pip compile", download)
        self.assertNotIn("pip freeze", body)
        self.assertEqual(1, body.count("python -m pip download"))

    def test_linux_builder_uses_the_existing_target_and_same_version_pair(self) -> None:
        body = self.release_build
        linux = body[
            body.index("Build Linux remote artifact pair") : body.index(
                "Build immutable Windows release bundle"
            )
        ]

        self.assertEqual(1, body.count(LINUX_BUILDER))
        self.assertIn(LINUX_BUILDER, linux)
        self.assertIn("--target " + LINUX_TARGET, linux)
        self.assertIn("--source-root $root", linux)
        self.assertIn("--wheelhouse $wheelhouse", linux)
        self.assertIn("--output-dir $linuxOutput", linux)
        self.assertIn("WorkStack-Linux-${{ inputs.version }}-" + LINUX_TARGET, linux)
        self.assertIn(
            "refusing a local-only Windows release artifact.",
            linux,
        )

    def test_source_bound_dist_is_ready_before_the_linux_builder(self) -> None:
        body = self.release_build

        self.assertIn("refresh-dist", body)
        self.assertLess(body.index("refresh-dist"), body.index(LINUX_BUILDER))
        self.assertLess(body.index("refresh-dist"), body.index("freeze-tree"))
        self.assertLess(body.index("freeze-tree"), body.index(LINUX_BUILDER))
        self.assertEqual(1, body.count("refresh-dist"))
        self.assertNotIn("npm --prefix frontend run build", body)

    def test_the_windows_step_consumes_that_one_dist_instead_of_rebuilding(self) -> None:
        """The single refresh is the release's only frontend build.

        The installer call itself carries the switch, so the argument reaching
        the builder is asserted rather than a token counted anywhere in the
        step. `tests/test_windows_installer_dist_mode.py` executes that same
        command against the builder's real parameter block and proves what the
        switch does; here the point is only that this workflow selects it, and
        that it does so after the explicit refresh and freeze.
        """

        body = self.release_build
        invocation = next(
            line.strip() for line in body.splitlines() if WINDOWS_BUILDER in line
        )
        builder = BUILDER_PATH.read_text(encoding="utf-8-sig")

        self.assertIn("-ConsumeAdmittedDist", invocation.split())
        self.assertIn("[switch]$ConsumeAdmittedDist", builder)
        self.assertIn(
            "$distGateCommand = if ($ConsumeAdmittedDist) { 'verify-dist' } else { 'refresh-dist' }",
            builder,
        )
        self.assertLess(body.index("refresh-dist"), body.index("-ConsumeAdmittedDist"))
        self.assertLess(body.index("freeze-tree"), body.index("-ConsumeAdmittedDist"))
        # The workflow never asks the gate to verify the dist itself: the one
        # consuming call belongs to the builder it invokes.
        self.assertNotIn("release_gate.py verify-dist", body)
        self.assertIn("-ConsumeAdmittedDist", self.guide)
        self.assertIn("it never runs `npm run build`", self.guide)

    def test_exact_candidate_version_and_one_frozen_frontend_are_preserved(self) -> None:
        body = self.release_build

        self.assertIn('ref: ${{ inputs.candidate_sha }}', body)
        self.assertIn("WorkStack-Setup-${{ inputs.version }}.ps1", body)
        self.assertEqual(1, body.count("freeze-tree"))
        self.assertEqual(1, body.count("verify-tree"))
        self.assertLess(body.index("freeze-tree"), body.index(WINDOWS_BUILDER))
        self.assertLess(body.index(WINDOWS_BUILDER), body.index("verify-tree"))
        self.assertIn("Release HEAD drifted", body)
        self.assertIn("frozen-dist-manifest.json", body)

    def test_one_immutable_artifact_and_numeric_id_downstream_are_unchanged(self) -> None:
        self.assertEqual(1, self.workflow.count("name: immutable-release-bundle"))
        self.assertEqual(1, self.workflow.count("Upload one immutable release artifact"))
        download_count = self.workflow.count(
            "artifact-ids: ${{ needs.release-build.outputs.artifact_id }}"
        )
        self.assertGreaterEqual(download_count, 2)
        assemble = self.release_build[
            self.release_build.index("Assemble and verify internal release receipt") : self.release_build.index(
                "Upload one immutable release artifact"
            )
        ]
        copied = assemble[
            assemble.index("foreach ($name in @(") : assemble.index("Copy-Item -LiteralPath 'scripts/windows/Test-WorkStackReleaseBundle.ps1'")
        ]
        self.assertIn("WorkStack-Setup-${{ inputs.version }}.ps1", copied)
        self.assertIn("frozen-dist-manifest.json", copied)
        self.assertNotIn("linux-remote", copied)
        self.assertNotIn("WorkStack-Linux-", copied)
        self.assertNotIn("linux-wheelhouse", copied)
        publish = self._job_body(self.workflow, "publish")
        self.assertIn("Shinick-Han/work-stack-public", publish)
        self.assertNotIn(LINUX_BUILDER, publish)
        self.assertNotIn(WINDOWS_BUILDER, publish)

    def test_release_policy_permissions_and_public_target_are_not_bypassed(self) -> None:
        self.assertEqual(
            1,
            len(re.findall(r"(?m)^permissions:\n  contents: read\s*$", self.workflow)),
        )
        self.assertEqual(1, self.workflow.count("environment: work-stack-public-release"))
        self.assertNotIn("contents: write", self.workflow)
        for step_title in (
            "Download locked Linux remote wheels",
            "Build Linux remote artifact pair",
            "Build immutable Windows release bundle",
        ):
            header = self._named_step(self.release_build, step_title).split("run:", 1)[0]
            self.assertNotIn("if:", header)
            self.assertNotIn("continue-on-error", header)
        policy = self._job_body(self.workflow, "release-policy")
        self.assertIn("if: always()", policy)
        self.assertIn("needs.release-policy.outputs.allow_publish == 'true'", self.workflow)
        self.assertIn("build_linux_remote_artifact.py", self.guide)
        self.assertIn(LINUX_TARGET, self.guide)
        self.assertIn("pair-admission failure stops the Windows", self.guide)

    def test_guide_does_not_add_a_second_publication_mechanism(self) -> None:
        self.assertIn("numeric artifact ID", self.guide)
        self.assertIn("Shinick-Han/work-stack-public", self.guide)
        self.assertNotIn("work-stack-release-", self.guide)

    @staticmethod
    def _job_body(workflow: str, name: str) -> str:
        match = re.search(
            rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
            workflow,
        )
        if match is None:
            raise AssertionError(f"missing job {name}")
        return match.group("body")

    @staticmethod
    def _named_step(body: str, name: str) -> str:
        match = re.search(
            rf"(?ms)^      - name: {re.escape(name)}\n(?P<body>.*?)(?=^      - name: |\Z)",
            body,
        )
        if match is None:
            raise AssertionError(f"missing step {name}")
        return match.group("body")

    @classmethod
    def _windows_installer_step(cls, body: str) -> str:
        return cls._named_step(body, "Build immutable Windows release bundle")


if __name__ == "__main__":
    unittest.main()

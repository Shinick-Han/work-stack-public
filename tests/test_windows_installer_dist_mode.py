"""The Windows builder's two dist modes, proven through the real gate.

The release must perform exactly one real frontend build. The builder therefore
grew one explicit switch, ``-ConsumeAdmittedDist``, which makes it admit an
already built tree through the shared release gate's ``verify-dist`` instead of
producing a new one with ``refresh-dist``. Everything here executes the
product's own code:

* the builder's marked dist stage is extracted verbatim and run in PowerShell;
* the release gate it calls is the real ``scripts/release_gate.py``, not a stub;
* the receipt it reads is written by the real gate's own receipt helpers;
* the workflow's installer invocation is executed and bound against the
  builder's real ``param(...)`` block, so what is asserted is the effective
  invoked command rather than tokens counted in YAML.

No installer is produced, no wheel or dependency is installed and nothing is
downloaded. The synthetic checkout deliberately has no ``frontend/node_modules``
-- that is what makes the "no second build" claim testable: ``refresh-dist``
refuses on its build path there, while ``verify-dist`` admits the same tree.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BUILDER = SCRIPTS / "windows" / "Build-WindowsInstaller.ps1"
BUILDER_REF = "scripts/windows/Build-WindowsInstaller.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
GUIDE = ROOT / "docs" / "IMMUTABLE-RELEASE-GUIDE.md"
INSTALLER_STEP = "Build immutable Windows release bundle"
VERSION = "1.0.13"
LINUX_STEM = f"WorkStack-Linux-{VERSION}-cp312-manylinux_2_17_x86_64"
# One unit separator between recorded arguments, so a path with a space in it
# still reads back as exactly one argument.
UNIT = "\x1f"

# Every file the real dist gate rosters as a frontend build input, so a
# synthetic checkout can be admitted by the real gate.
SOURCE_FILES = {
    "desktop/python-webview-shell/generated/theme_tokens.py": "TOKENS = {}\n",
    "frontend/index.html": "<!doctype html><html></html>\n",
    "frontend/package-lock.json": '{"lockfileVersion": 3}\n',
    "frontend/package.json": '{"name": "synthetic-frontend"}\n',
    "frontend/src/main.ts": "export const main = 1\n",
    "frontend/tsconfig.app.json": "{}\n",
    "frontend/tsconfig.json": "{}\n",
    "frontend/tsconfig.node.json": "{}\n",
    "frontend/vite.config.ts": "export default {}\n",
    "scripts/generate-theme-tokens.mjs": "export default 1\n",
    "tests/fixtures/checkpoint_change_v1.json": "{}\n",
    "theme/theme-tokens.json": "{}\n",
}
DIST_FILES = {
    "index.html": "<html>admitted</html>\n",
    "assets/app.js": "export const build = 1\n",
}
# The real gate is loaded as files, not as a package, so the synthetic checkout
# needs the same sibling set the product ships.
GATE_FILES = (
    "release_gate.py",
    "dist_source_gate.py",
    "dist_source_gate_receipt.py",
    "dist_source_gate_manifest.py",
)
# Codes only the build path of `refresh` can reach. `verify` never runs a build,
# so it can never produce one of these.
BUILD_PATH_CODES = (
    "DIST_DEPENDENCIES_MISSING",
    "DIST_BUILD_TOOL_MISSING",
    "DIST_BUILD_FAILED",
    "DIST_DEPENDENCY_LINK_FAILED",
)


def load_dist_gate() -> Any:
    spec = importlib.util.spec_from_file_location(
        "workstack_dist_gate_under_test", SCRIPTS / "dist_source_gate.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("the dist gate could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def powershell() -> str | None:
    for candidate in ("pwsh.exe", "pwsh", "powershell.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def workflow_step(name: str) -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^      - name: {re.escape(name)}\n(?P<body>.*?)(?=^      - name: |\Z)", workflow
    )
    if match is None:
        raise AssertionError(f"missing workflow step {name}")
    return match.group("body")


def release_build_job() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^  release-build:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", workflow
    )
    if match is None:
        raise AssertionError("missing job release-build")
    return match.group("body")


def installer_invocations(text: str) -> list[str]:
    """Every line that actually invokes the Windows builder, comments excluded."""

    return [
        line.strip()
        for line in text.splitlines()
        if BUILDER_REF in line and not line.strip().lstrip("&").strip().startswith("#")
    ]


def quoted(value: object) -> str:
    return str(value).replace("'", "''")


class WindowsInstallerDistModeTest(unittest.TestCase):
    """One frontend build per release, enforced by the builder itself."""

    def setUp(self) -> None:
        self.shell = powershell()
        if self.shell is None:
            self.skipTest("no PowerShell host is available")
        self.gate = load_dist_gate()
        self.temporary = tempfile.mkdtemp(prefix="workstack-dist-mode-")
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.base = Path(self.temporary)
        self.source = self.base / "source"
        self.payload = self.base / "payload"
        self.log = self.base / "invocations.log"
        for relative, text in SOURCE_FILES.items():
            path = self.source.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        self.dist = self.source / "frontend" / "dist"
        for relative, text in DIST_FILES.items():
            path = self.dist.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        for name in GATE_FILES:
            shutil.copyfile(SCRIPTS / name, self.source / "scripts" / name)
        self.payload.mkdir()
        self.admit()

    def admit(self) -> None:
        """Record the receipt the real gate itself writes for this exact tree."""

        receipt = self.gate.build_receipt(
            self.gate.source_entries(self.source), self.gate.dist_entries(self.dist)
        )
        self.gate.write_receipt(self.gate.receipt_path(self.source), receipt)

    def dist_stage(self) -> str:
        """The builder's own marked dist stage, verbatim."""

        script = BUILDER.read_text(encoding="utf-8-sig")
        start = script.index("# ---- admitted frontend dist")
        end = script.index("# ---- end admitted frontend dist")
        return script[start : script.index("\n", end) + 1]

    def run_stage(self, *, consume: bool) -> subprocess.CompletedProcess:
        """Run the real stage with a `python` that records how it was invoked."""

        body = [
            "$ErrorActionPreference = 'Stop'",
            "$WorkStackTestPython = '" + quoted(sys.executable) + "'",
            "$WorkStackInvocationLog = '" + quoted(self.log) + "'",
            "function python {",
            "    ($args | ForEach-Object { [string]$_ }) -join [char]0x1f |"
            " Add-Content -LiteralPath $WorkStackInvocationLog -Encoding utf8",
            "    & $WorkStackTestPython @args",
            "}",
            "$ConsumeAdmittedDist = [switch]$" + ("true" if consume else "false"),
            "$sourcePath = '" + quoted(self.source) + "'",
            "$payload = '" + quoted(self.payload) + "'",
            "New-Item -ItemType Directory -Force -Path (Join-Path $payload 'frontend') | Out-Null",
            self.dist_stage(),
        ]
        script = self.base / "stage.ps1"
        script.write_text("\n".join(body) + "\n", encoding="utf-8-sig")
        return subprocess.run(
            [self.shell, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )

    def invocations(self) -> list[list[str]]:
        """Every effective command line the stage handed to the interpreter."""

        if not self.log.is_file():
            return []
        return [
            line.split(UNIT)
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def operations(self) -> list[str]:
        operations = []
        for call in self.invocations():
            self.assertTrue(call[0].endswith("release_gate.py"), call)
            operations.append(call[1])
        return operations

    def staged(self) -> dict[str, bytes]:
        root = self.payload / "frontend" / "dist"
        if not root.is_dir():
            return {}
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def live(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.dist).as_posix(): path.read_bytes()
            for path in sorted(self.dist.rglob("*"))
            if path.is_file()
        }

    def refuse_consume(self, code: str) -> str:
        completed = self.run_stage(consume=True)
        output = completed.stdout + completed.stderr

        self.assertNotEqual(0, completed.returncode, output)
        self.assertIn(code, output)
        self.assertIn("refused to package this tree (verify-dist)", output)
        # The refusal happens before the copy: nothing was packaged, and the
        # staged binding was never even reached.
        self.assertEqual({}, self.staged())
        self.assertEqual(["verify-dist"], self.operations())
        return output

    # -- consume mode ---------------------------------------------------

    def test_consume_mode_invokes_verify_dist_and_never_refresh_dist(self) -> None:
        completed = self.run_stage(consume=True)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        # Exactly two interpreter calls: the admission, then the staged binding.
        self.assertEqual(["verify-dist", "verify-staged-dist"], self.operations())
        self.assertNotIn("refresh-dist", self.log.read_text(encoding="utf-8"))
        admission = self.invocations()[0]
        self.assertEqual(["--repo", str(self.source)], admission[2:])

    def test_consume_mode_still_binds_the_packaged_copy_to_the_admitted_digest(self) -> None:
        admitted = self.live()

        completed = self.run_stage(consume=True)

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(admitted, self.staged())
        self.assertIn("dist_digest", completed.stdout)
        staged_call = self.invocations()[1]
        self.assertIn("--expect", staged_call)
        self.assertTrue(staged_call[staged_call.index("--expect") + 1].startswith("sha256:"))

    def test_consume_mode_runs_no_build_where_a_refresh_could_not_even_start(self) -> None:
        """Same tree, same gate: verify admits it, refresh cannot build it.

        The synthetic checkout has no ``frontend/node_modules``, so a real
        ``refresh-dist`` refuses on its build path. That the consuming mode
        succeeds on the identical tree is direct evidence that it performs no
        second frontend build.
        """

        consumed = self.run_stage(consume=True)
        self.assertEqual(0, consumed.returncode, consumed.stdout + consumed.stderr)
        self.assertEqual(["verify-dist", "verify-staged-dist"], self.operations())

        self.log.unlink()
        shutil.rmtree(self.payload / "frontend", ignore_errors=True)
        refreshed = self.run_stage(consume=False)

        output = refreshed.stdout + refreshed.stderr
        self.assertNotEqual(0, refreshed.returncode, output)
        self.assertEqual(["refresh-dist"], self.operations())
        self.assertIn("refused to package this tree (refresh-dist)", output)
        self.assertTrue(
            any(code in output for code in BUILD_PATH_CODES),
            f"refresh did not reach the build path: {output}",
        )
        self.assertEqual({}, self.staged())

    # -- consume mode fails closed --------------------------------------

    def test_consume_mode_refuses_a_missing_receipt(self) -> None:
        self.gate.receipt_path(self.source).unlink()

        self.refuse_consume("DIST_RECEIPT_MISSING")

    def test_consume_mode_refuses_a_stale_receipt(self) -> None:
        (self.source / "frontend" / "src" / "main.ts").write_text(
            "export const main = 2\n", encoding="utf-8", newline="\n"
        )

        self.refuse_consume("DIST_SOURCE_DRIFT")

    def test_consume_mode_refuses_a_tampered_dist(self) -> None:
        (self.dist / "assets" / "app.js").write_text(
            "export const build = 666\n", encoding="utf-8", newline="\n"
        )

        self.refuse_consume("DIST_CONTENT_DRIFT")

    def test_consume_mode_refuses_an_absent_dist(self) -> None:
        shutil.rmtree(self.dist)

        completed = self.run_stage(consume=True)
        output = completed.stdout + completed.stderr

        self.assertNotEqual(0, completed.returncode, output)
        self.assertIn("refused to package this tree (verify-dist)", output)
        self.assertEqual({}, self.staged())
        self.assertEqual(["verify-dist"], self.operations())


class ReleaseWorkflowInstallerBindingTest(unittest.TestCase):
    """The workflow's own installer command, bound to the builder's real parameters.

    The workflow step is executed verbatim against a probe carrying the
    builder's real ``param(...)`` block, so the assertion is about which
    parameters the effective command line actually binds.
    """

    def setUp(self) -> None:
        self.shell = powershell()
        if self.shell is None:
            self.skipTest("no PowerShell host is available")
        self.temporary = tempfile.mkdtemp(prefix="workstack-installer-binding-")
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.base = Path(self.temporary)
        self.remote = self.base / ".artifacts" / "release-output" / "linux-remote"
        self.remote.mkdir(parents=True)
        (self.remote / f"{LINUX_STEM}.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        (self.remote / f"{LINUX_STEM}.json").write_text("{}\n", encoding="utf-8")
        self.probe = self.base / "scripts" / "windows" / "Build-WindowsInstaller.ps1"
        self.probe.parent.mkdir(parents=True)
        self.bound = self.base / "bound.txt"
        self.probe.write_text(self.probe_script(), encoding="utf-8")

    def probe_script(self) -> str:
        """The builder's real parameter block, reporting exactly what it bound.

        ``$LASTEXITCODE`` is set the way the real builder leaves it, because the
        real builder's last act is a native command; the workflow step checks it.
        """

        script = BUILDER.read_text(encoding="utf-8-sig")
        start = script.index("[CmdletBinding()]")
        end = script.index("\n)\n", start) + len("\n)\n")
        return script[start:end] + (
            "\n$report = @(\n"
            '    "ConsumeAdmittedDist=$([bool]$ConsumeAdmittedDist)"\n'
            '    "SkipWheelDownload=$([bool]$SkipWheelDownload)"\n'
            '    "OutputPath=$OutputPath"\n'
            '    "LinuxArtifactArchivePath=$LinuxArtifactArchivePath"\n'
            '    "LinuxArtifactSidecarPath=$LinuxArtifactSidecarPath"\n'
            ")\n"
            "$report | Set-Content -LiteralPath $env:WORKSTACK_BOUND_REPORT -Encoding utf8\n"
            "$global:LASTEXITCODE = 0\n"
        )

    def step_body(self) -> str:
        body = workflow_step(INSTALLER_STEP)
        run = body[body.index("run: |\n") + len("run: |\n") :]
        lines = [re.sub(r"^ {10}", "", line) for line in run.splitlines()]
        return "\n".join(lines).replace("${{ inputs.version }}", VERSION)

    def run_script(self, name: str, text: str) -> subprocess.CompletedProcess:
        script = self.base / name
        script.write_text("$ErrorActionPreference = 'Stop'\n" + text + "\n", encoding="utf-8-sig")
        return subprocess.run(
            [self.shell, "-NoProfile", "-NonInteractive", "-File", str(script)],
            cwd=str(self.base),
            env={**os.environ, "WORKSTACK_BOUND_REPORT": str(self.bound)},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    def report(self) -> dict[str, str]:
        text = self.bound.read_text(encoding="utf-8")
        return dict(line.split("=", 1) for line in text.splitlines() if line.strip())

    def test_the_workflow_command_binds_consume_mode_and_the_linux_pair(self) -> None:
        completed = self.run_script("step.ps1", self.step_body())

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        bound = self.report()
        self.assertEqual("True", bound["ConsumeAdmittedDist"])
        self.assertEqual("False", bound["SkipWheelDownload"])
        self.assertEqual(
            f".artifacts/release-output/WorkStack-Setup-{VERSION}.ps1", bound["OutputPath"]
        )
        self.assertEqual(str(self.remote / f"{LINUX_STEM}.zip"), bound["LinuxArtifactArchivePath"])
        self.assertEqual(str(self.remote / f"{LINUX_STEM}.json"), bound["LinuxArtifactSidecarPath"])

    def test_the_step_still_refuses_before_building_when_the_pair_is_absent(self) -> None:
        (self.remote / f"{LINUX_STEM}.json").unlink()

        completed = self.run_script("step.ps1", self.step_body())

        output = completed.stdout + completed.stderr
        self.assertNotEqual(0, completed.returncode, output)
        self.assertIn("Linux remote pair is missing", output)
        self.assertFalse(self.bound.exists(), "the installer builder was invoked anyway")

    def test_a_standalone_build_keeps_the_refreshing_default(self) -> None:
        """No switch, no consume mode: a local build is unchanged."""

        completed = self.run_script(
            "default.ps1", "& " + BUILDER_REF + " -OutputPath 'out.ps1'"
        )

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual("False", self.report()["ConsumeAdmittedDist"])


class ReleaseBuildsTheFrontendOnceTest(unittest.TestCase):
    """The release as a whole, and what it documents about building once."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")
        cls.builder = BUILDER.read_text(encoding="utf-8-sig")
        cls.guide = GUIDE.read_text(encoding="utf-8")

    def test_the_builder_offers_exactly_two_gate_modes_and_no_bypass(self) -> None:
        self.assertIn("[switch]$ConsumeAdmittedDist", self.builder)
        self.assertIn(
            "$distGateCommand = if ($ConsumeAdmittedDist) { 'verify-dist' } else { 'refresh-dist' }",
            self.builder,
        )
        self.assertEqual(1, self.builder.count("$distGateCommand --repo $sourcePath"))
        self.assertEqual(1, self.builder.count("'refresh-dist'"))
        self.assertEqual(1, self.builder.count("'verify-dist'"))
        # The admission and the staged binding are optional in neither mode.
        self.assertEqual(1, self.builder.count("verify-staged-dist"))
        self.assertEqual(1, self.builder.count("release_gate.py') $distGateCommand"))

    def test_the_release_performs_one_refresh_and_no_other_frontend_build(self) -> None:
        job = release_build_job()
        invocations = installer_invocations(job)

        self.assertEqual(1, len(invocations), invocations)
        self.assertIn("-ConsumeAdmittedDist", invocations[0].split())
        self.assertEqual(1, job.count("refresh-dist"))
        self.assertEqual(1, self.workflow.count("refresh-dist"))
        # The release build itself never runs the frontend build command; the
        # separate risk-selected compatibility job is not part of this release
        # artifact and builds nothing that is packaged.
        self.assertNotIn("npm --prefix frontend run build", job)
        self.assertNotIn("npm run build", job)
        self.assertEqual([], installer_invocations(self.workflow.replace(job, "")))
        self.assertLess(job.index("refresh-dist"), job.index(BUILDER_REF))
        self.assertLess(job.index("freeze-tree"), job.index(BUILDER_REF))

    def test_the_guide_states_the_enforced_once_only_contract(self) -> None:
        self.assertIn("builds the frontend once", self.guide)
        self.assertIn("-ConsumeAdmittedDist", self.guide)
        self.assertIn("verify-dist", self.guide)
        self.assertIn("it never runs `npm run build`", self.guide)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "release_gate.py"


def load_module():
    spec = importlib.util.spec_from_file_location("release_gate", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("release gate module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseClassifierTests(unittest.TestCase):
    def test_critical_and_installer_changes_select_proportional_gates(self) -> None:
        module = load_module()
        policy = module.load_path_policy(ROOT / "quality" / "release-path-policy.json")
        self.assertIn("windows_extended", policy["always_gates"])
        result = module.classify_paths(
            ["workstack/service.py", "scripts/windows/Install-WorkStack.ps1"], policy
        )
        self.assertTrue(result["gates"]["targeted_mutation"])
        self.assertTrue(result["gates"]["windows_extended"])
        self.assertTrue(result["gates"]["browser_compat"])
        self.assertEqual(result["unknown_paths"], [])

    def test_unknown_path_fails_closed_to_full_matrix(self) -> None:
        module = load_module()
        policy = module.load_path_policy(ROOT / "quality" / "release-path-policy.json")
        result = module.classify_paths(["future/new-root/file.xyz"], policy)
        self.assertEqual(result["unknown_paths"], ["future/new-root/file.xyz"])
        self.assertTrue(all(result["gates"].values()))


class CandidateIdentityTests(unittest.TestCase):
    def test_candidate_must_be_full_sha_reachable_and_match_version(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "quality.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Quality Gate"], cwd=repo, check=True)
            package = repo / "workstack"
            package.mkdir()
            (package / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
            ).stdout.strip()

            receipt = module.validate_candidate(repo, sha, "1.2.3", "main")
            self.assertEqual(receipt["candidate_sha"], sha)
            self.assertRegex(receipt["tree_sha"], r"\A[0-9a-f]{40}\Z")
            with self.assertRaisesRegex(module.ReleaseGateError, "full lowercase commit SHA"):
                module.validate_candidate(repo, "main", "1.2.3", "main")
            with self.assertRaisesRegex(module.ReleaseGateError, "source version"):
                module.validate_candidate(repo, sha, "1.2.4", "main")

    def test_change_range_refuses_an_unrelated_release_base(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "quality.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Quality Gate"], cwd=repo, check=True)
            (repo / "base.txt").write_text("base", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
            subprocess.run(["git", "checkout", "--orphan", "other"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "rm", "-rf", "."], cwd=repo, check=True, capture_output=True)
            (repo / "other.txt").write_text("other", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "other"], cwd=repo, check=True, capture_output=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()
            with self.assertRaisesRegex(module.ReleaseGateError, "ancestor"):
                module.changed_paths(repo, base, head)


class ImmutableBundleTests(unittest.TestCase):
    def test_freeze_and_bundle_verification_detect_mutation(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / "dist"
            dist.mkdir()
            (dist / "index.html").write_text("one", encoding="utf-8")
            manifest_path = root / "frozen-dist-manifest.json"
            module.freeze_tree(dist, manifest_path)
            module.verify_tree(dist, manifest_path)
            (dist / "index.html").write_text("two", encoding="utf-8")
            with self.assertRaisesRegex(module.ReleaseGateError, "frozen tree mismatch"):
                module.verify_tree(dist, manifest_path)

            (dist / "index.html").write_text("one", encoding="utf-8")
            module.verify_tree(dist, manifest_path)
            candidate = {
                "candidate_sha": "a" * 40,
                "tree_sha": "b" * 40,
                "version": "1.2.3",
            }
            bundle = root / "bundle"
            bundle.mkdir()
            installer = bundle / "WorkStack-Setup-1.2.3.ps1"
            installer.write_text("installer", encoding="utf-8")
            sidecar = bundle / "WorkStack-Setup-1.2.3.ps1.sha256"
            sidecar.write_text(f"{module.sha256_file(installer)}  {installer.name}\n", encoding="utf-8")
            update = bundle / "workstack-update.json"
            update.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "version": "1.2.3",
                        "installer": {"name": installer.name, "sha256": module.sha256_file(installer)},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            frozen = bundle / manifest_path.name
            frozen.write_bytes(manifest_path.read_bytes())
            verifier = bundle / "Test-WorkStackReleaseBundle.ps1"
            verifier.write_text("verifier", encoding="utf-8")
            module.write_build_receipt(bundle, candidate)
            verified = module.verify_bundle(bundle)
            self.assertEqual(verified["version"], "1.2.3")
            installer.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(module.ReleaseGateError, "payload hash mismatch"):
                module.verify_bundle(bundle)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell is unavailable")
    def test_shipping_powershell_verifier_accepts_exact_bundle_and_rejects_tamper(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bundle.mkdir()
            installer = bundle / "WorkStack-Setup-1.2.3.ps1"
            installer.write_text("installer", encoding="utf-8")
            digest = module.sha256_file(installer)
            (bundle / f"{installer.name}.sha256").write_bytes(
                f"{digest}  {installer.name}\n".encode("utf-8")
            )
            (bundle / "workstack-update.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "version": "1.2.3",
                        "installer": {"name": installer.name, "sha256": digest},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (bundle / "frozen-dist-manifest.json").write_text(
                '{"files":[{"path":"index.html","sha256":"' + "0" * 64 + '","size":1}],"schema_version":1}\n',
                encoding="utf-8",
            )
            shutil.copy2(
                ROOT / "scripts" / "windows" / "Test-WorkStackReleaseBundle.ps1",
                bundle / "Test-WorkStackReleaseBundle.ps1",
            )
            module.write_build_receipt(
                bundle,
                {"candidate_sha": "a" * 40, "tree_sha": "b" * 40, "version": "1.2.3"},
            )
            command = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(bundle / "Test-WorkStackReleaseBundle.ps1"),
                "-BundlePath",
                str(bundle),
            ]
            # Windows PowerShell 5.1 writes its localized error trailer to stderr in
            # the console OEM code page (CP949 on a ko-KR host). Decode leniently so a
            # non-UTF-8 byte cannot turn a captured stream into None; the ASCII
            # "hash mismatch" text and the exit code are unaffected.
            accepted = subprocess.run(
                command, text=True, capture_output=True, encoding="utf-8", errors="replace"
            )
            self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)
            installer.write_text("tampered", encoding="utf-8")
            refused = subprocess.run(
                command, text=True, capture_output=True, encoding="utf-8", errors="replace"
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("hash mismatch", (refused.stdout + refused.stderr).lower())


class DistSourceCommandTests(unittest.TestCase):
    """The dist/source gate is reachable, fail-closed and actionable from the CLI."""

    def write_repo(self, root: Path) -> None:
        (root / "frontend" / "src").mkdir(parents=True)
        (root / "frontend" / "src" / "main.tsx").write_text("export const main = 1\n", encoding="utf-8")
        (root / "frontend" / "index.html").write_text("<div id=root></div>\n", encoding="utf-8")
        (root / "frontend" / "package.json").write_text('{"name":"ui"}\n', encoding="utf-8")
        (root / "frontend" / "package-lock.json").write_text(
            '{"lockfileVersion":3}\n', encoding="utf-8"
        )
        (root / "frontend" / "vite.config.ts").write_text("export default {}\n", encoding="utf-8")
        for name in ("tsconfig.json", "tsconfig.app.json", "tsconfig.node.json"):
            (root / "frontend" / name).write_text("{}\n", encoding="utf-8")
        (root / "scripts").mkdir()
        (root / "scripts" / "generate-theme-tokens.mjs").write_text("export const t = 1\n", encoding="utf-8")
        (root / "theme").mkdir()
        (root / "theme" / "theme-tokens.json").write_text('{"color":{}}\n', encoding="utf-8")
        generated = root / "desktop" / "python-webview-shell" / "generated"
        generated.mkdir(parents=True)
        (generated / "theme_tokens.py").write_text("TOKENS = {}\n", encoding="utf-8")
        fixtures = root / "tests" / "fixtures"
        fixtures.mkdir(parents=True)
        (fixtures / "checkpoint_change_v1.json").write_text('{"event_id":1}\n', encoding="utf-8")
        dist = root / "frontend" / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>stale</html>\n", encoding="utf-8")

    def run_gate(self, *arguments: str):
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), *arguments],
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_verify_dist_refuses_an_unreceipted_tree_with_the_repair_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.write_repo(repo)

            refused = self.run_gate("verify-dist", "--repo", str(repo))

            self.assertEqual(1, refused.returncode)
            output = refused.stdout + refused.stderr
            self.assertIn("DIST_RECEIPT_MISSING", output)
            self.assertIn("refresh-dist", output)

    def test_verify_dist_accepts_a_tree_its_recorded_source_built(self) -> None:
        module = load_module()
        gate = module.load_dist_source_gate()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.write_repo(repo)
            gate.write_receipt(
                gate.receipt_path(repo),
                gate.build_receipt(
                    gate.source_entries(repo), gate.dist_entries(repo / "frontend" / "dist")
                ),
            )

            accepted = self.run_gate("verify-dist", "--repo", str(repo))
            self.assertEqual(0, accepted.returncode, accepted.stdout + accepted.stderr)
            self.assertIn("source_digest", accepted.stdout)

            (repo / "frontend" / "src" / "main.tsx").write_text(
                "export const main = 2\n", encoding="utf-8"
            )
            refused = self.run_gate("verify-dist", "--repo", str(repo))

            self.assertEqual(1, refused.returncode)
            self.assertIn("DIST_SOURCE_DRIFT", refused.stdout + refused.stderr)

    def test_refresh_dist_reports_a_missing_build_tool_instead_of_trusting(self) -> None:
        module = load_module()
        gate = module.load_dist_source_gate()
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.write_repo(repo)
            with self.assertRaises(gate.DistSourceGateError) as raised:
                gate.refresh(repo, build_command=("workstack-absent-build-tool", "run", "build"))
            self.assertEqual("DIST_BUILD_TOOL_MISSING", raised.exception.code)
            self.assertFalse(gate.receipt_path(repo).exists())

    def test_verify_staged_dist_binds_a_package_copy_to_the_admitted_digest(self) -> None:
        """A packager proves what it copied without reading the live tree again."""

        module = load_module()
        gate = module.load_dist_source_gate()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            self.write_repo(repo)
            gate.write_receipt(
                gate.receipt_path(repo),
                gate.build_receipt(
                    gate.source_entries(repo), gate.dist_entries(repo / "frontend" / "dist")
                ),
            )
            admitted = json.loads(self.run_gate("verify-dist", "--repo", str(repo)).stdout)
            self.assertTrue(admitted["dist_digest"].startswith("sha256:"))
            staged = root / "payload" / "frontend" / "dist"
            staged.parent.mkdir(parents=True)
            shutil.copytree(repo / "frontend" / "dist", staged)
            # The live tree is rewritten the moment the gate call returned. The
            # staged copy is what the package ships, and it is still admitted.
            (repo / "frontend" / "dist" / "index.html").write_text(
                "<html>swapped</html>\n", encoding="utf-8"
            )

            accepted = self.run_gate(
                "verify-staged-dist", "--staged", str(staged), "--expect", admitted["dist_digest"]
            )
            self.assertEqual(0, accepted.returncode, accepted.stdout + accepted.stderr)
            self.assertIn(admitted["dist_digest"], accepted.stdout)

            (staged / "index.html").write_text("<html>swapped</html>\n", encoding="utf-8")
            refused = self.run_gate(
                "verify-staged-dist", "--staged", str(staged), "--expect", admitted["dist_digest"]
            )

            self.assertEqual(1, refused.returncode)
            output = refused.stdout + refused.stderr
            self.assertIn("DIST_STAGED_DRIFT", output)
            self.assertIn("was admitted", output)

    def test_verify_staged_dist_refuses_an_unbound_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_repo(root)

            refused = self.run_gate(
                "verify-staged-dist",
                "--staged",
                str(root / "frontend" / "dist"),
                "--expect",
                "looked-fine-to-me",
            )

            self.assertEqual(1, refused.returncode)
            self.assertIn("DIST_STAGED_UNBOUND", refused.stdout + refused.stderr)

    def test_every_dist_subcommand_is_exposed(self) -> None:
        module = load_module()
        parser = module.build_parser()
        choices = parser._subparsers._group_actions[0].choices

        self.assertIn("verify-dist", choices)
        self.assertIn("refresh-dist", choices)
        self.assertIn("verify-staged-dist", choices)


class ReleasePolicyTests(unittest.TestCase):
    def test_selected_skipped_job_blocks_and_all_success_allows(self) -> None:
        module = load_module()
        selection = {
            "gates": {
                "quality": True,
                "chromium_smoke": True,
                "windows_first_launch": True,
                "browser_compat": False,
                "targeted_mutation": False,
                "windows_extended": False,
            }
        }
        results = {
            "release_build": "success",
            "quality": "success",
            "chromium_smoke": "success",
            "windows_first_launch": "skipped",
            "browser_compat": "skipped",
            "targeted_mutation": "skipped",
            "windows_extended": "skipped",
        }
        denied = module.evaluate_policy(selection, results)
        self.assertFalse(denied["allow_publish"])
        self.assertIn("windows_first_launch:skipped", denied["blocking_results"])
        results["windows_first_launch"] = "success"
        allowed = module.evaluate_policy(selection, results)
        self.assertTrue(allowed["allow_publish"])
        self.assertEqual(allowed["blocking_results"], [])


if __name__ == "__main__":
    unittest.main()

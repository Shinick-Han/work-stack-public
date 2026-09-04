from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
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

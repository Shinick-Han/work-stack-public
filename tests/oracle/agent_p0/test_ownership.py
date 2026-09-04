import tempfile
import unittest
from pathlib import Path

import fixture_support


class PathMatchingTest(unittest.TestCase):
    def setUp(self):
        self.runner = fixture_support.runner_module()

    def test_double_star_spans_segments(self):
        self.assertTrue(
            self.runner.path_matches("quality/agent-p0-oracle/golden/x.jsonl", "quality/agent-p0-oracle/**")
        )
        self.assertTrue(
            self.runner.path_matches("quality/agent-p0-oracle/manifest.v1.json", "quality/agent-p0-oracle/**")
        )
        self.assertFalse(self.runner.path_matches("quality/other/x.json", "quality/agent-p0-oracle/**"))

    def test_single_star_stays_in_segment(self):
        self.assertTrue(self.runner.path_matches("tests/test_agent_x.py", "tests/test_*.py"))
        self.assertFalse(self.runner.path_matches("tests/oracle/agent_p0/test_x.py", "tests/test_*.py"))

    def test_exact_paths(self):
        self.assertTrue(self.runner.path_matches("workstack/agent_transport.py", "workstack/agent_transport.py"))
        self.assertFalse(self.runner.path_matches("workstack/other.py", "workstack/agent_transport.py"))


def make_entry(path, status="A", old_path=None, old_mode="000000", new_mode="100644"):
    return {
        "path": path,
        "old_path": old_path,
        "status": status,
        "score": 0,
        "old_mode": old_mode,
        "new_mode": new_mode,
        "old_oid": "0" * 40,
        "new_oid": "1" * 40,
    }


class SyntheticOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.runner = fixture_support.runner_module()

    def test_owned_addition_passes(self):
        violations = self.runner.check_ownership(
            [make_entry("workstack/agent_authority.py")],
            ["workstack/agent_authority.py"],
            ["workstack/storage/**"],
            ["add"],
        )
        self.assertEqual(violations, [])

    def test_path_outside_allowlist_fails(self):
        violations = self.runner.check_ownership(
            [make_entry("workstack/store.py")], ["workstack/agent_authority.py"], [], ["add"]
        )
        self.assertTrue(any("outside owned_paths" in item for item in violations))

    def test_forbidden_path_fails(self):
        violations = self.runner.check_ownership(
            [make_entry("workstack/storage/lease.py")], ["workstack/**"], ["workstack/storage/**"], ["add"]
        )
        self.assertTrue(any("forbidden_paths" in item for item in violations))

    def test_disallowed_change_type_fails(self):
        violations = self.runner.check_ownership(
            [make_entry("workstack/agent_authority.py", status="D")],
            ["workstack/agent_authority.py"],
            [],
            ["add"],
        )
        self.assertTrue(any("not allowed" in item for item in violations))

    def test_symlink_and_gitlink_modes_fail(self):
        violations = self.runner.check_ownership(
            [make_entry("link/authority", new_mode="120000"), make_entry("sub/module", new_mode="160000")],
            ["**"],
            [],
            ["add"],
        )
        flagged = [item for item in violations if "120000" in item or "160000" in item]
        self.assertEqual(len(flagged), 2)

    def test_case_fold_collision_fails(self):
        violations = self.runner.check_ownership(
            [make_entry("workstack/Agent_Authority.py"), make_entry("workstack/agent_authority.py")],
            ["workstack/**"],
            [],
            ["add"],
        )
        self.assertTrue(any("case-fold" in item for item in violations))

    def test_rename_old_path_must_also_be_owned(self):
        entry = make_entry("workstack/renamed.py", status="R", old_path="outside/old.py")
        violations = self.runner.check_ownership([entry], ["workstack/**"], [], ["rename"])
        self.assertTrue(any("outside/old.py is outside owned_paths" in item for item in violations))

    def test_parent_traversal_path_fails(self):
        violations = self.runner.check_ownership(
            [make_entry("workstack/../escape.py")], ["workstack/**"], [], ["add"]
        )
        self.assertTrue(any("'..'" in item for item in violations))


def _init_repo(work: Path, name: str) -> Path:
    repo = work / name
    repo.mkdir()
    fixture_support.git(repo, "init", "-q", "-b", "main")
    fixture_support._configure_git(repo)
    return repo


class IndexOnlyEntryTest(unittest.TestCase):
    """Raw-diff ownership checks must reject symlink/gitlink modes and case-fold
    collisions even when the entries exist only in the commit tree, never on disk."""

    def test_index_only_modes_and_case_folds_are_rejected(self):
        runner = fixture_support.runner_module()
        with tempfile.TemporaryDirectory(prefix="p0-ownership-") as temporary:
            repo = _init_repo(Path(temporary), "repo")
            fixture_support._write_files(repo, {"workstack/__init__.py": ""})
            fixture_support.git(repo, "add", "-A")
            fixture_support.git(repo, "commit", "-q", "-m", "base")
            base_sha = fixture_support.git(repo, "rev-parse", "HEAD").strip()
            blob = fixture_support.git(repo, "hash-object", "-w", "--stdin").strip()
            fixture_support.git(repo, "update-index", "--add", "--cacheinfo", "120000,%s,link/authority" % blob)
            fixture_support.git(repo, "update-index", "--add", "--cacheinfo", "160000,%s,sub/module" % base_sha)
            fixture_support.git(
                repo, "update-index", "--add", "--cacheinfo", "100644,%s,WorkStack/Agent_Authority.py" % blob
            )
            fixture_support.git(
                repo, "update-index", "--add", "--cacheinfo", "100644,%s,workstack/agent_authority.py" % blob
            )
            fixture_support.git(repo, "commit", "-q", "-m", "candidate")
            candidate_sha = fixture_support.git(repo, "rev-parse", "HEAD").strip()

            entries = runner.parse_diff_raw(runner.git_diff_raw(repo, base_sha, candidate_sha))
            self.assertEqual(len(entries), 4)
            violations = runner.check_ownership(entries, ["**"], [], ["add"])
            joined = "\n".join(violations)
            self.assertIn("120000", joined)
            self.assertIn("160000", joined)
            self.assertIn("case-fold", joined)


def _build_rename_tree(work: Path) -> tuple:
    repo = _init_repo(work, "repo")
    fixture_support._write_files(repo, {"workstack/old_name.py": "VALUE = 1\n"})
    fixture_support.git(repo, "add", "-A")
    fixture_support.git(repo, "commit", "-q", "-m", "base")
    base_sha = fixture_support.git(repo, "rev-parse", "HEAD").strip()
    fixture_support.git(repo, "mv", "workstack/old_name.py", "workstack/new_name.py")
    fixture_support.git(repo, "commit", "-q", "-m", "candidate")
    candidate_sha = fixture_support.git(repo, "rev-parse", "HEAD").strip()
    return repo, base_sha, candidate_sha


class RenameOwnershipTest(unittest.TestCase):
    def test_rename_diff_maps_source_and_destination(self):
        runner = fixture_support.runner_module()
        with tempfile.TemporaryDirectory(prefix="p0-rename-") as temporary:
            repo, base_sha, candidate_sha = _build_rename_tree(Path(temporary))
            entries = runner.parse_diff_raw(runner.git_diff_raw(repo, base_sha, candidate_sha))
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["status"], "R")
            self.assertEqual(entries[0]["path"], "workstack/new_name.py")
            self.assertEqual(entries[0]["old_path"], "workstack/old_name.py")

    def test_rename_with_matching_allowlist_passes(self):
        runner = fixture_support.runner_module()
        with tempfile.TemporaryDirectory(prefix="p0-rename-") as temporary:
            repo, base_sha, candidate_sha = _build_rename_tree(Path(temporary))
            entries = runner.parse_diff_raw(runner.git_diff_raw(repo, base_sha, candidate_sha))
            violations = runner.check_ownership(
                entries, ["workstack/new_name.py", "workstack/old_name.py"], [], ["rename"]
            )
            self.assertEqual(violations, [])

    def test_rename_with_wrong_allowlist_fails(self):
        runner = fixture_support.runner_module()
        with tempfile.TemporaryDirectory(prefix="p0-rename-") as temporary:
            repo, base_sha, candidate_sha = _build_rename_tree(Path(temporary))
            entries = runner.parse_diff_raw(runner.git_diff_raw(repo, base_sha, candidate_sha))
            violations = runner.check_ownership(entries, ["workstack/old_name.py"], [], ["rename"])
            self.assertTrue(any("outside owned_paths" in item for item in violations))


if __name__ == "__main__":
    unittest.main()

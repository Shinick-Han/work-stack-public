"""Focused fixtures for opt-in remote Skill install.

Temp HOME only. Does not touch the operator's installed Skill, live SSOT,
company hosts, or PATH/shell rc. App fixtures come from the existing
verified-unpack admission helper, not a one-field product_version receipt.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_remote_verified_unpack import MODULE as UNPACK
from test_remote_verified_unpack import make_artifact
from test_remote_verified_unpack import payload_blobs


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "python-webview-shell"
CLI_PATH = ROOT / "scripts" / "remote_provision_install.py"
HELPER_PATH = SHELL / "remote_skill_install.py"
GUIDE_PATH = ROOT / "docs" / "WORKSTACK_INSTALL_OPERATION_GUIDE.ko.md"
for entry in (str(ROOT), str(SHELL)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import remote_skill_install as SKILL

CLI_SPEC = importlib.util.spec_from_file_location("remote_provision_install_cli_skill", CLI_PATH)
assert CLI_SPEC is not None and CLI_SPEC.loader is not None
CLI = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(CLI)

PAYLOAD_PATHS = SKILL.SKILL_PAYLOAD_PATHS
DEST_PATHS = SKILL.DEST_RELATIVE_PATHS
V1 = "1.0.13"
V2 = "1.0.14"
OLD_BLOBS = {
    "SKILL.md": b"old-skill\n",
    "references/commands.md": b"old-commands\n",
    "references/journal-policy.md": b"old-journal\n",
}
NEW_BLOBS = {
    "SKILL.md": b"new-skill\n",
    "references/commands.md": b"new-commands\n",
    "references/journal-policy.md": b"new-journal\n",
}


def _write_tree(root: Path, blobs: dict[str, bytes]) -> None:
    for name, data in blobs.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def verified_app_blobs(skill_bytes: dict[str, bytes] | None = None) -> dict[str, bytes]:
    files = dict(payload_blobs())
    chosen = NEW_BLOBS if skill_bytes is None else skill_bytes
    for payload, dest_name in zip(PAYLOAD_PATHS, DEST_PATHS):
        files[payload] = chosen[dest_name]
    files["desktop/python-webview-shell/remote_skill_install.py"] = HELPER_PATH.read_bytes()
    return files


def plant_verified_app(root: Path, skill_bytes: dict[str, bytes] | None = None) -> Path:
    archive, sidecar = make_artifact(verified_app_blobs(skill_bytes))
    placed = UNPACK.place_verified_unpack(
        archive_bytes=archive, sidecar_bytes=sidecar, app_dir=str(root)
    )
    if placed.get("outcome") != "unpacked_verified":
        raise AssertionError(placed)
    return root


def plant_version_only_app(root: Path, blobs: dict[str, bytes], version: str) -> Path:
    for payload, dest_name in zip(PAYLOAD_PATHS, DEST_PATHS):
        path = root / payload
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blobs[dest_name])
    receipt = {"product_version": version, "schema_version": 1}
    (root / ".workstack-unpacked.json").write_text(json.dumps(receipt), encoding="utf-8")
    return root


def dest_blobs(home: Path) -> dict[str, bytes]:
    dest = home / ".agents" / "skills" / "work-stack"
    return {name: (dest / name).read_bytes() for name in DEST_PATHS}


def try_dir_symlink(target: Path, link: Path) -> str:
    try:
        os.symlink(os.fspath(target), os.fspath(link), target_is_directory=True)
    except OSError:
        pass
    else:
        if os.path.islink(os.fspath(link)):
            return ""
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and (os.path.isdir(link) or os.path.lexists(link)):
        return ""
    detail = completed.stderr.strip() or completed.stdout.strip() or str(completed.returncode)
    return detail


def win_to_wsl(path: Path) -> str:
    resolved = str(path.resolve())
    return "/mnt/%s%s" % (resolved[0].lower(), resolved[2:].replace("\\", "/"))


class RemoteSkillInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="workstack-skill-")
        self.base = Path(self.workspace.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.app = plant_verified_app(self.base / "app")
        self.sibling = self.home / ".agents" / "skills" / "other-skill"
        self.sibling.mkdir(parents=True)
        self.sibling_marker = self.sibling / "SKILL.md"
        self.sibling_marker.write_bytes(b"sibling-untouched\n")

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def assert_siblings_untouched(self) -> None:
        self.assertEqual(self.sibling_marker.read_bytes(), b"sibling-untouched\n")

    def test_inspect_without_apply_does_not_create_dest(self) -> None:
        planned = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=False
        )
        self.assertEqual(planned["action"], "install")
        self.assertEqual(planned["outcome"], "planned")
        self.assertFalse((self.home / ".agents" / "skills" / "work-stack").exists())
        self.assert_siblings_untouched()

    def test_absent_install_then_noop(self) -> None:
        installed = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=True
        )
        self.assertEqual(installed["outcome"], "installed")
        self.assertEqual(dest_blobs(self.home), NEW_BLOBS)
        receipt = json.loads(
            (self.home / ".agents" / "skills" / "work-stack" / SKILL.RECEIPT_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["skill"], "work-stack")
        self.assertEqual(receipt["product_version"], V2)
        self.assertNotIn("owner", receipt)
        self.assertNotIn("workspace_uid", receipt)
        replay = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=True
        )
        self.assertEqual(replay["action"], "noop")
        self.assertEqual(replay["outcome"], "noop")
        self.assertEqual(dest_blobs(self.home), NEW_BLOBS)
        self.assert_siblings_untouched()

    def test_version_only_receipt_and_source_tamper_refuse_before_home_write(self) -> None:
        fake = plant_version_only_app(self.base / "fake-app", NEW_BLOBS, V2)
        refused = SKILL.run_skill_install(
            install_root=str(fake), home=str(self.home), apply=True
        )
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["code"], "APP_NOT_VERIFIED")
        self.assertFalse((self.home / ".agents" / "skills" / "work-stack").exists())
        skill_path = self.app / "integrations" / "agent-skill" / "work-stack" / "SKILL.md"
        skill_path.write_bytes(b"tampered-source\n")
        tampered = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=True
        )
        self.assertEqual(tampered["action"], "refuse")
        self.assertEqual(tampered["code"], "APP_NOT_VERIFIED")
        self.assertFalse((self.home / ".agents" / "skills" / "work-stack").exists())
        self.assert_siblings_untouched()

    def test_cli_entry_point_absent_install_noop(self) -> None:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False):
            inspect = CLI.run(["--install-skill", "--install-root", str(self.app)])
            self.assertEqual(inspect, 0)
            self.assertFalse((self.home / ".agents" / "skills" / "work-stack").exists())
            first = CLI.run(
                ["--install-skill", "--install-root", str(self.app), "--apply"]
            )
            second = CLI.run(
                ["--install-skill", "--install-root", str(self.app), "--apply"]
            )
        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertEqual(dest_blobs(self.home), NEW_BLOBS)
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(CLI_PATH),
                "--install-skill",
                "--install-root",
                str(self.app),
                "--apply",
            ],
            capture_output=True,
            env=env,
            cwd=str(ROOT),
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0)
        document = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(document["outcome"], "noop")
        self.assertEqual(dest_blobs(self.home), NEW_BLOBS)
        self.assert_siblings_untouched()

    def test_payload_helper_runs_without_source_repo_on_path(self) -> None:
        helper = self.app / "desktop" / "python-webview-shell" / "remote_skill_install.py"
        self.assertTrue(helper.is_file())
        isolated = self.base / "cwd-not-repo"
        isolated.mkdir()
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["PYTHONPATH"] = ""
        env["PYTHONNOUSERSITE"] = "1"
        inspect = subprocess.run(
            [sys.executable, "-I", "-B", str(helper), "--install-root", str(self.app)],
            capture_output=True,
            env=env,
            cwd=str(isolated),
            timeout=30,
        )
        self.assertEqual(inspect.returncode, 0, inspect.stderr.decode("utf-8", "replace"))
        planned = json.loads(inspect.stdout.decode("utf-8"))
        self.assertEqual(planned["action"], "install")
        self.assertFalse((self.home / ".agents" / "skills" / "work-stack").exists())
        applied = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(helper),
                "--install-root",
                str(self.app),
                "--apply",
            ],
            capture_output=True,
            env=env,
            cwd=str(isolated),
            timeout=30,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr.decode("utf-8", "replace"))
        document = json.loads(applied.stdout.decode("utf-8"))
        self.assertEqual(document["outcome"], "installed")
        self.assertEqual(dest_blobs(self.home), NEW_BLOBS)
        self.assert_siblings_untouched()

    def test_foreign_and_modified_dest_are_refused(self) -> None:
        dest = self.home / ".agents" / "skills" / "work-stack"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_bytes(b"user-edited\n")
        (dest / "notes.md").write_bytes(b"foreign\n")
        refused = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=True
        )
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["outcome"], "refused")
        self.assertEqual((dest / "SKILL.md").read_bytes(), b"user-edited\n")
        self.assertEqual((dest / "notes.md").read_bytes(), b"foreign\n")
        self.assertFalse((dest / "references" / "commands.md").exists())
        self.assert_siblings_untouched()
        (dest / "notes.md").unlink()
        _write_tree(dest, NEW_BLOBS)
        (dest / "SKILL.md").write_bytes(b"user-edited\n")
        modified = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=True
        )
        self.assertEqual(modified["action"], "refuse")
        self.assertEqual((dest / "SKILL.md").read_bytes(), b"user-edited\n")
        self.assert_siblings_untouched()

    def test_owned_receipt_allows_update_only_when_files_match_receipt(self) -> None:
        dest = self.home / ".agents" / "skills" / "work-stack"
        _write_tree(dest, OLD_BLOBS)
        receipt = SKILL._build_receipt(V1, OLD_BLOBS)
        (dest / SKILL.RECEIPT_NAME).write_bytes(SKILL.canonical_json(receipt) + b"\n")
        updated = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=True
        )
        self.assertEqual(updated["outcome"], "updated")
        self.assertEqual(dest_blobs(self.home), NEW_BLOBS)
        (dest / "SKILL.md").write_bytes(b"user-edited-after-own\n")
        refused = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=True
        )
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual((dest / "SKILL.md").read_bytes(), b"user-edited-after-own\n")
        self.assert_siblings_untouched()

    def test_destination_symlink_dir_refuses_and_leaves_sibling_bytes(self) -> None:
        dest = self.home / ".agents" / "skills" / "work-stack"
        dest.mkdir(parents=True)
        sibling_refs = self.sibling / "references"
        sibling_refs.mkdir()
        marker = sibling_refs / "journal-policy.md"
        marker.write_bytes(b"sibling-refs\n")
        link = dest / "references"
        reason = try_dir_symlink(sibling_refs, link)
        if reason:
            self._run_or_skip_wsl_symlink_dir()
            return
        refused = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=True
        )
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["code"], "SKILL_DEST_FOREIGN")
        self.assertEqual(marker.read_bytes(), b"sibling-refs\n")
        self.assertFalse((sibling_refs / "commands.md").exists())
        self.assertFalse((dest / "SKILL.md").exists())
        self.assert_siblings_untouched()

    def _run_or_skip_wsl_symlink_dir(self) -> None:
        distro = os.environ.get("WORKSTACK_TEST_WSL_DISTRO")
        if not distro or not sys.platform.startswith("win"):
            self.skipTest(
                "directory symlink unavailable on this host; "
                "WORKSTACK_TEST_WSL_DISTRO not set"
            )
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        wsl = str(Path(root) / "System32" / "wsl.exe")
        script = self.base / "wsl-skill-symlink.py"
        script.write_text(
            "\n".join(
                [
                    "import json, os, sys, tempfile",
                    "from pathlib import Path",
                    "sys.path.insert(0, sys.argv[1])",
                    "import remote_skill_install as SKILL",
                    "home = Path(tempfile.mkdtemp(prefix='skill-wsl-'))",
                    "app = Path(sys.argv[2])",
                    "dest = home / '.agents' / 'skills' / 'work-stack'",
                    "sibling = home / '.agents' / 'skills' / 'other-skill' / 'references'",
                    "dest.mkdir(parents=True)",
                    "sibling.mkdir(parents=True)",
                    "marker = sibling / 'journal-policy.md'",
                    "marker.write_bytes(b'sibling-refs\\n')",
                    "os.symlink(str(sibling), str(dest / 'references'), target_is_directory=True)",
                    "result = SKILL.run_skill_install(install_root=str(app), home=str(home), apply=True)",
                    "ok = result.get('action') == 'refuse' and marker.read_bytes() == b'sibling-refs\\n'",
                    "print(json.dumps({'ok': ok, 'result': result}))",
                    "sys.exit(0 if ok else 2)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                wsl,
                "-d",
                distro,
                "-e",
                "python3",
                win_to_wsl(script),
                win_to_wsl(SHELL),
                win_to_wsl(self.app),
            ],
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        document = json.loads(completed.stdout.decode("utf-8").strip().splitlines()[-1])
        self.assertTrue(document["ok"], document)

    def test_late_edit_deletion_and_source_dest_links_refuse(self) -> None:
        dest = self.home / ".agents" / "skills" / "work-stack"
        _write_tree(dest, OLD_BLOBS)
        receipt = SKILL._build_receipt(V1, OLD_BLOBS)
        (dest / SKILL.RECEIPT_NAME).write_bytes(SKILL.canonical_json(receipt) + b"\n")
        real_read = SKILL._read_dest_blobs
        calls = {"n": 0}

        def edit_after_inspect(path: Path) -> dict[str, bytes]:
            blobs = real_read(path)
            calls["n"] += 1
            if calls["n"] == 1:
                (path / "SKILL.md").write_bytes(b"late-edit\n")
            return blobs

        with self.subTest("late-edit"):
            with mock.patch.object(SKILL, "_read_dest_blobs", side_effect=edit_after_inspect):
                edited = SKILL.apply_skill_install(install_root=str(self.app), home=str(self.home))
            self.assertEqual(edited["action"], "refuse")
            self.assertEqual(edited["code"], "SKILL_DEST_MODIFIED")
            self.assertEqual((dest / "SKILL.md").read_bytes(), b"late-edit\n")
            self.assertEqual((dest / "references" / "commands.md").read_bytes(), b"old-commands\n")

        _write_tree(dest, OLD_BLOBS)
        (dest / SKILL.RECEIPT_NAME).write_bytes(SKILL.canonical_json(receipt) + b"\n")
        calls["n"] = 0

        def delete_after_inspect(path: Path) -> dict[str, bytes]:
            blobs = real_read(path)
            calls["n"] += 1
            if calls["n"] == 1:
                (path / "SKILL.md").unlink()
            return blobs

        with self.subTest("late-deletion"):
            with mock.patch.object(SKILL, "_read_dest_blobs", side_effect=delete_after_inspect):
                deleted = SKILL.apply_skill_install(install_root=str(self.app), home=str(self.home))
            self.assertEqual(deleted["action"], "refuse")
            self.assertNotEqual(deleted.get("code"), "")
            self.assertFalse((dest / "SKILL.md").exists())
            self.assertEqual((dest / "references" / "commands.md").read_bytes(), b"old-commands\n")

        with self.subTest("source-symlink"):
            source = self.app / "integrations" / "agent-skill" / "work-stack" / "SKILL.md"
            other = self.base / "other-skill.md"
            other.write_bytes(b"linked-source\n")
            original = source.read_bytes()
            source.unlink()
            try:
                os.symlink(os.fspath(other), os.fspath(source))
            except OSError as error:
                source.write_bytes(original)
                self.skipTest("file symlink unavailable: %s" % error)
            linked = SKILL.run_skill_install(
                install_root=str(self.app), home=str(self.home), apply=True
            )
            self.assertEqual(linked["action"], "refuse")
            self.assertEqual((dest / "references" / "commands.md").read_bytes(), b"old-commands\n")
        self.assert_siblings_untouched()

    def plant_owned_dest(self) -> Path:
        """Plant an owned V1 destination whose receipt matches its bytes."""

        dest = self.home / ".agents" / "skills" / "work-stack"
        _write_tree(dest, OLD_BLOBS)
        receipt = SKILL._build_receipt(V1, OLD_BLOBS)
        (dest / SKILL.RECEIPT_NAME).write_bytes(SKILL.canonical_json(receipt) + b"\n")
        return dest

    def assert_receipt_still_old(self, dest: Path) -> None:
        parsed = SKILL._parse_skill_receipt((dest / SKILL.RECEIPT_NAME).read_bytes())
        self.assertEqual(parsed["product_version"], V1)
        self.assertTrue(SKILL._receipt_matches_blobs(parsed, OLD_BLOBS))

    def test_edit_after_second_read_refuses_and_keeps_user_bytes(self) -> None:
        dest = self.plant_owned_dest()
        real_state = SKILL._current_owned_state

        def edit_after_second_read(path: Path) -> SKILL.OwnedState:
            state = real_state(path)
            (path / "SKILL.md").write_bytes(b"late-edit-after-second-read\n")
            return state

        with mock.patch.object(SKILL, "_current_owned_state", side_effect=edit_after_second_read):
            refused = SKILL.apply_skill_install(install_root=str(self.app), home=str(self.home))
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["outcome"], "refused")
        self.assertEqual(refused["code"], "SKILL_DEST_MODIFIED")
        self.assertEqual((dest / "SKILL.md").read_bytes(), b"late-edit-after-second-read\n")
        self.assertEqual((dest / "references" / "commands.md").read_bytes(), OLD_BLOBS["references/commands.md"])
        self.assertEqual(
            (dest / "references" / "journal-policy.md").read_bytes(),
            OLD_BLOBS["references/journal-policy.md"],
        )
        self.assert_receipt_still_old(dest)
        self.assert_siblings_untouched()

    def test_deletion_after_second_read_is_not_recreated(self) -> None:
        dest = self.plant_owned_dest()
        real_state = SKILL._current_owned_state

        def delete_after_second_read(path: Path) -> SKILL.OwnedState:
            state = real_state(path)
            (path / "SKILL.md").unlink()
            return state

        with mock.patch.object(SKILL, "_current_owned_state", side_effect=delete_after_second_read):
            refused = SKILL.apply_skill_install(install_root=str(self.app), home=str(self.home))
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["outcome"], "refused")
        self.assertEqual(refused["code"], "SKILL_DEST_MODIFIED")
        self.assertFalse((dest / "SKILL.md").exists())
        self.assertEqual((dest / "references" / "commands.md").read_bytes(), OLD_BLOBS["references/commands.md"])
        self.assert_receipt_still_old(dest)
        self.assert_siblings_untouched()

    def test_edit_during_copy_previous_refuses_before_any_destination_write(self) -> None:
        dest = self.plant_owned_dest()
        previous = dest.parent / SKILL.PREVIOUS_NAME
        real_write = SKILL._exclusive_write

        def edit_mid_copy(path: Path, content: bytes) -> None:
            real_write(path, content)
            if path == previous / "SKILL.md":
                (dest / "references" / "commands.md").write_bytes(b"edit-mid-copy\n")

        with mock.patch.object(SKILL, "_exclusive_write", side_effect=edit_mid_copy):
            refused = SKILL.apply_skill_install(install_root=str(self.app), home=str(self.home))
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["code"], "SKILL_DEST_MODIFIED")
        self.assertEqual((dest / "references" / "commands.md").read_bytes(), b"edit-mid-copy\n")
        self.assertEqual((dest / "SKILL.md").read_bytes(), OLD_BLOBS["SKILL.md"])
        self.assertEqual((previous / "SKILL.md").read_bytes(), OLD_BLOBS["SKILL.md"])
        self.assert_receipt_still_old(dest)
        self.assert_siblings_untouched()

    def test_change_before_its_own_write_refuses_and_claims_no_success(self) -> None:
        dest = self.plant_owned_dest()
        previous = dest.parent / SKILL.PREVIOUS_NAME
        stage = dest.parent / SKILL.STAGE_NAME
        real_copy = SKILL._copy_previous

        def edit_after_copy(dest_path: Path, previous_path: Path, expected: SKILL.OwnedState) -> None:
            real_copy(dest_path, previous_path, expected)
            (dest_path / "references" / "journal-policy.md").write_bytes(b"edit-before-its-write\n")

        with mock.patch.object(SKILL, "_copy_previous", side_effect=edit_after_copy):
            refused = SKILL.apply_skill_install(install_root=str(self.app), home=str(self.home))
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["outcome"], "refused")
        self.assertEqual(refused["code"], "SKILL_DEST_MODIFIED")
        self.assertEqual((dest / "references" / "journal-policy.md").read_bytes(), b"edit-before-its-write\n")
        self.assertEqual((dest / "SKILL.md").read_bytes(), NEW_BLOBS["SKILL.md"])
        self.assertEqual((dest / "references" / "commands.md").read_bytes(), NEW_BLOBS["references/commands.md"])
        self.assert_receipt_still_old(dest)
        for name in SKILL.OWNED_ORDER:
            self.assertTrue((previous / name).exists())
            self.assertTrue((stage / name).exists())
        self.assertEqual((previous / "references" / "journal-policy.md").read_bytes(), OLD_BLOBS["references/journal-policy.md"])
        follow_up = SKILL.run_skill_install(
            install_root=str(self.app), home=str(self.home), apply=False
        )
        self.assertEqual(follow_up["action"], "refuse")
        self.assertEqual(follow_up["code"], "SKILL_DEST_MODIFIED")
        self.assert_siblings_untouched()

    def test_deletion_before_its_own_write_is_not_recreated(self) -> None:
        dest = self.plant_owned_dest()
        previous = dest.parent / SKILL.PREVIOUS_NAME
        real_copy = SKILL._copy_previous

        def delete_after_copy(dest_path: Path, previous_path: Path, expected: SKILL.OwnedState) -> None:
            real_copy(dest_path, previous_path, expected)
            (dest_path / "references" / "journal-policy.md").unlink()

        with mock.patch.object(SKILL, "_copy_previous", side_effect=delete_after_copy):
            refused = SKILL.apply_skill_install(install_root=str(self.app), home=str(self.home))
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["code"], "SKILL_DEST_MODIFIED")
        self.assertFalse((dest / "references" / "journal-policy.md").exists())
        self.assert_receipt_still_old(dest)
        self.assertEqual(
            (previous / "references" / "journal-policy.md").read_bytes(),
            OLD_BLOBS["references/journal-policy.md"],
        )
        self.assert_siblings_untouched()

    def test_receipt_swapped_before_its_write_refuses_and_keeps_user_receipt(self) -> None:
        dest = self.plant_owned_dest()
        real_replace = SKILL._replace_owned

        def edit_receipt_before_its_write(
            dest_path: Path, stage: Path, names: tuple[str, ...], expected: SKILL.OwnedState
        ) -> None:
            real_replace(dest_path, stage, names, expected)
            if names == SKILL.DEST_RELATIVE_PATHS:
                (dest_path / SKILL.RECEIPT_NAME).write_bytes(b"{}\n")

        with mock.patch.object(SKILL, "_replace_owned", side_effect=edit_receipt_before_its_write):
            refused = SKILL.apply_skill_install(install_root=str(self.app), home=str(self.home))
        self.assertEqual(refused["action"], "refuse")
        self.assertEqual(refused["outcome"], "refused")
        self.assertEqual(refused["code"], "SKILL_DEST_MODIFIED")
        self.assertEqual((dest / SKILL.RECEIPT_NAME).read_bytes(), b"{}\n")
        self.assert_siblings_untouched()

    def test_ssh_parser_does_not_gain_skill_flag(self) -> None:
        options = {
            option for action in CLI.build_parser()._actions for option in action.option_strings
        }
        self.assertNotIn("--install-skill", options)

    def test_guide_documents_exact_opt_in_invocation(self) -> None:
        guide = GUIDE_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "python -I scripts/remote_provision_install.py --install-skill --install-root <verified-app>",
            guide,
        )
        self.assertIn(
            "python -I scripts/remote_provision_install.py --install-skill --install-root <verified-app> --apply",
            guide,
        )
        self.assertIn(
            "python -I <verified-app>/desktop/python-webview-shell/remote_skill_install.py --install-root <verified-app>",
            guide,
        )
        self.assertIn(
            "python -I <verified-app>/desktop/python-webview-shell/remote_skill_install.py --install-root <verified-app> --apply",
            guide,
        )
        self.assertIn("Windows GUI는 이 명령을 대신 실행하지 않습니다", guide)

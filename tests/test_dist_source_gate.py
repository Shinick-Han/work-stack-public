"""Contract tests for the frontend dist/source binding gate.

Synthetic repositories and a deterministic stub build only. No npm, no network
and no live .artifacts tree. Freshness is judged by content digests, so no test
here may pass by touching an mtime.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "dist_source_gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("dist_source_gate", GATE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("dist source gate module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = load_gate()

# The stub stands in for `npm run build`: it is deterministic, derives every
# output byte from the source it reads, and honours --outDir exactly as vite
# does. Its working directory is the *captured* frontend, so `repo` below is the
# private scratch input tree and never the live checkout.
STUB_BUILD = """
import hashlib
import pathlib
import sys

arguments = sys.argv[1:]
out = pathlib.Path(arguments[arguments.index("--outDir") + 1])
repo = pathlib.Path.cwd().parent
source = (repo / "frontend" / "src" / "main.tsx").read_bytes()
lock = (repo / "frontend" / "package-lock.json").read_bytes()
digest = hashlib.sha256(source + lock).hexdigest()
out.mkdir(parents=True, exist_ok=True)
(out / "assets").mkdir(parents=True, exist_ok=True)
(out / "index.html").write_bytes(("<html>" + digest + "</html>\\n").encode("utf-8"))
(out / "assets" / ("app-" + digest[:8] + ".js")).write_bytes(("export const build = 1\\n").encode("utf-8"))
"""

# Leaves the LIVE checkout changed: the capture no longer describes the tree
# that would be packaged, so the freshly built dist must not be installed.
STUB_MUTATING = STUB_BUILD + """
handle = pathlib.Path({live!r}) / "frontend" / "src" / "main.tsx"
handle.write_bytes(handle.read_bytes() + b"// raced\\n")
"""

# Change / read / restore of the LIVE checkout while the build runs. The build
# reads only its captured copy, so the transient bytes can reach neither the
# emitted output nor the receipt.
STUB_RACING_LIVE = """
import hashlib
import pathlib
import sys

live = pathlib.Path({live!r}) / "frontend" / "src" / "main.tsx"
original = live.read_bytes()
live.write_bytes(b"export const main = 999\\n")
try:
    arguments = sys.argv[1:]
    out = pathlib.Path(arguments[arguments.index("--outDir") + 1])
    repo = pathlib.Path.cwd().parent
    assert repo.resolve() != pathlib.Path({live!r}).resolve(), "the build ran in the live checkout"
    source = (repo / "frontend" / "src" / "main.tsx").read_bytes()
    lock = (repo / "frontend" / "package-lock.json").read_bytes()
    digest = hashlib.sha256(source + lock).hexdigest()
    out.mkdir(parents=True, exist_ok=True)
    (out / "assets").mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_bytes(("<html>" + digest + "</html>\\n").encode("utf-8"))
    (out / "assets" / ("app-" + digest[:8] + ".js")).write_bytes(b"export const build = 1\\n")
finally:
    live.write_bytes(original)
"""

# Records the directory the build actually compiled from, so a test can assert
# it is the private capture and not the live checkout.
STUB_RECORDING_CAPTURE = STUB_BUILD + """
(out / "captured-repo.txt").write_text(str(repo), encoding="utf-8")
"""

STUB_FAILING = """
import sys

sys.stderr.write("stub build refused\\n")
raise SystemExit(3)
"""


def write_repo(root: Path) -> None:
    """Create the smallest tree the gate's input roster admits."""

    (root / "frontend" / "src").mkdir(parents=True)
    (root / "frontend" / "src" / "main.tsx").write_text("export const main = 1\n", encoding="utf-8")
    (root / "frontend" / "src" / "generated").mkdir()
    (root / "frontend" / "src" / "generated" / "theme-tokens.css").write_text(
        ":root{--a:#000}\n", encoding="utf-8"
    )
    (root / "frontend" / "index.html").write_text("<div id=root></div>\n", encoding="utf-8")
    (root / "frontend" / "package.json").write_text('{"name":"ui"}\n', encoding="utf-8")
    (root / "frontend" / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (root / "frontend" / "vite.config.ts").write_text("export default {}\n", encoding="utf-8")
    for name in ("tsconfig.json", "tsconfig.app.json", "tsconfig.node.json"):
        (root / "frontend" / name).write_text("{}\n", encoding="utf-8")
    # Installed dependencies are a precondition of the build, never something
    # the gate creates: the roster skips them and only the lock digest binds them.
    (root / "frontend" / "node_modules" / ".bin").mkdir(parents=True)
    (root / "frontend" / "node_modules" / "marker.txt").write_text("installed\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "generate-theme-tokens.mjs").write_text("export const tokens = 1\n", encoding="utf-8")
    (root / "theme").mkdir()
    (root / "theme" / "theme-tokens.json").write_text('{"color":{}}\n', encoding="utf-8")
    generated = root / "desktop" / "python-webview-shell" / "generated"
    generated.mkdir(parents=True)
    (generated / "theme_tokens.py").write_text("TOKENS = {}\n", encoding="utf-8")
    fixtures = root / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "checkpoint_change_v1.json").write_text('{"event_id":1}\n', encoding="utf-8")


def write_stale_dist(root: Path) -> dict[str, str]:
    dist = root / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>stale</html>\n", encoding="utf-8")
    (dist / "assets" / "app-00000000.js").write_text("export const build = 0\n", encoding="utf-8")
    return tree_hashes(dist)


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): GATE._sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def make_link(target: Path, link: Path) -> bool:
    """Create a directory junction/symlink, or report that this host refuses."""

    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, AttributeError, ImportError, NotImplementedError):
        return False
    return True


class DistSourceGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.mkdtemp(prefix="workstack-dist-gate-")
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.repo = Path(self.temporary) / "repo"
        self.repo.mkdir()
        write_repo(self.repo)
        self.stub = Path(self.temporary) / "stub_build.py"
        self.stub.write_text(STUB_BUILD, encoding="utf-8")

    def build_command(self, script: str | None = None) -> tuple[str, ...]:
        if script is None:
            return (sys.executable, str(self.stub))
        path = Path(self.temporary) / "stub_variant.py"
        path.write_text(script, encoding="utf-8")
        return (sys.executable, str(path))

    def refresh(self, **kwargs) -> dict:
        return GATE.refresh(self.repo, build_command=self.build_command(), **kwargs)

    def refuse(self, call, code: str) -> GATE.DistSourceGateError:
        with self.assertRaises(GATE.DistSourceGateError) as raised:
            call()
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    # --- the accepted case -------------------------------------------------

    def test_refresh_records_a_receipt_that_admits_the_tree_it_built(self) -> None:
        result = self.refresh()

        self.assertTrue(result["replaced_dist"])
        self.assertEqual(2, result["dist_file_count"])
        receipt = json.loads(GATE.receipt_path(self.repo).read_text(encoding="utf-8"))
        self.assertEqual(GATE.SCHEMA_VERSION, receipt["schema_version"])
        self.assertEqual(result["source_digest"], receipt["source_digest"])
        self.assertEqual(
            sorted(tree_hashes(self.repo / "frontend" / "dist")),
            sorted(entry["path"] for entry in receipt["dist"]["files"]),
        )
        self.assertEqual(
            {entry["path"] for entry in receipt["source_inputs"]},
            {entry["path"] for entry in GATE.source_entries(self.repo)},
        )
        self.assertEqual(GATE.verify(self.repo)["source_digest"], result["source_digest"])

    def test_unchanged_source_keeps_being_accepted_without_rebuilding_bytes(self) -> None:
        self.refresh()
        before = tree_hashes(self.repo / "frontend" / "dist")

        second = self.refresh()

        self.assertFalse(second["replaced_dist"])
        self.assertEqual(before, tree_hashes(self.repo / "frontend" / "dist"))
        self.assertEqual(second["source_digest"], GATE.verify(self.repo)["source_digest"])

    # --- the captured build ------------------------------------------------

    def test_the_build_runs_out_of_the_captured_tree_not_the_live_checkout(self) -> None:
        result = GATE.refresh(
            self.repo, build_command=self.build_command(STUB_RECORDING_CAPTURE)
        )

        captured = (self.repo / "frontend" / "dist" / "captured-repo.txt").read_text(encoding="utf-8")
        self.assertNotEqual(str(self.repo.resolve()), captured)
        self.assertTrue(Path(captured).name == "input")
        self.assertIsNotNone(result["source_digest"])

    def test_change_read_restore_of_the_live_checkout_cannot_contaminate_the_build(self) -> None:
        expected_source = (self.repo / "frontend" / "src" / "main.tsx").read_bytes()

        result = GATE.refresh(
            self.repo,
            build_command=self.build_command(STUB_RACING_LIVE.format(live=str(self.repo))),
        )

        # The live file was changed and restored while the build ran. The output
        # must be the one derived from the captured (original) bytes, and the
        # receipt must still admit the restored checkout.
        self.assertEqual(expected_source, (self.repo / "frontend" / "src" / "main.tsx").read_bytes())
        honest = self.refresh()
        self.assertFalse(honest["replaced_dist"])
        self.assertEqual(result["source_digest"], honest["source_digest"])
        self.assertIsNotNone(GATE.verify(self.repo))

    def test_a_live_change_that_outlives_the_build_refuses_installation(self) -> None:
        stale = write_stale_dist(self.repo)

        self.refuse(
            lambda: GATE.refresh(
                self.repo,
                build_command=self.build_command(STUB_MUTATING.format(live=str(self.repo))),
            ),
            "DIST_SOURCE_MOVED",
        )

        self.assertEqual(stale, tree_hashes(self.repo / "frontend" / "dist"))
        self.assertFalse(GATE.receipt_path(self.repo).exists())

    def test_the_captured_digest_is_taken_from_the_bytes_that_were_captured(self) -> None:
        destination = Path(self.temporary) / "capture"
        destination.mkdir()

        entries = GATE.capture_inputs(self.repo, destination)

        paths = {entry["path"]: entry["sha256"] for entry in entries}
        self.assertEqual(GATE.source_digest(entries), GATE.source_digest(GATE.source_entries(self.repo)))
        self.assertEqual(
            paths["frontend/src/main.tsx"],
            GATE._sha256(destination / "frontend" / "src" / "main.tsx"),
        )
        self.assertIn("tests/fixtures/checkpoint_change_v1.json", paths)

    def test_the_live_dependency_tree_survives_the_scratch_cleanup(self) -> None:
        self.refresh()

        marker = self.repo / "frontend" / "node_modules" / "marker.txt"
        self.assertTrue(marker.is_file())
        self.assertEqual("installed\n", marker.read_text(encoding="utf-8"))

    def test_uninstalled_dependencies_refuse_instead_of_building(self) -> None:
        shutil.rmtree(self.repo / "frontend" / "node_modules")

        failure = self.refuse(lambda: self.refresh(), "DIST_DEPENDENCIES_MISSING")

        self.assertIn("never installs", failure.detail)
        self.assertFalse(GATE.receipt_path(self.repo).exists())

    # --- the input roster --------------------------------------------------

    def test_the_roster_covers_every_documented_build_input(self) -> None:
        paths = {entry["path"] for entry in GATE.source_entries(self.repo)}

        for required in (
            "frontend/src/main.tsx",
            "frontend/src/generated/theme-tokens.css",
            "frontend/index.html",
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/vite.config.ts",
            "frontend/tsconfig.json",
            "frontend/tsconfig.app.json",
            "frontend/tsconfig.node.json",
            "scripts/generate-theme-tokens.mjs",
            "theme/theme-tokens.json",
            "desktop/python-webview-shell/generated/theme_tokens.py",
            "tests/fixtures/checkpoint_change_v1.json",
            "frontend/.env",
            "frontend/.env.local",
            "frontend/.env.production",
            "frontend/.env.production.local",
        ):
            with self.subTest(path=required):
                self.assertIn(required, paths)

    def test_each_newly_covered_input_alone_invalidates_the_receipt(self) -> None:
        self.refresh()

        for relative in (
            "desktop/python-webview-shell/generated/theme_tokens.py",
            "tests/fixtures/checkpoint_change_v1.json",
            "frontend/src/generated/theme-tokens.css",
        ):
            with self.subTest(path=relative):
                path = self.repo.joinpath(*relative.split("/"))
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                try:
                    self.refuse(lambda: GATE.verify(self.repo), "DIST_SOURCE_DRIFT")
                finally:
                    path.write_bytes(original)
        self.assertIsNotNone(GATE.verify(self.repo))

    def test_an_added_env_file_is_never_silently_admitted(self) -> None:
        self.refresh()

        (self.repo / "frontend" / ".env.production").write_text("", encoding="utf-8")

        # Present-but-empty is bound, so it is a digest change rather than a
        # silently ignored input.
        self.refuse(lambda: GATE.verify(self.repo), "DIST_SOURCE_DRIFT")

    def test_a_non_empty_env_file_refuses_the_release_build(self) -> None:
        self.refresh()

        (self.repo / "frontend" / ".env").write_text("VITE_API=https://example.invalid\n", encoding="utf-8")

        failure = self.refuse(lambda: GATE.verify(self.repo), "DIST_ENV_FILE_UNSUPPORTED")
        self.assertIn("frontend/.env", failure.detail)
        self.assertNotIn("example.invalid", failure.detail)
        self.refuse(lambda: self.refresh(), "DIST_ENV_FILE_UNSUPPORTED")

    def test_an_ambient_build_override_refuses_without_leaking_its_value(self) -> None:
        with mock.patch.dict(os.environ, {"VITE_SECRET_TOKEN": "s3cret-value"}):
            failure = self.refuse(lambda: self.refresh(), "DIST_BUILD_ENV_OVERRIDE")

        self.assertIn("VITE_SECRET_TOKEN", failure.detail)
        self.assertNotIn("s3cret-value", failure.detail)
        self.assertFalse(GATE.receipt_path(self.repo).exists())

    def test_the_build_child_never_inherits_an_ambient_vite_variable(self) -> None:
        with mock.patch.dict(os.environ, {"VITE_SECRET_TOKEN": "s3cret-value", "NODE_ENV": "test"}):
            child = GATE._child_environment()

        self.assertNotIn("VITE_SECRET_TOKEN", child)
        self.assertNotIn("NODE_ENV", child)
        self.assertIn("PATH", {name.upper() for name in child})

    # --- the refusals ------------------------------------------------------

    def test_missing_receipt_refuses_with_the_repair_command(self) -> None:
        write_stale_dist(self.repo)

        failure = self.refuse(lambda: GATE.verify(self.repo), "DIST_RECEIPT_MISSING")

        self.assertIn("refresh-dist", failure.detail)
        self.assertIn("--repo", failure.detail)

    def test_changed_frontend_source_invalidates_the_receipt(self) -> None:
        self.refresh()

        (self.repo / "frontend" / "src" / "main.tsx").write_text(
            "export const main = 2\n", encoding="utf-8"
        )

        failure = self.refuse(lambda: GATE.verify(self.repo), "DIST_SOURCE_DRIFT")
        self.assertIn("frontend/src/main.tsx", failure.detail)
        self.assertIn("refresh-dist", failure.detail)

    def test_a_new_source_file_alone_invalidates_the_receipt(self) -> None:
        self.refresh()

        (self.repo / "frontend" / "src" / "extra.tsx").write_text("export const x = 1\n", encoding="utf-8")

        self.refuse(lambda: GATE.verify(self.repo), "DIST_SOURCE_DRIFT")

    def test_changed_build_configuration_invalidates_the_receipt(self) -> None:
        self.refresh()

        for name in ("package-lock.json", "vite.config.ts", "tsconfig.app.json"):
            with self.subTest(name=name):
                path = self.repo / "frontend" / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                try:
                    self.refuse(lambda: GATE.verify(self.repo), "DIST_SOURCE_DRIFT")
                finally:
                    path.write_bytes(original)
        self.assertIsNotNone(GATE.verify(self.repo))

    def test_changed_theme_input_invalidates_the_receipt(self) -> None:
        self.refresh()

        (self.repo / "theme" / "theme-tokens.json").write_text('{"color":{"a":1}}\n', encoding="utf-8")

        self.refuse(lambda: GATE.verify(self.repo), "DIST_SOURCE_DRIFT")

    def test_hand_edited_dist_is_rejected_even_when_the_source_is_unchanged(self) -> None:
        self.refresh()

        (self.repo / "frontend" / "dist" / "index.html").write_text("<html>tampered</html>\n", encoding="utf-8")

        self.refuse(lambda: GATE.verify(self.repo), "DIST_CONTENT_DRIFT")

    def test_extra_dist_file_is_rejected(self) -> None:
        self.refresh()

        (self.repo / "frontend" / "dist" / "stowaway.js").write_text("//\n", encoding="utf-8")

        self.refuse(lambda: GATE.verify(self.repo), "DIST_CONTENT_DRIFT")

    def test_absent_dist_is_refused_rather_than_assumed(self) -> None:
        self.refresh()
        shutil.rmtree(self.repo / "frontend" / "dist")

        self.refuse(lambda: GATE.verify(self.repo), "DIST_MISSING")

    def test_missing_build_input_is_refused(self) -> None:
        self.refresh()
        (self.repo / "frontend" / "vite.config.ts").unlink()

        self.refuse(lambda: GATE.verify(self.repo), "DIST_SOURCE_MISSING")
        self.refuse(lambda: self.refresh(), "DIST_SOURCE_MISSING")

    # --- containment -------------------------------------------------------

    def test_a_regular_file_at_dist_fails_closed_instead_of_raising_oserror(self) -> None:
        shutil.rmtree(self.repo / "frontend" / "dist", ignore_errors=True)
        (self.repo / "frontend" / "dist").write_text("not a directory\n", encoding="utf-8")

        self.refuse(lambda: self.refresh(), "DIST_NONREGULAR")
        self.assertEqual(
            "not a directory\n", (self.repo / "frontend" / "dist").read_text(encoding="utf-8")
        )

    def test_a_symlinked_dist_is_refused_before_anything_is_deleted(self) -> None:
        outside = Path(self.temporary) / "outside"
        (outside / "keep").mkdir(parents=True)
        (outside / "keep" / "evidence.txt").write_text("untouched\n", encoding="utf-8")
        if not make_link(outside / "keep", self.repo / "frontend" / "dist"):
            self.skipTest("this host does not allow creating a directory link")

        self.refuse(lambda: self.refresh(), "DIST_PATH_ESCAPE")

        self.assertTrue((outside / "keep" / "evidence.txt").is_file())

    def test_a_redirected_ancestor_is_refused_before_anything_is_deleted(self) -> None:
        outside = Path(self.temporary) / "elsewhere"
        (outside / "dist").mkdir(parents=True)
        (outside / "dist" / "evidence.txt").write_text("untouched\n", encoding="utf-8")
        redirected = Path(self.temporary) / "redirected"
        redirected.mkdir()
        if not make_link(outside, redirected / "frontend"):
            self.skipTest("this host does not allow creating a directory link")

        failure = self.refuse(
            lambda: GATE.refresh(redirected, build_command=self.build_command()), "DIST_PATH_ESCAPE"
        )

        self.assertIn("symlink or junction", failure.detail)
        self.assertTrue((outside / "dist" / "evidence.txt").is_file())

    def test_a_redirected_artifact_directory_is_refused_before_staging(self) -> None:
        outside = Path(self.temporary) / "artifacts-elsewhere"
        outside.mkdir()
        (outside / "evidence.txt").write_text("untouched\n", encoding="utf-8")
        if not make_link(outside, self.repo / GATE.ARTIFACT_DIR):
            self.skipTest("this host does not allow creating a directory link")

        self.refuse(lambda: self.refresh(), "DIST_PATH_ESCAPE")

        self.assertTrue((outside / "evidence.txt").is_file())

    def test_a_failed_replacement_restores_the_previous_output(self) -> None:
        stale = write_stale_dist(self.repo)
        real_replace = GATE.os.replace
        calls: list[int] = []

        def flaky(source, target):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(13, "simulated rename failure")
            return real_replace(source, target)

        with mock.patch.object(GATE.os, "replace", flaky):
            failure = self.refuse(lambda: self.refresh(), "DIST_REPLACE_FAILED")

        self.assertIn("restored", failure.detail)
        self.assertEqual(stale, tree_hashes(self.repo / "frontend" / "dist"))
        self.assertFalse(GATE.receipt_path(self.repo).exists())

    def test_an_unrecoverable_replacement_names_the_preserved_tree(self) -> None:
        write_stale_dist(self.repo)
        real_replace = GATE.os.replace

        def always_failing(source, target):
            if Path(target).name == "dist":
                raise OSError(13, "simulated rename failure")
            return real_replace(source, target)

        with mock.patch.object(GATE.os, "replace", always_failing):
            failure = self.refuse(lambda: self.refresh(), "DIST_REPLACE_FAILED")

        self.assertIsNotNone(failure.preserved)
        preserved = Path(failure.preserved)
        self.assertTrue(preserved.is_dir(), failure.detail)
        self.assertIn("previous", failure.detail)
        self.assertTrue((preserved / "index.html").is_file())
        shutil.rmtree(preserved.parent, ignore_errors=True)

    # --- the receipt schema ------------------------------------------------

    def test_unknown_receipt_schema_is_refused(self) -> None:
        self.refresh()
        GATE.receipt_path(self.repo).write_text('{"schema_version": 99}\n', encoding="utf-8")

        self.refuse(lambda: GATE.verify(self.repo), "DIST_RECEIPT_INVALID")

    def test_a_receipt_without_source_inputs_is_refused(self) -> None:
        self.refresh()
        location = GATE.receipt_path(self.repo)
        receipt = json.loads(location.read_text(encoding="utf-8"))
        receipt.pop("source_inputs")
        location.write_text(json.dumps(receipt), encoding="utf-8")

        failure = self.refuse(lambda: GATE.verify(self.repo), "DIST_RECEIPT_INVALID")
        self.assertIn("unexpected fields", failure.detail)

    def test_a_receipt_whose_digest_does_not_summarise_its_inputs_is_refused(self) -> None:
        self.refresh()
        location = GATE.receipt_path(self.repo)
        receipt = json.loads(location.read_text(encoding="utf-8"))
        receipt["source_inputs"][0]["sha256"] = "sha256:" + "0" * 64
        location.write_text(json.dumps(receipt), encoding="utf-8")

        failure = self.refuse(lambda: GATE.verify(self.repo), "DIST_RECEIPT_INVALID")
        self.assertIn("source_inputs", failure.detail)

    def test_a_receipt_with_a_malformed_manifest_entry_is_refused(self) -> None:
        self.refresh()
        location = GATE.receipt_path(self.repo)
        receipt = json.loads(location.read_text(encoding="utf-8"))
        receipt["dist"]["files"][0].pop("size")
        location.write_text(json.dumps(receipt), encoding="utf-8")

        failure = self.refuse(lambda: GATE.verify(self.repo), "DIST_RECEIPT_INVALID")
        self.assertIn("dist.files", failure.detail)

    def test_a_receipt_whose_inputs_are_out_of_order_is_refused(self) -> None:
        self.refresh()
        location = GATE.receipt_path(self.repo)
        receipt = json.loads(location.read_text(encoding="utf-8"))
        receipt["source_inputs"].reverse()
        receipt["source_digest"] = GATE.source_digest(receipt["source_inputs"])
        location.write_text(json.dumps(receipt), encoding="utf-8")

        failure = self.refuse(lambda: GATE.verify(self.repo), "DIST_RECEIPT_INVALID")
        self.assertIn("sorted", failure.detail)

    # --- the repair path ---------------------------------------------------

    def test_stale_dist_is_regenerated_before_it_can_be_packaged(self) -> None:
        stale = write_stale_dist(self.repo)

        result = self.refresh()

        self.assertTrue(result["replaced_dist"])
        current = tree_hashes(self.repo / "frontend" / "dist")
        self.assertNotEqual(stale, current)
        self.assertNotIn("assets/app-00000000.js", current)
        self.assertIsNotNone(GATE.verify(self.repo))

    def test_a_source_change_after_a_receipt_is_repaired_by_one_refresh(self) -> None:
        self.refresh()
        (self.repo / "frontend" / "src" / "main.tsx").write_text("export const main = 3\n", encoding="utf-8")
        self.refuse(lambda: GATE.verify(self.repo), "DIST_SOURCE_DRIFT")

        result = self.refresh()

        self.assertTrue(result["replaced_dist"])
        self.assertIsNotNone(GATE.verify(self.repo))

    def test_a_failing_build_never_writes_a_receipt(self) -> None:
        write_stale_dist(self.repo)

        failure = self.refuse(
            lambda: GATE.refresh(self.repo, build_command=self.build_command(STUB_FAILING)),
            "DIST_BUILD_FAILED",
        )

        self.assertIn("exit 3", failure.detail)
        self.assertFalse(GATE.receipt_path(self.repo).exists())

    def test_an_absent_build_tool_refuses_instead_of_trusting_the_tree(self) -> None:
        write_stale_dist(self.repo)

        failure = self.refuse(
            lambda: GATE.refresh(
                self.repo, build_command=("workstack-no-such-build-tool", "run", "build")
            ),
            "DIST_BUILD_TOOL_MISSING",
        )

        self.assertIn("refresh-dist", failure.detail)
        self.assertFalse(GATE.receipt_path(self.repo).exists())

    def test_no_staging_tree_survives_a_refresh(self) -> None:
        self.refresh()

        leftovers = [path.name for path in (self.repo / ".artifacts").iterdir()]

        self.assertEqual([GATE.RECEIPT_NAME], sorted(leftovers))

    # --- the digest itself -------------------------------------------------

    def test_the_digest_is_content_addressed_and_ignores_timestamps(self) -> None:
        entries = GATE.source_entries(self.repo)
        first = GATE.source_digest(entries)
        target = self.repo / "frontend" / "src" / "main.tsx"
        data = target.read_bytes()
        target.write_bytes(b"// touched\n")
        target.write_bytes(data)

        self.assertEqual(first, GATE.source_digest(GATE.source_entries(self.repo)))
        self.assertTrue(first.startswith("sha256:"))

    def test_the_default_receipt_lives_in_the_ignored_artifacts_directory(self) -> None:
        self.assertEqual(
            (self.repo / ".artifacts" / "dist-source-receipt.json"), GATE.receipt_path(self.repo)
        )


class StagedDistConsumptionTest(unittest.TestCase):
    """What a packager actually copies must be the tree the gate admitted.

    frontend/dist is generated and git-ignored, so an ordinary process may
    rewrite it the instant a successful gate call returns. These tests assert on
    the bytes a package would ship, not on the order the gate was called in.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.mkdtemp(prefix="workstack-dist-staged-")
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.repo = Path(self.temporary) / "repo"
        self.repo.mkdir()
        write_repo(self.repo)
        self.stub = Path(self.temporary) / "stub_build.py"
        self.stub.write_text(STUB_BUILD, encoding="utf-8")
        self.dist = self.repo / "frontend" / "dist"
        self.admitted = GATE.refresh(self.repo, build_command=(sys.executable, str(self.stub)))
        self.package = Path(self.temporary) / "package"
        self.package.mkdir()

    def stage(self) -> Path:
        """Copy the live dist the way a packager does, into a private tree."""

        staged = self.package / "payload" / "frontend" / "dist"
        if staged.is_dir():
            shutil.rmtree(staged)
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.dist, staged)
        return staged

    def refuse_staged(self, staged: Path) -> GATE.DistSourceGateError:
        with self.assertRaises(GATE.DistSourceGateError) as raised:
            GATE.verify_staged_dist(staged, self.admitted)
        self.assertEqual("DIST_STAGED_DRIFT", raised.exception.code)
        with self.assertRaises(GATE.DistSourceGateError) as by_digest:
            GATE.verify_staged_dist(staged, self.admitted["dist_digest"])
        self.assertEqual("DIST_STAGED_DRIFT", by_digest.exception.code)
        return raised.exception

    def emitted_asset(self) -> Path:
        return next(path for path in sorted(self.dist.rglob("*.js")) if path.is_file())

    # --- what the gate hands back ------------------------------------------

    def test_refresh_and_verify_hand_back_the_tree_they_admitted(self) -> None:
        receipt = json.loads(GATE.receipt_path(self.repo).read_text(encoding="utf-8"))

        self.assertEqual(receipt["dist"]["files"], self.admitted["dist_files"])
        self.assertEqual(
            GATE.dist_digest(receipt["dist"]["files"]), self.admitted["dist_digest"]
        )
        again = GATE.verify(self.repo)
        self.assertEqual(self.admitted["dist_digest"], again["dist_digest"])
        self.assertEqual(self.admitted["dist_files"], again["dist_files"])
        self.assertEqual(71, len(self.admitted["dist_digest"]))

    # --- the accepted case --------------------------------------------------

    def test_an_untouched_staged_copy_is_accepted_by_manifest_and_by_digest(self) -> None:
        staged = self.stage()

        summary = GATE.verify_staged_dist(staged, self.admitted)
        by_digest = GATE.verify_staged_dist(staged, self.admitted["dist_digest"])

        self.assertEqual(self.admitted["dist_digest"], summary["dist_digest"])
        self.assertEqual(summary["dist_digest"], by_digest["dist_digest"])
        self.assertEqual(self.admitted["dist_file_count"], summary["dist_file_count"])
        self.assertEqual(
            tree_hashes(self.dist), tree_hashes(staged), "the admitted bytes are what was staged"
        )

    # --- the consumption counterexamples ------------------------------------

    def test_an_asset_mutated_after_the_gate_cannot_be_staged(self) -> None:
        """Successful gate, then an ordinary rewrite, then the copy."""

        asset = self.emitted_asset()
        relative = asset.relative_to(self.dist).as_posix()
        asset.write_bytes(b"export const build = 666\n")

        staged = self.stage()
        failure = self.refuse_staged(staged)

        self.assertIn(relative, failure.detail)
        self.assertIn(self.admitted["dist_digest"], failure.detail)

    def test_a_live_change_restored_after_the_copy_is_still_refused(self) -> None:
        """A fresh live re-check would pass here; the staged bytes are mixed."""

        asset = self.emitted_asset()
        original = asset.read_bytes()
        asset.write_bytes(b"export const build = 777\n")
        staged = self.stage()
        asset.write_bytes(original)

        # The live tree is honest again, so re-verifying it proves nothing about
        # what was copied -- and hashing the copy on its own only certifies the
        # copy. Only the retained admitted manifest catches the mixed snapshot.
        self.assertEqual(self.admitted["dist_digest"], GATE.verify(self.repo)["dist_digest"])
        self.assertNotEqual(
            self.admitted["dist_digest"], GATE.dist_digest(GATE.dist_entries(staged))
        )
        failure = self.refuse_staged(staged)

        self.assertIn(asset.relative_to(self.dist).as_posix(), failure.detail)

    def test_an_added_emitted_file_restored_after_the_copy_is_refused(self) -> None:
        extra = self.dist / "assets" / "injected.js"
        extra.write_bytes(b"export const injected = 1\n")
        staged = self.stage()
        extra.unlink()

        self.assertEqual(self.admitted["dist_digest"], GATE.verify(self.repo)["dist_digest"])
        failure = self.refuse_staged(staged)

        self.assertIn("assets/injected.js", failure.detail)

    def test_a_deleted_emitted_file_restored_after_the_copy_is_refused(self) -> None:
        asset = self.emitted_asset()
        relative = asset.relative_to(self.dist).as_posix()
        original = asset.read_bytes()
        asset.unlink()
        staged = self.stage()
        asset.write_bytes(original)

        self.assertEqual(self.admitted["dist_digest"], GATE.verify(self.repo)["dist_digest"])
        failure = self.refuse_staged(staged)

        self.assertIn(relative, failure.detail)

    def test_an_emptied_or_missing_staged_tree_is_refused(self) -> None:
        staged = self.stage()
        for path in sorted(staged.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        with self.assertRaises(GATE.DistSourceGateError) as emptied:
            GATE.verify_staged_dist(staged, self.admitted)
        self.assertEqual("DIST_MISSING", emptied.exception.code)

        staged.rmdir()
        with self.assertRaises(GATE.DistSourceGateError) as absent:
            GATE.verify_staged_dist(staged, self.admitted)
        self.assertEqual("DIST_MISSING", absent.exception.code)

    def test_an_unbound_admitted_value_is_refused_rather_than_assumed(self) -> None:
        staged = self.stage()

        for value in (
            {},
            {"dist_files": []},
            {"dist_files": [{"path": "index.html"}]},
            {"dist_digest": self.admitted["dist_digest"]},
            "",
            "sha256:not-a-digest",
            "the tree looked fine",
        ):
            with self.subTest(value=value):
                with self.assertRaises(GATE.DistSourceGateError) as raised:
                    GATE.verify_staged_dist(staged, value)
                self.assertEqual("DIST_STAGED_UNBOUND", raised.exception.code)

    def test_the_staged_check_never_reads_the_live_dist(self) -> None:
        """Deleting the live tree entirely cannot change the staged verdict."""

        staged = self.stage()
        shutil.rmtree(self.dist)

        summary = GATE.verify_staged_dist(staged, self.admitted)

        self.assertEqual(self.admitted["dist_digest"], summary["dist_digest"])
        self.assertFalse(self.dist.exists())


if __name__ == "__main__":
    unittest.main()

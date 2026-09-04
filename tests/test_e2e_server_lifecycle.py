"""Fixture contract tests for the acceptance lifecycle in scripts/run_e2e_server.py.

These are SOURCE tests. They import the fixture module with a contained
environment and exercise its pure decisions; they construct no WorkStack, no
Store, no server and no process, and they are not browser or runtime evidence.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "run_e2e_server.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_e2e_server_fixture", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


class AcceptancePathsTest(unittest.TestCase):
    def test_every_owned_path_is_under_the_control_root(self):
        root = Path("C:/owned/control-root")
        paths = MODULE.acceptance_paths(root)
        for name, path in paths.items():
            self.assertTrue(str(path).startswith(str(root)), f"{name} escaped the control root")
        self.assertEqual(paths["ready"].name, "ready.json")
        self.assertEqual(paths["stop"].name, "stop.request")
        self.assertEqual(paths["completion"].name, "completion.json")


class RefusalTest(unittest.TestCase):
    def test_a_relative_control_root_is_refused(self):
        root = Path("relative-root")
        with self.assertRaises(MODULE.AcceptanceRefusal):
            MODULE.refuse_prefilled_root(root, MODULE.acceptance_paths(root))

    def test_a_prefilled_record_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = MODULE.acceptance_paths(root)
            # Healthy control: an empty absolute root is accepted.
            MODULE.refuse_prefilled_root(root, paths)
            for name in ("ready", "completion", "stop"):
                target = paths[name]
                target.write_text("{}", encoding="utf-8")
                with self.assertRaises(MODULE.AcceptanceRefusal, msg=name):
                    MODULE.refuse_prefilled_root(root, paths)
                target.unlink()

    def test_a_prefilled_runtime_subtree_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = MODULE.acceptance_paths(root)
            MODULE.refuse_prefilled_root(root, paths)
            paths["runtime"].mkdir()
            with self.assertRaises(MODULE.AcceptanceRefusal):
                MODULE.refuse_prefilled_root(root, paths)

    def test_a_refused_relative_root_is_never_created(self):
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            os.chdir(directory)
            try:
                with self.assertRaises(MODULE.AcceptanceRefusal):
                    MODULE.run_acceptance(18791, Path("relative-root"), "run-a", 1.0)
                self.assertFalse((Path(directory) / "relative-root").exists())
            finally:
                os.chdir(previous)

    def test_an_occupied_loopback_port_is_refused_even_with_reuseaddr(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with self.assertRaises(MODULE.AcceptanceRefusal):
                MODULE.refuse_occupied_port(port)
        finally:
            listener.close()
        # Healthy control: once released, the same port is accepted.
        MODULE.refuse_occupied_port(port)

    def test_a_nonpositive_budget_is_refused(self):
        for budget in (0.0, -1.0):
            with self.assertRaises(MODULE.AcceptanceRefusal, msg=str(budget)):
                MODULE.refuse_nonpositive_budget(budget)
        MODULE.refuse_nonpositive_budget(0.5)

    def test_a_missing_or_asset_free_dist_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            with self.assertRaises(MODULE.AcceptanceRefusal):
                MODULE.dist_manifest(project)
            dist = project / "frontend" / "dist"
            dist.mkdir(parents=True)
            (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
            with self.assertRaises(MODULE.AcceptanceRefusal):
                MODULE.dist_manifest(project)

    def test_a_built_dist_yields_a_bound_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory).resolve()
            assets = project / "frontend" / "dist" / "assets"
            assets.mkdir(parents=True)
            index = project / "frontend" / "dist" / "index.html"
            index.write_text("<!doctype html>", encoding="utf-8")
            (assets / "app.js").write_text("console.log(1)", encoding="utf-8")
            manifest = MODULE.dist_manifest(project)
            self.assertEqual(manifest["asset_count"], 1)
            self.assertEqual(len(manifest["index_sha256"]), 64)
            self.assertEqual(manifest["assets"], ["frontend/dist/assets/app.js"])


class StopAndDeadlineTest(unittest.TestCase):
    def test_only_this_run_stop_request_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = MODULE.acceptance_paths(Path(directory).resolve())
            self.assertFalse(MODULE.stop_requested(paths, "run-a"))
            paths["stop"].write_text("run-b", encoding="utf-8")
            self.assertFalse(MODULE.stop_requested(paths, "run-a"))
            paths["stop"].write_text("run-a\n", encoding="utf-8")
            self.assertTrue(MODULE.stop_requested(paths, "run-a"))

    def test_the_self_deadline_is_finite_and_not_extended(self):
        self.assertFalse(MODULE.deadline_expired(100.0, 10.0, 105.0))
        self.assertTrue(MODULE.deadline_expired(100.0, 10.0, 110.0))
        # A non-positive budget is refused before any run (see
        # test_a_nonpositive_budget_is_refused); the predicate never sees it.


class ContainmentTest(unittest.TestCase):
    def test_runtime_writes_are_contained_and_home_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = MODULE.acceptance_paths(root)
            before = dict(os.environ)
            home = os.environ.get("HOME")
            profile = os.environ.get("USERPROFILE")
            try:
                MODULE.contain_child_environment(paths)
                for key in ("TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA",
                            "XDG_CACHE_HOME", "WORK_STACK_HOME", "WORK_STACK_RUNTIME"):
                    self.assertTrue(os.environ[key].startswith(str(root)), key)
                self.assertEqual(os.environ.get("HOME"), home)
                self.assertEqual(os.environ.get("USERPROFILE"), profile)
            finally:
                os.environ.clear()
                os.environ.update(before)


class RecordTest(unittest.TestCase):
    def test_the_ready_record_binds_the_identity_a_parent_must_cross_check(self):
        paths = MODULE.acceptance_paths(Path("C:/owned/root"))
        record = MODULE.ready_record(
            "run-a", 18791, Path("C:/owned/root/temp/data"), paths,
            {"asset_count": 2, "index_sha256": "a" * 64}, "WS-UID",
        )
        self.assertEqual(record["run_id"], "run-a")
        self.assertEqual(record["port"], 18791)
        self.assertEqual(record["workspace_uid"], "WS-UID")
        self.assertTrue(str(record["runtime_dir"]).startswith("C:"))
        self.assertGreater(record["pid"], 0)
        actual = MODULE.ready_record(
            "run-a", 18791, Path("C:/owned/root/temp/data"), paths,
            {"asset_count": 2, "index_sha256": "a" * 64}, "WS-UID",
            runtime_dir=Path("C:/owned/root/runtime/0123456789abcdef0123"),
        )
        self.assertTrue(str(actual["runtime_dir"]).endswith("0123456789abcdef0123"))

    def test_records_round_trip_as_sorted_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "completion.json"
            MODULE.write_record(path, {"run_id": "run-a", "reason": "stop-request"})
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"reason": "stop-request", "run_id": "run-a"},
            )


class OrdinaryModeTest(unittest.TestCase):
    """Load-bearing inert control: the existing no-flag behaviour is preserved."""

    def test_no_acceptance_root_still_selects_the_ordinary_mode(self):
        calls = []
        original_ordinary = MODULE.run_ordinary
        original_acceptance = MODULE.run_acceptance
        MODULE.run_ordinary = lambda port: calls.append(("ordinary", port)) or 0
        MODULE.run_acceptance = lambda *args: calls.append(("acceptance", args)) or 0
        try:
            self.assertEqual(MODULE.main(["--port", "18781"]), 0)
            self.assertEqual(calls, [("ordinary", 18781)])
        finally:
            MODULE.run_ordinary = original_ordinary
            MODULE.run_acceptance = original_acceptance

    def test_acceptance_mode_requires_an_explicit_run_id(self):
        with self.assertRaises(MODULE.AcceptanceRefusal):
            MODULE.main(["--acceptance-root", "C:/owned/root"])

    def test_acceptance_mode_refuses_an_unbounded_budget_before_running(self):
        original_acceptance = MODULE.run_acceptance
        MODULE.run_acceptance = lambda *args: self.fail("run_acceptance must not run")
        try:
            with self.assertRaises(MODULE.AcceptanceRefusal):
                MODULE.main(["--acceptance-root", "C:/owned/root", "--run-id", "run-a",
                             "--budget-seconds", "0"])
        finally:
            MODULE.run_acceptance = original_acceptance


if __name__ == "__main__":
    unittest.main()

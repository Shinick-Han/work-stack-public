"""Contract for the canonical backend test launcher.

The launcher is exercised as the real script: a throwaway repository is built
under the contained temporary root, the launcher is copied into its ``scripts``
directory, and it is started as a child process the same way CI starts it. That
keeps the proof honest about the environment sanitisation and the native exit
status, which is precisely what an in-process call would hide.

Every proof about live Work Stack data uses a disposable stand-in tree built
under the contained temporary root. No test names, reads or writes a real Work
Stack directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "run_backend_tests.py"

FIXTURE_BASE = Path(tempfile.gettempdir()).resolve()

PASSING_SUITE = """import unittest


class Passing(unittest.TestCase):
    def test_environment_is_contained(self):
        import json, os, sys
        from pathlib import Path

        root = Path(os.environ["WORKSTACK_TEST_RESULTS_ROOT"]).resolve()
        report = {
            "contained": {},
            "aliases": sorted(n for n in os.environ if n.startswith("STALE_")),
            "encoding": {n: os.environ.get(n) for n in ("PYTHONUTF8", "PYTHONIOENCODING")},
            "coverage": {n: os.environ.get(n) for n in ("COVERAGE_FILE", "COVERAGE_RCFILE")},
            "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
            "writes_bytecode": sys.dont_write_bytecode is False,
            "unrelated": os.environ.get("UNRELATED_INHERITED"),
            "root": str(root),
        }
        for name in ("WORKSTACK_TEST_FIXTURE_ROOT", "WORK_STACK_TEST_RESULT_ROOT", "TEMP",
                     "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "WORK_STACK_HOME",
                     "WORK_STACK_RUNTIME", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX"):
            path = Path(os.environ[name])
            report["contained"][name] = path.is_absolute() and path.resolve().is_relative_to(root)
        Path(os.environ["WORKSTACK_TEST_FIXTURE_ROOT"], "probe.json").write_text(
            json.dumps(report), encoding="utf-8")
"""

FAILING_SUITE = """import unittest


class Failing(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2)
"""

# A configuration a hostile COVERAGE_RCFILE would impose: no branch coverage and
# a different measured source. Neither may reach the child.
FOREIGN_COVERAGERC = "[run]\nbranch = False\nsource =\n    scripts\n"


def _load_launcher():
    specification = importlib.util.spec_from_file_location("run_backend_tests_under_test", LAUNCHER)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


launcher = _load_launcher()


def snapshot_tree(root: Path) -> list[tuple[str, int]]:
    """Every entry below a directory with its size, so a refusal can be proved."""

    entries: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*")):
        entries.append((str(path.relative_to(root)), path.stat().st_size if path.is_file() else -1))
    return entries


def make_directory_link(link: Path, target: Path) -> bool:
    """Create a junction (Windows) or a symlink; report whether the host allowed it."""

    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, timeout=60,
        )
        return completed.returncode == 0 and link.exists()
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return True


class HostileEnvironment:
    """An inherited environment a developer machine or a stale CI job can produce."""

    def __init__(self, foreign: Path) -> None:
        self.foreign = foreign
        self.values = {
            "WORKSTACK_TEST_RESULTS_ROOT": str(foreign / "stale-results"),
            "WORKSTACK_TEST_FIXTURE_ROOT": str(foreign / "stale-fixtures"),
            "WORK_STACK_TEST_RESULT_ROOT": str(foreign / "stale-alias"),
            "WORK_STACK_HOME": str(foreign / "live" / "data"),
            "WORK_STACK_RUNTIME": str(foreign / "live" / "runtime"),
            "TEMP": str(foreign / "host-temp"),
            "TMP": str(foreign / "host-temp"),
            "APPDATA": str(foreign / "host-app"),
            "LOCALAPPDATA": str(foreign / "host-local"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "COVERAGE_FILE": str(foreign / "escaped.coverage"),
            "COVERAGE_RCFILE": str(foreign / "escaped.coveragerc"),
            "STALE_WORK_STACK_RESULT_DIR": str(foreign / "stale-extra"),
            "UNRELATED_INHERITED": "kept",
        }

    def apply(self, environment: dict) -> dict:
        merged = dict(environment)
        merged.update(self.values)
        return merged


class LauncherFixture:
    """A throwaway repository holding a copy of the launcher and one small suite."""

    def __init__(self, directory: Path, body: str) -> None:
        self.root = directory
        (self.root / "scripts").mkdir(parents=True, exist_ok=True)
        (self.root / "tests").mkdir(parents=True, exist_ok=True)
        shutil.copy2(LAUNCHER, self.root / "scripts" / "run_backend_tests.py")
        (self.root / "tests" / "test_sample_probe.py").write_text(body, encoding="utf-8")
        (self.root / ".coveragerc").write_text("[run]\nbranch = True\nsource =\n    tests\n", encoding="utf-8")
        self.result_base = self.root / "rr"
        self.result_base.mkdir(parents=True, exist_ok=True)

    def run(self, *extra: str, environment: dict | None = None,
            result_root: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", str(self.root / "scripts" / "run_backend_tests.py"),
             "--pattern", "test_sample_probe.py",
             "--result-root", result_root if result_root is not None else str(self.result_base),
             *extra],
            capture_output=True, text=True, timeout=300,
            env=environment if environment is not None else dict(os.environ),
        )

    def receipt(self, completed: subprocess.CompletedProcess) -> dict:
        lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        if not lines:
            raise AssertionError("launcher printed no receipt: " + completed.stdout + completed.stderr)
        return json.loads(lines[-1])

    def coverage_json(self) -> dict:
        """Render the canonical coverage JSON from the data the launcher produced."""

        destination = self.root / "rendered-coverage.json"
        clean = {n: v for n, v in os.environ.items() if not launcher.is_coverage_control(n)}
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "coverage", "json", "-o", str(destination)],
            cwd=str(self.root), capture_output=True, text=True, timeout=300, env=clean,
        )
        if completed.returncode != 0:
            raise AssertionError("coverage json failed: " + completed.stdout + completed.stderr)
        return json.loads(destination.read_text(encoding="utf-8"))


class ArgumentContract(unittest.TestCase):
    """The launcher accepts a focused pattern and coverage, never a shell string."""

    def test_default_discovery_matches_the_previous_ci_invocation(self):
        argv = launcher.build_child_argv(launcher.DEFAULT_PATTERN, False, True)
        self.assertEqual(argv[1:], ["-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"])

    def test_coverage_argv_wraps_the_same_discovery(self):
        plain = launcher.build_child_argv("test_focus*.py", False, True)
        covered = launcher.build_child_argv("test_focus*.py", True, True)
        self.assertEqual(covered[1:5], ["-B", "-m", "coverage", "run"])
        self.assertEqual(covered[5:], plain[2:])
        self.assertIn("test_focus*.py", covered)
        # Branch coverage and the JSON destination stay in .coveragerc, not argv.
        self.assertNotIn("--branch", covered)

    def test_quiet_drops_only_the_verbose_flag(self):
        self.assertNotIn("-v", launcher.build_child_argv("test_a.py", False, False))

    def test_arbitrary_shell_strings_are_rejected(self):
        for candidate in ("test_a.py && rm -rf /", "../outside/test_a.py", "tests/test_a.py",
                          "C:\\live\\test_a.py", "", "..", "test_a.py;echo"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValueError):
                    launcher.build_child_argv(candidate, False, True)


class ResultRootContract(unittest.TestCase):
    """Roots are unique, absolute, short and never a live Work Stack directory."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=str(FIXTURE_BASE))).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_concurrent_allocations_never_collide(self):
        roots = {launcher.allocate_result_root(self.base) for _ in range(8)}
        self.assertEqual(len(roots), 8)
        for root in roots:
            self.assertTrue(root.is_absolute() and root.is_dir())
            self.assertTrue(root.resolve().is_relative_to(self.base))
            self.assertLessEqual(len(root.name), 12)

    def test_preflight_refuses_a_root_inside_a_live_workstack_directory(self):
        live = self.base / "live" / "data"
        live.mkdir(parents=True)
        root = live / "inside"
        root.mkdir()
        environment = launcher.build_child_environment({"WORK_STACK_HOME": str(live)}, root)
        with unittest.mock.patch.dict(os.environ, {"WORK_STACK_HOME": str(live)}):
            with self.assertRaises(launcher.ContainmentError):
                launcher.prove_containment(environment, root)

    def test_preflight_refuses_a_root_that_contains_a_live_workstack_directory(self):
        root = launcher.allocate_result_root(self.base)
        live = root / "live"
        live.mkdir()
        environment = launcher.build_child_environment({"WORK_STACK_RUNTIME": str(live)}, root)
        with unittest.mock.patch.dict(os.environ, {"WORK_STACK_RUNTIME": str(live)}):
            with self.assertRaises(launcher.ContainmentError):
                launcher.prove_containment(environment, root)

    def test_preflight_creates_and_proves_every_contained_path(self):
        root = launcher.allocate_result_root(self.base)
        environment = launcher.build_child_environment({"PYTHONUTF8": "1"}, root)
        selected = launcher.prove_containment(environment, root)
        for name, _relative in launcher.CONTAINED_PATHS:
            self.assertTrue(Path(selected[name]).is_dir(), name)
            self.assertTrue(Path(selected[name]).resolve().is_relative_to(root), name)
        self.assertEqual(selected["PYTHONDONTWRITEBYTECODE"], "1")

    def test_preflight_rejects_an_escaping_contained_path(self):
        root = launcher.allocate_result_root(self.base)
        environment = launcher.build_child_environment({}, root)
        environment["APPDATA"] = str(self.base / "escaped")
        with self.assertRaises(launcher.ContainmentError):
            launcher.prove_containment(environment, root)

    def test_differently_cased_spellings_of_one_directory_overlap(self):
        first = Path("C:\\Users\\Someone\\WorkStack\\SSOT")
        second = Path("c:\\users\\someone\\workstack\\ssot\\main")
        self.assertEqual(launcher.paths_overlap(first, second), os.name == "nt")
        self.assertTrue(launcher.paths_overlap(first, first))
        self.assertFalse(launcher.paths_overlap(first, Path("C:\\Users\\Someone\\Other")))

    def test_the_known_ssot_location_is_forbidden_without_any_environment_hint(self):
        stand_in = self.base / "profile"
        live = launcher.live_workstack_directories({"USERPROFILE": str(stand_in), "HOME": str(stand_in)})
        self.assertIn(stand_in / "WorkStack" / "SSOT" / "main", live)
        # The operator's real SSOT is named only as a read-only assertion.
        real = launcher.live_workstack_directories({})
        self.assertIn(Path.home() / "WorkStack" / "SSOT" / "main", real)


class RefusalContract(unittest.TestCase):
    """A forbidden base is refused before allocation: no writes, and no child.

    ``stand_in`` is a disposable directory shaped like live Work Stack data. The
    real directories are never used as a subject of these proofs.
    """

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=str(FIXTURE_BASE))).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.stand_in = self.base / "profile"
        self.live = self.stand_in / "WorkStack" / "SSOT" / "main"
        self.live.mkdir(parents=True)
        (self.live / "agent.status.json").write_text('{"owner":"stand-in"}', encoding="utf-8")
        (self.live / "backlog.jsonl").write_text('{"id":"T-1"}\n', encoding="utf-8")
        self.before = snapshot_tree(self.stand_in)

    def refuse(self, result_root, environment: dict[str, str] | None = None) -> Exception:
        """Run the launcher, require a refusal, and prove the tree is untouched."""

        with unittest.mock.patch.dict(os.environ, environment or {}, clear=False):
            with unittest.mock.patch.object(launcher.subprocess, "Popen") as popen:
                with self.assertRaises(launcher.ContainmentError) as raised:
                    launcher.main(["--pattern", "test_sample_probe.py", "--result-root", str(result_root)])
        popen.assert_not_called()
        self.assertEqual(snapshot_tree(self.stand_in), self.before)
        return raised.exception

    def test_a_base_equal_to_a_live_directory_is_refused_untouched(self):
        self.refuse(self.live, {"WORK_STACK_HOME": str(self.live)})

    def test_default_candidates_never_probe_hostile_inherited_temp(self):
        environment = {key: str(self.live) for key in ("TMPDIR", "TEMP", "TMP", "WORK_STACK_HOME")}
        operations = []
        real_open = os.open

        def observe_open(path, *args, **kwargs):
            operations.append(os.fspath(path))
            return real_open(path, *args, **kwargs)

        with unittest.mock.patch.dict(os.environ, environment):
            with unittest.mock.patch.object(tempfile, "tempdir", None):
                with unittest.mock.patch.object(os, "open", side_effect=observe_open):
                    candidates = launcher.default_result_bases()
        self.assertEqual(operations, [])
        self.assertIn(self.live / "wsbt", candidates)
        self.assertEqual(snapshot_tree(self.stand_in), self.before)
        with self.assertRaises(launcher.ContainmentError):
            launcher.assert_base_is_allowed(candidates[-1], environment)

    def test_a_base_nested_in_a_live_directory_is_refused_untouched(self):
        self.refuse(self.live / "results", {"WORK_STACK_HOME": str(self.live)})

    def test_a_base_containing_a_live_directory_is_refused_untouched(self):
        self.refuse(self.stand_in / "WorkStack", {"WORK_STACK_RUNTIME": str(self.live)})

    def test_the_known_ssot_location_is_refused_without_an_environment_hint(self):
        failure = self.refuse(self.live / "rr", {"USERPROFILE": str(self.stand_in), "HOME": str(self.stand_in)})
        self.assertIn("SSOT", str(failure))

    def test_a_relative_result_root_is_refused_before_it_is_resolved(self):
        marker = "relative-base-must-never-be-created"
        with unittest.mock.patch.object(launcher.subprocess, "Popen") as popen:
            with self.assertRaises(launcher.ContainmentError) as raised:
                launcher.main(["--result-root", str(Path(marker) / "inner")])
        popen.assert_not_called()
        self.assertIn("absolute", str(raised.exception))
        self.assertFalse((Path.cwd() / marker).exists())

    def test_a_redirected_ancestor_is_refused_before_allocation(self):
        real = self.base / "real"
        real.mkdir()
        (real / "kept.txt").write_text("kept", encoding="utf-8")
        link = self.base / "link"
        if not make_directory_link(link, real):
            self.skipTest("this host cannot create a directory junction or symlink")
        before = snapshot_tree(real)
        with unittest.mock.patch.object(launcher.subprocess, "Popen") as popen:
            with self.assertRaises(launcher.ContainmentError) as raised:
                launcher.main(["--result-root", str(link / "inner")])
        popen.assert_not_called()
        self.assertIn("redirected", str(raised.exception))
        self.assertEqual(snapshot_tree(real), before)

    def test_a_real_child_launch_refuses_with_the_refusal_status(self):
        fixture = LauncherFixture(self.base / "repo", PASSING_SUITE)
        environment = dict(os.environ)
        environment["WORK_STACK_HOME"] = str(self.live)
        completed = fixture.run(result_root=str(self.live / "rr"), environment=environment)
        self.assertEqual(completed.returncode, launcher.REFUSAL_EXIT, completed.stdout + completed.stderr)
        self.assertIn("refused:", completed.stderr)
        self.assertEqual(completed.stdout.strip(), "")
        self.assertEqual(snapshot_tree(self.stand_in), self.before)
        # No receipt directory either: refusal happens before any launcher output.
        self.assertFalse((fixture.root / ".artifacts").exists())


class SanitisationContract(unittest.TestCase):
    """Stale aliases and encoding overrides are dropped; unrelated names survive."""

    def test_alias_and_encoding_names_are_recognised(self):
        for name in ("WORKSTACK_TEST_RESULTS_ROOT", "WORK_STACK_TEST_RESULT_ROOT",
                     "WORKSTACK_TEST_FIXTURE_ROOT", "STALE_WORK_RESULT_DIR",
                     "RESULTS_ROOT", "PYTHONUTF8", "PYTHONIOENCODING",
                     "PYTHONLEGACYWINDOWSSTDIO"):
            self.assertTrue(launcher.is_stale_alias(name), name)
        for name in ("PATH", "SystemRoot", "WORK_STACK_HOME", "UNRELATED_INHERITED"):
            self.assertFalse(launcher.is_stale_alias(name), name)

    def test_coverage_control_names_are_recognised(self):
        for name in ("COVERAGE_FILE", "COVERAGE_RCFILE", "coverage_file",
                     "COVERAGE_PROCESS_START", "COVERAGE_DEBUG_FILE"):
            self.assertTrue(launcher.is_coverage_control(name), name)
        for name in ("PATH", "COVERAGE", "MY_COVERAGE_FILE"):
            self.assertFalse(launcher.is_coverage_control(name), name)

    def test_hostile_values_are_replaced_and_unrelated_values_kept(self):
        base = Path(tempfile.mkdtemp(dir=str(FIXTURE_BASE))).resolve()
        self.addCleanup(shutil.rmtree, base, True)
        root = launcher.allocate_result_root(base)
        hostile = HostileEnvironment(base / "foreign").apply({"PATH": "kept"})
        environment = launcher.build_child_environment(hostile, root)
        self.assertEqual(environment["PATH"], "kept")
        self.assertEqual(environment["UNRELATED_INHERITED"], "kept")
        self.assertNotIn("PYTHONUTF8", environment)
        self.assertNotIn("PYTHONIOENCODING", environment)
        self.assertNotIn("STALE_WORK_STACK_RESULT_DIR", environment)
        self.assertNotIn("COVERAGE_FILE", environment)
        self.assertNotIn("COVERAGE_RCFILE", environment)
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        for name in ("WORKSTACK_TEST_RESULTS_ROOT", "WORK_STACK_TEST_RESULT_ROOT",
                     "TEMP", "APPDATA", "WORK_STACK_HOME"):
            self.assertTrue(Path(environment[name]).is_relative_to(root), name)


class CoverageControlContract(unittest.TestCase):
    """Inherited coverage controls cannot move or reconfigure repository coverage."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(dir=str(FIXTURE_BASE))).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_pins_name_the_repository_data_file_and_configuration(self):
        self.assertEqual(launcher.coverage_pins(False), {})
        pins = launcher.coverage_pins(True, repository_root=ROOT)
        self.assertEqual(pins["COVERAGE_FILE"], str(ROOT / ".coverage"))
        self.assertEqual(pins["COVERAGE_RCFILE"], str(ROOT / ".coveragerc"))

    def test_a_missing_repository_configuration_is_not_pinned(self):
        pins = launcher.coverage_pins(True, repository_root=self.directory)
        self.assertEqual(pins, {"COVERAGE_FILE": str(self.directory / ".coverage")})

    def test_inherited_controls_are_replaced_by_the_pins(self):
        root = launcher.allocate_result_root(self.directory)
        pins = launcher.coverage_pins(True, repository_root=ROOT)
        hostile = HostileEnvironment(self.directory / "foreign").apply({})
        environment = launcher.build_child_environment(hostile, root, pins)
        self.assertEqual(environment["COVERAGE_FILE"], str(ROOT / ".coverage"))
        self.assertEqual(environment["COVERAGE_RCFILE"], str(ROOT / ".coveragerc"))
        selected = launcher.prove_containment(environment, root, pins)
        self.assertEqual(selected["COVERAGE_FILE"], str(ROOT / ".coverage"))
        self.assertEqual(selected["COVERAGE_RCFILE"], str(ROOT / ".coveragerc"))

    def test_preflight_refuses_a_coverage_override_that_survived(self):
        root = launcher.allocate_result_root(self.directory)
        environment = launcher.build_child_environment({}, root, {})
        environment["COVERAGE_FILE"] = str(self.directory / "escaped.coverage")
        with self.assertRaises(launcher.ContainmentError) as raised:
            launcher.prove_containment(environment, root, {})
        self.assertIn("coverage control", str(raised.exception))

    def test_hostile_overrides_cannot_move_or_reconfigure_a_real_coverage_run(self):
        fixture = LauncherFixture(self.directory / "repo", PASSING_SUITE)
        foreign = self.directory / "foreign"
        foreign.mkdir(parents=True, exist_ok=True)
        (foreign / "escaped.coveragerc").write_text(FOREIGN_COVERAGERC, encoding="utf-8")
        hostile = HostileEnvironment(foreign).apply(dict(os.environ))
        completed = fixture.run("--coverage", environment=hostile)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        receipt = fixture.receipt(completed)
        self.assertTrue(receipt["coverage"])
        self.assertEqual(receipt["environment"]["COVERAGE_FILE"], str(fixture.root / ".coverage"))
        self.assertEqual(receipt["environment"]["COVERAGE_RCFILE"], str(fixture.root / ".coveragerc"))
        # The escaping destination stayed empty and the data landed in the repository.
        self.assertFalse((foreign / "escaped.coverage").exists())
        self.assertTrue((fixture.root / ".coverage").exists())
        probe = json.loads(
            (Path(receipt["result_root"]) / "fixtures" / "probe.json").read_text(encoding="utf-8"))
        self.assertEqual(probe["coverage"], {
            "COVERAGE_FILE": str(fixture.root / ".coverage"),
            "COVERAGE_RCFILE": str(fixture.root / ".coveragerc"),
        })
        # The repository .coveragerc, not the foreign one, decided the policy:
        # branch coverage on, and the repository's own measured source.
        report = fixture.coverage_json()
        self.assertTrue(report["meta"]["branch_coverage"])
        self.assertGreater(report["totals"]["num_branches"], 0)
        measured = sorted(name.replace("\\", "/") for name in report["files"])
        self.assertTrue(all(name.startswith("tests/") for name in measured), measured)


class RealSuiteContract(unittest.TestCase):
    """One real small suite, started exactly the way CI starts the launcher."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(dir=str(FIXTURE_BASE))).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_hostile_inherited_environment_is_contained_for_the_child(self):
        fixture = LauncherFixture(self.directory, PASSING_SUITE)
        hostile = HostileEnvironment(self.directory / "foreign")
        completed = fixture.run(environment=hostile.apply(dict(os.environ)))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        receipt = fixture.receipt(completed)
        root = Path(receipt["result_root"])
        probe = json.loads((root / "fixtures" / "probe.json").read_text(encoding="utf-8"))
        self.assertTrue(all(probe["contained"].values()), probe["contained"])
        self.assertEqual(probe["root"], str(root))
        self.assertEqual(probe["aliases"], [])
        self.assertEqual(probe["encoding"], {"PYTHONUTF8": None, "PYTHONIOENCODING": None})
        self.assertEqual(probe["coverage"], {"COVERAGE_FILE": None, "COVERAGE_RCFILE": None})
        self.assertEqual(probe["bytecode"], "1")
        self.assertFalse(probe["writes_bytecode"])
        self.assertEqual(probe["unrelated"], "kept")
        # Nothing was written where the hostile environment pointed.
        self.assertFalse((self.directory / "foreign" / "stale-fixtures").exists())
        self.assertFalse((self.directory / "foreign" / "live").exists())
        self.assertFalse((self.directory / "foreign" / "escaped.coverage").exists())

    def test_receipt_records_exit_cwd_selected_environment_and_log_hash(self):
        import hashlib

        fixture = LauncherFixture(self.directory, PASSING_SUITE)
        completed = fixture.run()
        receipt = fixture.receipt(completed)
        self.assertEqual(receipt["exit"], 0)
        self.assertEqual(Path(receipt["cwd"]), fixture.root)
        self.assertEqual(receipt["pattern"], "test_sample_probe.py")
        self.assertFalse(receipt["coverage"])
        log = Path(receipt["log"])
        self.assertTrue(log.is_relative_to(fixture.root / ".artifacts" / "backend-tests"))
        self.assertEqual(hashlib.sha256(log.read_bytes()).hexdigest(), receipt["log_sha256"])
        self.assertIn("test_environment_is_contained", log.read_text(encoding="utf-8", errors="replace"))
        stored = json.loads(Path(receipt["log"]).with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(stored["log_sha256"], receipt["log_sha256"])
        # Only contained, non-secret paths are recorded; no coverage pins here.
        self.assertEqual(
            sorted(stored["environment"]),
            sorted({name for name, _ in launcher.CONTAINED_PATHS} | {"PYTHONDONTWRITEBYTECODE"}),
        )

    def test_two_launches_of_the_same_suite_use_different_roots(self):
        fixture = LauncherFixture(self.directory, PASSING_SUITE)
        first = fixture.receipt(fixture.run())
        second = fixture.receipt(fixture.run())
        self.assertNotEqual(first["result_root"], second["result_root"])
        self.assertNotEqual(first["log"], second["log"])

    def test_failing_suite_propagates_its_native_exit_status(self):
        fixture = LauncherFixture(self.directory, FAILING_SUITE)
        completed = fixture.run()
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        receipt = fixture.receipt(completed)
        self.assertEqual(receipt["exit"], 1)
        self.assertIn("FAILED", Path(receipt["log"]).read_text(encoding="utf-8", errors="replace"))

    def test_coverage_run_produces_data_beside_the_repository(self):
        fixture = LauncherFixture(self.directory, PASSING_SUITE)
        completed = fixture.run("--coverage")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        receipt = fixture.receipt(completed)
        self.assertTrue(receipt["coverage"])
        self.assertEqual(receipt["argv"][2:5], ["-m", "coverage", "run"])
        self.assertTrue((fixture.root / ".coverage").exists())


if __name__ == "__main__":
    unittest.main()

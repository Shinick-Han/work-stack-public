"""Canonical launcher for the backend unittest suite.

CI used to invoke ``coverage run -m unittest discover`` directly, which left the
child process on whatever ``TEMP``, ``APPDATA`` and result-root values the host
happened to export. The backend host test contract requires a *contained*
environment instead: every fixture, temporary file, cache and application-data
path must resolve inside one absolute result root that is never a live Work Stack
directory. This launcher builds that environment, proves containment before the
child starts, runs the same discovery CI ran before, and writes a receipt with
the native exit status.

Refusal happens before allocation. The prospective result-root base is proven
absolute, free of symlink/junction redirection and disjoint from every live Work
Stack location *before* a single directory is created, so a forbidden base is
left byte-for-byte untouched and no child is ever started.

Coverage configuration is the repository's own. Inherited ``COVERAGE_*``
controls are dropped and, when ``--coverage`` is requested, ``COVERAGE_FILE``
and ``COVERAGE_RCFILE`` are pinned to the repository ``.coverage`` and
``.coveragerc``, so branch coverage and its destination cannot be redirected or
reconfigured by the host.

It is deliberately not a shell wrapper: the only things a caller may vary are the
discovery pattern, whether branch coverage is collected, and where the result
root is allocated. Arbitrary command strings are not accepted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIRECTORY = "tests"
DEFAULT_PATTERN = "test_*.py"
RECEIPT_DIRECTORY = Path(".artifacts") / "backend-tests"

# A containment refusal is not a test failure, so it gets its own status and
# never masquerades as one of unittest's exit codes.
REFUSAL_EXIT = 2

# Only these characters may appear in a focused discovery pattern. Separators,
# drive letters and parent references are rejected so the pattern can never
# redirect discovery outside the tests directory.
PATTERN_GRAMMAR = re.compile(r"\A[A-Za-z0-9_.*?\[\]-]+\Z")

# Inherited names that alias a foreign result root, plus the encoding overrides
# that would hide a real Windows default-encoding failure from the suite.
DISCARDED_NAMES = frozenset(
    {
        "RESULTS_ROOT",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONLEGACYWINDOWSSTDIO",
    }
)

# Windows marks junctions as well as symlinks with this attribute. A redirected
# ancestor would make the lexical guard below describe a different directory
# from the one that is really written to, so redirection is refused outright.
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# Coverage reads its data destination and its whole configuration from these
# names, so the repository pins them rather than inheriting them.
COVERAGE_DATA_NAME = ".coverage"
COVERAGE_CONFIG_NAME = ".coveragerc"

# Every contained path the child receives, mapped to its directory under the
# result root. The nine names the host test contract validates are all present.
CONTAINED_PATHS = (
    ("WORKSTACK_TEST_RESULTS_ROOT", ""),
    ("WORKSTACK_TEST_FIXTURE_ROOT", "fixtures"),
    ("WORK_STACK_TEST_RESULT_ROOT", "results"),
    ("TEMP", "tmp"),
    ("TMP", "tmp"),
    ("TMPDIR", "tmp"),
    ("APPDATA", "app"),
    ("LOCALAPPDATA", "local"),
    ("WORK_STACK_HOME", "home"),
    ("WORK_STACK_RUNTIME", "runtime"),
    ("XDG_CACHE_HOME", "cache"),
    ("PYTHONPYCACHEPREFIX", "pyc"),
)

# Live Work Stack locations known by name rather than announced by an inherited
# variable. ``WorkStack/SSOT/main`` is the operator's real single source of
# truth; the POSIX pair are the store's own defaults.
KNOWN_LIVE_RELATIVE = (
    ("WorkStack", "SSOT", "main"),
    (".local", "share", "workstack"),
    (".local", "state", "workstack"),
)


class ContainmentError(RuntimeError):
    """A preflight proof failed, so no child process may be started."""


def is_stale_alias(name: str) -> bool:
    """Report whether an inherited variable would alias a foreign result root."""

    upper = name.upper()
    if "WORK" in upper and ("RESULT" in upper or "FIXTURE" in upper):
        return True
    return upper in DISCARDED_NAMES


def is_coverage_control(name: str) -> bool:
    """Report whether an inherited variable would reconfigure coverage."""

    return name.upper().startswith("COVERAGE_")


def validate_pattern(pattern: str) -> str:
    """Accept a focused discovery pattern, never an arbitrary shell string."""

    if not pattern or not PATTERN_GRAMMAR.match(pattern) or ".." in pattern:
        raise ValueError("discovery pattern must be a bare filename glob: " + pattern)
    return pattern


def paths_overlap(first: Path, second: Path) -> bool:
    """Report whether either path is the other or lies inside it.

    Comparison is per component and case-folded through ``os.path.normcase``, so
    a differently cased spelling of the same Windows directory cannot slip past
    the guard.
    """

    left = tuple(os.path.normcase(part) for part in first.parts)
    right = tuple(os.path.normcase(part) for part in second.parts)
    shared = min(len(left), len(right))
    return shared > 0 and left[:shared] == right[:shared]


def candidate_forms(path: Path) -> list[Path]:
    """Return the lexical and resolved spellings a guard must both reject."""

    lexical = Path(os.path.normpath(str(Path(path).expanduser())))
    forms = [lexical]
    try:
        resolved = lexical.resolve()
    except OSError:
        return forms
    if resolved != lexical:
        forms.append(resolved)
    return forms


def redirected_ancestor(path: Path) -> Path | None:
    """Return the first existing lexical ancestor that is a reparse point.

    The ancestors inspected are those of the path *as written*, read with
    ``lstat`` so a junction or symlink is reported rather than silently
    followed. A component that does not exist yet cannot redirect anything, so
    it is skipped.
    """

    lexical = Path(os.path.normpath(str(Path(path).expanduser())))
    for ancestor in (*reversed(lexical.parents), lexical):
        try:
            status = ancestor.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(status.st_mode):
            return ancestor
        if getattr(status, "st_file_attributes", 0) & REPARSE_POINT:
            return ancestor
    return None


def live_workstack_directories(environment: Mapping[str, str]) -> list[Path]:
    """Return the directories that hold real user data and must stay untouched."""

    candidates: list[Path] = []
    for name in ("WORK_STACK_HOME", "WORK_STACK_RUNTIME"):
        value = environment.get(name)
        if value:
            candidates.append(Path(value))
    for name in ("LOCALAPPDATA", "APPDATA"):
        value = environment.get(name)
        if value:
            candidates.append(Path(value) / "WorkStack")
    home_value = environment.get("USERPROFILE") or environment.get("HOME")
    home = Path(home_value) if home_value else Path.home()
    for relative in KNOWN_LIVE_RELATIVE:
        candidates.append(home.joinpath(*relative))
    live: list[Path] = []
    for candidate in candidates:
        for form in candidate_forms(candidate):
            if form not in live:
                live.append(form)
    return live


def assert_base_is_allowed(base: Path, environment: Mapping[str, str]) -> None:
    """Prove a prospective result-root base before anything at all is created.

    The ordering is the point: an explicit ``--result-root`` is checked for
    absoluteness *before* it is resolved, its original lexical ancestors are
    checked for redirection, and both its lexical and resolved spellings are
    checked against every live Work Stack location. Only after this returns may
    a caller create the base or allocate inside it.
    """

    if not Path(base).expanduser().is_absolute():
        raise ContainmentError("result root base must be absolute: " + str(base))
    redirected = redirected_ancestor(base)
    if redirected is not None:
        raise ContainmentError("result root base is redirected at: " + str(redirected))
    forbidden = live_workstack_directories(environment)
    for form in candidate_forms(base):
        for live in forbidden:
            if paths_overlap(form, live):
                raise ContainmentError(
                    "result root base overlaps a live Work Stack directory: " + str(live)
                )


def default_result_bases() -> tuple[Path, ...]:
    """Return the short absolute base directories to try, in order."""

    if os.name == "nt":
        preferred = Path((os.environ.get("SystemDrive") or "C:") + "\\") / "wsbt"
    else:
        preferred = Path("/tmp") / "wsbt"
    # gettempdir() probes candidates by writing to them. Read configuration only;
    # resolve_result_base must guard the fallback before any write occurs.
    inherited = next((os.environ[key] for key in ("TMPDIR", "TEMP", "TMP")
                      if os.environ.get(key)), None)
    fallback = (Path(inherited) if inherited else Path.home() / ".cache") / "wsbt"
    if fallback == preferred:
        return (preferred,)
    return (preferred, fallback)


def resolve_result_base(explicit: str | None, environment: Mapping[str, str]) -> Path:
    """Choose a proven base; every candidate is guarded before it is created."""

    if explicit is not None:
        base = Path(explicit).expanduser()
        assert_base_is_allowed(base, environment)
        return base
    failure: OSError | None = None
    for candidate in default_result_bases():
        assert_base_is_allowed(candidate, environment)
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            failure = error
            continue
        return candidate
    raise ContainmentError("no usable result root base: " + str(failure))


def allocate_result_root(base: Path) -> Path:
    """Allocate a unique short result root; concurrent launchers never collide.

    The base must already have been proven by :func:`assert_base_is_allowed`.
    This function creates directories, so it can never be the guard itself.
    """

    target = Path(base).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="r", dir=str(target))).resolve()


def coverage_pins(coverage: bool, repository_root: Path = REPOSITORY_ROOT) -> dict[str, str]:
    """Pin coverage's data file and configuration to the repository's own.

    ``.coverage`` beside the repository root and ``.coveragerc`` are exactly
    where cwd-relative discovery put them before, so the CI branch-coverage
    location, the measured sources and the JSON output path are unchanged, but
    an inherited ``COVERAGE_FILE`` or ``COVERAGE_RCFILE`` can no longer move or
    reconfigure them.
    """

    if not coverage:
        return {}
    pins = {"COVERAGE_FILE": str(repository_root / COVERAGE_DATA_NAME)}
    configuration = repository_root / COVERAGE_CONFIG_NAME
    if configuration.is_file():
        pins["COVERAGE_RCFILE"] = str(configuration)
    return pins


def build_child_environment(
    inherited: Mapping[str, str],
    result_root: Path,
    pins: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the contained child environment without creating anything yet."""

    environment = {
        name: value
        for name, value in inherited.items()
        if not is_stale_alias(name) and not is_coverage_control(name)
    }
    for name, relative in CONTAINED_PATHS:
        environment[name] = str(result_root / relative if relative else result_root)
    environment.update(pins or {})
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def prove_containment(
    environment: dict[str, str],
    result_root: Path,
    pins: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Prove, before any child starts, that the contained paths cannot escape.

    Returns the selected non-secret path map that the receipt records.
    """

    root = Path(result_root)
    if not root.is_absolute():
        raise ContainmentError("result root must be absolute: " + str(root))
    root = root.resolve()
    for live in live_workstack_directories(os.environ):
        if paths_overlap(root, live):
            raise ContainmentError("result root overlaps a live Work Stack directory: " + str(live))
    selected: dict[str, str] = {}
    for name, _relative in CONTAINED_PATHS:
        path = Path(environment[name])
        if not path.is_absolute():
            raise ContainmentError("contained path is not absolute: " + name)
        path.mkdir(parents=True, exist_ok=True)
        if not path.resolve().is_relative_to(root):
            raise ContainmentError("contained path escaped the result root: " + name)
        selected[name] = str(path)
    for name in environment:
        if is_stale_alias(name) and name not in selected:
            raise ContainmentError("stale alias survived sanitisation: " + name)
    expected = dict(pins or {})
    surviving = {name: environment[name] for name in environment if is_coverage_control(name)}
    if surviving != expected:
        raise ContainmentError("inherited coverage control survived sanitisation: " + str(surviving))
    selected.update(expected)
    selected["PYTHONDONTWRITEBYTECODE"] = environment["PYTHONDONTWRITEBYTECODE"]
    return selected


def build_child_argv(pattern: str, coverage: bool, verbose: bool) -> list[str]:
    """Build the discovery command line, matching the previous CI invocation."""

    discovery = ["-m", "unittest", "discover", "-s", TESTS_DIRECTORY, "-p", validate_pattern(pattern)]
    if verbose:
        discovery.append("-v")
    if coverage:
        # ``coverage run`` reads the pinned .coveragerc, so branch coverage, the
        # measured sources and the JSON output location are the repository's own
        # configuration, exactly as before.
        return [sys.executable, "-B", "-m", "coverage", "run", *discovery]
    return [sys.executable, "-B", *discovery]


def _tee(argv: list[str], cwd: Path, environment: dict[str, str], log: Path) -> int:
    """Run the child, mirroring its output to the console and the log."""

    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as handle:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        for chunk in iter(lambda: process.stdout.read1(65536), b""):
            handle.write(chunk)
            sys.stdout.buffer.write(chunk)
            sys.stdout.flush()
        process.stdout.close()
        return process.wait()


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help="Focused unittest discovery pattern; defaults to full discovery.",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Collect the repository's configured branch coverage.",
    )
    parser.add_argument(
        "--result-root",
        default=None,
        help="Absolute base directory for the unique contained result root.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Drop unittest verbose output.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    pattern = validate_pattern(arguments.pattern)
    inherited = dict(os.environ)
    # Nothing above this line touches the filesystem, and the guard below runs
    # before the first mkdir, so a refused base is never allocated into.
    base = resolve_result_base(arguments.result_root, inherited)
    result_root = allocate_result_root(base)
    pins = coverage_pins(arguments.coverage)
    environment = build_child_environment(inherited, result_root, pins)
    selected = prove_containment(environment, result_root, pins)

    child_argv = build_child_argv(pattern, arguments.coverage, not arguments.quiet)
    receipt_directory = REPOSITORY_ROOT / RECEIPT_DIRECTORY
    log = receipt_directory / (result_root.name + ".log")
    started = datetime.now(timezone.utc).isoformat()
    exit_code = _tee(child_argv, REPOSITORY_ROOT, environment, log)
    receipt = {
        "started": started,
        "ended": datetime.now(timezone.utc).isoformat(),
        "cwd": str(REPOSITORY_ROOT),
        "argv": child_argv,
        "pattern": pattern,
        "coverage": bool(arguments.coverage),
        "exit": exit_code,
        "result_root": str(result_root),
        "environment": selected,
        "log": str(log),
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
    }
    receipt_path = receipt_directory / (result_root.name + ".json")
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))
    # The child's native status is the launcher's status: a failing suite stays a
    # failing suite, and a crash keeps its own exit code.
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContainmentError as refusal:
        print("refused: " + str(refusal), file=sys.stderr)
        raise SystemExit(REFUSAL_EXIT)

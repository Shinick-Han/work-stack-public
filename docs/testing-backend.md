# Running the backend test suite

The backend suite is started through one launcher, `scripts/run_backend_tests.py`.
CI and local runs use the same entry point so that a suite which passes on a
laptop is the suite CI ran.

## Why a launcher

Several backend contracts — the Windows desktop host contract most strictly —
require the test process to run inside a *contained* environment: the fixture
root, temporary directory, cache, application data and Work Stack home must all
resolve inside a single absolute result root, and none of them may be a live
Work Stack directory. Running `unittest discover` directly leaves those values at
whatever the host exported, which either fails the contract outright or, worse,
writes fixtures into the user's real Work Stack data.

The launcher builds that environment, proves containment *before* the child
starts, and only then runs discovery. The proof runs before the *first* `mkdir`,
so a forbidden result root is refused with the directory left byte-for-byte
untouched and no child process started.

## Usage

```
python -B scripts/run_backend_tests.py [--pattern GLOB] [--coverage] [--result-root DIR] [--quiet]
```

| Option | Meaning |
| --- | --- |
| *(none)* | Full discovery: `unittest discover -s tests -p test_*.py -v`. |
| `--pattern GLOB` | Focused discovery. A bare filename glob such as `test_storage_*.py`; separators, parent references and shell metacharacters are rejected. |
| `--coverage` | Run under `coverage run` with the repository `.coveragerc` pinned (branch coverage, measured sources and JSON output location all stay in that file, whatever the host exported). |
| `--result-root DIR` | Absolute base directory in which the unique result root is allocated, refused if it is relative, redirected, or overlaps live Work Stack data. Defaults to a short path (`%SystemDrive%\wsbt`, `/tmp/wsbt`). |
| `--quiet` | Drop unittest's verbose output. |

The launcher deliberately does not accept an arbitrary command string. If you
need a different command, run it yourself — but then you own the containment.

Examples:

```
python -B scripts/run_backend_tests.py                                  # everything
python -B scripts/run_backend_tests.py --pattern test_storage_v4_*.py   # one focused area
python -B scripts/run_backend_tests.py --coverage                       # what CI runs
```

## The child environment

Every launch allocates a fresh unique directory inside the result-root base, so
concurrent launches never share state. Inside it the launcher creates and exports:

| Variable | Directory |
| --- | --- |
| `WORKSTACK_TEST_RESULTS_ROOT` | the result root itself |
| `WORKSTACK_TEST_FIXTURE_ROOT` | `fixtures` |
| `WORK_STACK_TEST_RESULT_ROOT` | `results` |
| `TEMP`, `TMP`, `TMPDIR` | `tmp` |
| `APPDATA` | `app` |
| `LOCALAPPDATA` | `local` |
| `WORK_STACK_HOME` | `home` |
| `WORK_STACK_RUNTIME` | `runtime` |
| `XDG_CACHE_HOME` | `cache` |
| `PYTHONPYCACHEPREFIX` | `pyc` |

It also drops inherited names that would alias a foreign result root (anything
matching `*WORK*RESULT*` / `*WORK*FIXTURE*`, plus `RESULTS_ROOT`) and the
encoding overrides `PYTHONUTF8`, `PYTHONIOENCODING` and
`PYTHONLEGACYWINDOWSSTDIO`, so the suite is measured against the real Windows
default encoding rather than a CI-only override. Bytecode writing is off
(`PYTHONDONTWRITEBYTECODE=1` and `-B`). Unrelated variables such as `PATH` are
inherited untouched.

Every inherited `COVERAGE_*` name is dropped as well. Coverage configuration
belongs to the repository, not to the host: with `--coverage` the launcher pins
`COVERAGE_FILE` to the repository `.coverage` and `COVERAGE_RCFILE` to the
repository `.coveragerc` — the same two files cwd-relative discovery used
before — so an inherited `COVERAGE_FILE` cannot move the data out of the
repository and an inherited `COVERAGE_RCFILE` cannot turn branch coverage off or
change the measured sources. Preflight refuses to start the child if any
`COVERAGE_*` name other than those pins survived.

## Refusal, before anything is created

The result-root base is proven before a single directory is created:

1. An explicit `--result-root` must be absolute. This is checked on the path as
   written, *before* it is resolved, because resolving a relative path silently
   turns it into an absolute one under the current working directory.
2. Every existing lexical ancestor of the base is read with `lstat` and refused
   if it is a symlink, junction or any other reparse point. A redirected
   ancestor would make the check below describe a different directory from the
   one that is really written to.
3. Both the lexical and the resolved spelling of the base are compared, per
   path component and case-folded, against every live Work Stack location:
   `WORK_STACK_HOME`, `WORK_STACK_RUNTIME`, `%LOCALAPPDATA%\WorkStack`,
   `%APPDATA%\WorkStack`, `~/WorkStack/SSOT/main`, `~/.local/share/workstack`
   and `~/.local/state/workstack`. Equality, nesting in either direction and a
   differently cased spelling of the same directory are all refused.

A refusal exits with status **2** and prints `refused: <reason>` on stderr, so it
can never be mistaken for a test failure. Nothing is allocated, no receipt or log
is written, and no child process is started.

After allocation the same overlap proof runs again on the allocated root, and
preflight additionally refuses if any exported path escapes the root or if a
stale alias survived sanitisation.

## Receipts

Each run writes `.artifacts/backend-tests/<root>.log` (the child's combined
output, also mirrored to the console) and `.artifacts/backend-tests/<root>.json`,
which records the UTC start and end, the working directory, the exact child
argv, the pattern, whether coverage was collected, the native exit status, the
selected non-secret contained paths (including the coverage pins when
`--coverage` was requested), and the SHA-256 of the log. The same JSON is
printed on stdout as the last line.

The launcher exits with the child's own status: a failing suite stays a failing
suite, and no failure is translated or swallowed. The single exception is a
containment refusal, which exits 2 without ever starting a child.

## CI

`.github/workflows/quality-reusable.yml` runs
`python -B scripts/run_backend_tests.py --coverage` and uploads
`.artifacts/backend-tests` as `backend-test-evidence-*` whenever that step
actually ran, so a red run keeps its receipt and log. The separate
`coverage json` and `scripts/check_coverage.py` steps are unchanged: the launcher
runs with the repository root as its working directory, so `.coverage` and
`.artifacts/quality/python-coverage.json` land exactly where they did before.

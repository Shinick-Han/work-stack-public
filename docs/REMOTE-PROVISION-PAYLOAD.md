# Remote provision installer payload

`desktop/python-webview-shell/remote_provision_payload.py` builds the single Python
source program that a remote `python -I -B -` interpreter reads on standard input in
order to run the checked-in installer engine.

This is a bounded T0039 prerequisite. It builds source text and nothing else. It does
not open an SSH connection, launch a process, orchestrate an install, or change what the
installer will admit.

## Why the payload carries its own bytes

`remote_provision_probe.py` already streams `remote_provision_collector.py` to
`python -I -B -`. That works because the collector needs no binary input: the
interpreter consumes standard input until EOF and then runs what it read.

The installer does need binary input — the artifact archive and its sidecar. There is no
second read of the same stream after EOF, so framed binary data cannot simply be appended
after the program text. The shared remote command grammar in
`remote_command_contract.py` deliberately prohibits `-c` and shell quoting, so a
heredoc, an inline `-c` program, or a shell-quoted argument are all out of scope, and
this slice adds no remote staging file and no second network hop.

The decision for this slice is therefore: emit **one complete ASCII Python program** that
contains every required byte as a base64 literal.

## What the generated program contains

Four base64 literals, in a fixed order:

| Literal | Source |
| --- | --- |
| `_LEAF_B64` | verbatim bytes of `remote_provision_installer_linux.py` |
| `_MAIN_B64` | verbatim bytes of `remote_provision_installer.py` |
| `_ARCHIVE_B64` | caller-supplied archive bytes |
| `_SIDECAR_B64` | caller-supplied sidecar bytes |

followed by a fixed bootstrap that:

1. decodes the leaf module and registers it in `sys.modules` under exactly
   `remote_provision_installer_linux` — the identity the admission module's
   `from remote_provision_installer_linux import InstallerError` requires;
2. decodes and executes the admission module as `remote_provision_installer`;
3. calls `installer_main(sys.argv[1:], archive_bytes, sidecar_bytes)` and exits with its
   status.

Both modules are compiled with the filenames `<remote_provision_installer_linux>` and
`<remote_provision_installer>`, so nothing that a traceback could reference maps to a
real path. Any bootstrap failure is converted to the same bounded single-line refusal the
installer itself writes.

Under `-I` the script directory is not on `sys.path`, so the two modules resolve only
through the `sys.modules` registration performed by the bootstrap. No staged file, no
import search, and no product package is involved.

## The trade-off, stated honestly

Base64 inflates transferred bytes by roughly **4/3**. Memory has a separate cost: the
local builder retains caller bytes, encoded strings, literal chunks, the joined source
and its final ASCII bytes during construction. A review measurement on CPython 3.12
Windows with a 64 MiB archive and 16 KiB sidecar produced 89,643,450 source bytes
(85.49 MiB) and observed a **435.0 MiB local peak working set**. This is one measurement,
not a portable memory bound.

The receiving interpreter also needs source parsing/compiler state, encoded literals,
decoded module and artifact bytes, plus installer state. Peak memory depends on the
interpreter version and allocation behavior; artifact size plus its base64 encoding
is not a valid upper bound. Maximum-size remote execution time and peak memory remain
unverified. A separate timing probe had a Windows pipe-cleanup problem and supplies
no evidence of product execution speed.

This is **not** streaming and **not** zero-copy. It buys a single stdin write with no
staging file, no extra hop, and no shell quoting, and it pays for that in transfer size
and local and remote memory. A future slice that needs a larger artifact should replace the
transport rather than raise these caps.

## Bounds

All bounds are module constants; the total is derived, not hand-picked.

| Constant | Value | Meaning |
| --- | --- | --- |
| `MAX_ARCHIVE_BYTES` | 64 MiB | mirrors the installer's `MAX_ARCHIVE` |
| `MAX_SIDECAR_BYTES` | 16 KiB | mirrors the installer's `MAX_SIDECAR` |
| `MAX_MODULE_BYTES` | 128 KiB | per fixed module source read (both are well under this today) |
| `BOOTSTRAP_BUDGET_BYTES` | 8 KiB | fixed code and literal headers |
| `MAX_PAYLOAD_BYTES` | ~85.8 MiB | `BOOTSTRAP_BUDGET_BYTES + 2 x literal(MAX_MODULE_BYTES) + literal(MAX_ARCHIVE_BYTES) + literal(MAX_SIDECAR_BYTES)` |

A test asserts that `MAX_ARCHIVE_BYTES` and `MAX_SIDECAR_BYTES` still equal the installer
engine's own constants, so the two cannot drift apart silently.

## API and admission boundary

```python
build_installer_payload_source(*, archive_bytes: bytes, sidecar_bytes: bytes) -> bytes
```

The builder accepts **bytes only**. It never accepts source code, paths, module names,
templates, or callbacks, so there is no way for a caller to have arbitrary code embedded in
the generated program. The sibling module filenames are fixed constants.

The builder checks **type and size only**. It does not parse the archive, does not verify a
digest, and makes no provenance or authenticity claim about the embedded bytes — a hash
computed over bytes the caller supplied would prove nothing about their origin. Full
artifact admission stays exactly where it is today: `_admit_artifact` inside
`remote_provision_installer.py`.

Import is effect-free. No filesystem, environment, network, process, or `argv` access runs
at import; `_SHELL_DIR` is computed with `os.path.abspath` and performs no I/O. The two
sibling module files are read only when the builder is invoked, and only **after** the
caller-supplied bytes have passed their type and size checks.

## Errors

`PayloadError` carries a stable code and a fixed message. Source text, filesystem paths,
and raw exception detail are never propagated, and no underlying exception is reachable
through `__cause__`.

| Code | Cause |
| --- | --- |
| `INVALID_PAYLOAD_INPUT` | archive or sidecar is not `bytes`, or exceeds its cap |
| `PAYLOAD_SOURCE_UNAVAILABLE` | a fixed sibling module is missing, unreadable, empty, or over `MAX_MODULE_BYTES` |
| `PAYLOAD_TOO_LARGE` | the generated program exceeds `MAX_PAYLOAD_BYTES` |

## Tests

`tests/test_remote_provision_payload.py` (21 tests, `python -m unittest`; pytest is not
installed in this checkout).

The transport tests execute the **actual generated source** via
`[sys.executable, "-I", "-B", "-", ...]` with a bounded timeout, `CREATE_NO_WINDOW`, a
disposable temporary cwd, and a minimal environment. They cover:

- an invalid real artifact reaching the real installer's refusal path and producing one
  bounded JSON line on stderr, empty stdout, exit status 2, no traceback, and no path or
  source leakage;
- a raw-binary / quote / newline / NUL / code-shaped canary in both the archive and the
  sidecar staying inert, with the fixture directory unchanged and no marker file written;
- repeated runs of the same source producing identical status and stderr;
- malformed argv refused as `INVALID_INSTALLER`.

Reaching `REMOTE_ARTIFACT_INVALID` is meaningful evidence: that code is raised by the
admission module using `InstallerError` imported from the leaf, so it can only be produced
if both modules loaded under their required fixed identities and the real admission code
ran. The embedded module bytes are separately asserted to be byte-for-byte equal to the
checked-in files, so no substitute source is standing in for the real installer.

**These tests establish refusal and wiring only.** No test in this file claims, or is
evidence for, a successful Windows or Linux install. Verifying a successful install needs a
genuine artifact on a matching target and is out of scope here: the frozen artifact targets
`cp312` / `cpython-312-x86_64-linux-gnu`, and the WSL Ubuntu available on this machine runs
Python 3.14.4 / `cpython-314-x86_64-linux-gnu` / glibc 2.43, which the installer correctly
refuses as incompatible.

The remaining tests cover effect-free import, refusal of wrong-typed and oversized inputs
*before* any sibling read, sanitized missing / oversized module-source errors, deterministic
output for the same bytes, base64-only literal lines, a deterministic import namespace
(exactly two `sys.modules` registrations, under the two fixed names), and the derived size
bound.

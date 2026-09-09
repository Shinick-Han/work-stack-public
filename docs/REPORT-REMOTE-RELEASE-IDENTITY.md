# Remote installer release identity — repair report

- Date: 2026-09-08
- Worktree: `C:/ws-orca/worktrees/workstack/integration/ns-remote-release-identity`
- Base commit: `2330e75` (`fix(cli): gate the UTF-8 output boundary on Windows`)
- Upstream finding: `REPORT-REMOTE-UPGRADE-PATH.md` §5.3 / R2
- Scope owned here: `desktop/python-webview-shell/remote_provision_installer.py`,
  `desktop/python-webview-shell/remote_provision_installer_linux.py`, and
  `tests/test_remote_provision_installer.py`. Nothing else was touched.

## 1. The defect

The two standalone install-engine modules pinned the release identity to `1.0.8`
in three places while `workstack/__init__.py` had moved to `1.0.13`:

```
remote_provision_installer.py:32   PRODUCT = "1.0.8"
remote_provision_installer.py:35   ARCHIVE_NAME = "WorkStack-Linux-1.0.8-cp312-manylinux_2_17_x86_64.zip"
remote_provision_installer_linux.py:29  PRODUCT = "1.0.8"
```

Both gates compare for exact equality, so a legitimately built current artifact
was rejected twice over: `_identity_fields` / `_parse_sidecar` refused the
sidecar and manifest with `REMOTE_ARTIFACT_INVALID`, and — had it got past that
— `_accept_probe` would have refused the post-install probe with
`REMOTE_SMOKE_FAILED`. The builder
(`scripts/build_linux_remote_artifact.py:684`) names its output
`WorkStack-Linux-{version}-{TARGET_ID}.zip`, so the archive-name check failed on
the same drift.

Nothing was wrong with the checks. The constants they read were stale, and
nothing failed when they went stale.

## 2. How the modules reach the remote host (established first)

`remote_provision_payload.build_installer_payload_source` reads these two files
verbatim from `desktop/python-webview-shell/`, base64-embeds them as literals in
one ASCII program, and the generated prologue `_load`s the leaf under the fixed
name `remote_provision_installer_linux` **before** loading the admission module.
The program then calls `installer_main(argv, archive, sidecar)`.

Two consequences fixed the shape of this repair:

- **No product package is importable on the remote host.** The install engine
  runs before `workstack` exists there; both module docstrings state that
  product packages are not imported, and
  `test_import_isolation_across_split` enforces it at the source level. Reading
  `workstack.__version__` at run time is therefore not available.
- **The leaf is already loaded first and already imported from.** The admission
  module already does `from remote_provision_installer_linux import
  InstallerError`, and the existing contract test asserts
  `MODULE.InstallerError is LINUX.InstallerError`. Importing more names across
  that same seam needs no payload-builder change and adds no module to the
  payload.

`remote_provision_payload.py` was consequently **not modified** — the remote
execution is self-contained without it.

## 3. What changed

### 3.1 One definition site inside the payload

`remote_provision_installer_linux.py` is now the sole definition of the release
identity, and it states the current release:

```python
# Sole release identity; imported by the admission module, test-pinned to workstack/__init__.py.
PRODUCT = "1.0.13"
PROTOCOL = 1
```

`remote_provision_installer.py` imports that pair instead of restating it, and
derives the expected archive name from it rather than carrying a third literal:

```python
from remote_provision_installer_linux import PRODUCT
from remote_provision_installer_linux import PROTOCOL
...
TARGET_ID = "cp312-manylinux_2_17_x86_64"
ARCHIVE_NAME = "WorkStack-Linux-%s-%s.zip" % (PRODUCT, TARGET_ID)
```

Three hand-maintained literals across two modules became **one**. The artifact
gate and the smoke gate can no longer disagree with each other, because they
read the same object.

### 3.2 What deliberately did **not** change

- Every equality check is intact. `_same_value` still requires identical type
  and value; `_identity_fields` still checks schema, product, and protocol on
  **both** the sidecar and the manifest; `_parse_sidecar` still checks the
  archive name, size, and SHA-256; `_accept_probe` still requires the probe line
  to be canonical and to report the exact expected uid, product, and protocol.
- No version is inferred from the artifact. The expected target remains a
  trusted constant compiled into the engine; the artifact is still compared
  against it, never trusted to declare it.
- Install-root refusal (`INSTALL_ROOT_EXISTS`), the `renameat2(RENAME_NOREPLACE)`
  atomic commit, the euid/owner rules, the data-root admission, and the offline
  no-network posture are untouched.
- `workstack/__init__.py`, `workstack_desktop.py`, connection
  activation/lifetime code, `scripts/build_linux_remote_artifact.py`, and
  `scripts/release_gate.py` were not modified (other ownership).

### 3.3 Why one source, and not injection at payload assembly

The preferred shape was a single source of release identity established at
payload assembly. Injecting it there — having the generated program set
`module.PRODUCT` after loading — was rejected on purpose: it would make the
admission constant caller-supplied, so a defect or tamper in payload generation
could relax the very gate that exists to refuse a wrong artifact.
`remote_provision_payload.py` documents that it makes no provenance claim and
that "admission of the archive stays entirely with the installer engine"; moving
the expected identity out of the engine would contradict that.

Collapsing the three constants onto the leaf achieves the same
one-definition-site property **inside** the trusted, checked-in engine, with no
new payload surface. The residual duplication — between the leaf's constant and
`workstack/__init__.py` — is the part that cannot be removed without importing a
package that does not exist on the remote host, so it is covered by enforced
tests instead (§4) and by the follow-up in §6.

## 4. Tests

All in `tests/test_remote_provision_installer.py`.

### 4.1 Fixture migration (not a wholesale golden rewrite)

The happy-path fixtures previously hard-coded `"1.0.8"` in 24 places. They now
read the identity the shipped engine declares:

```python
PRODUCT = MODULE.PRODUCT
PROTOCOL = MODULE.PROTOCOL
ARCHIVE_NAME = MODULE.ARCHIVE_NAME
```

The `_load` helper and the `LINUX`/`MODULE` loads moved above the fixture
constants so this is possible; no test body was reordered. Every rewritten
literal was a **positive** fixture whose job is to be a valid artifact while
some other dimension is made invalid — so binding it to the engine's declared
identity preserves each test's intent exactly, and makes a future release bump
move the fixtures with the engine instead of silently leaving them describing a
superseded release.

`1.0.8` did not disappear from the suite. It is retained as
`ReleaseIdentityTests.SUPERSEDED`, the concrete neighbouring release that every
identity gate must still refuse (§4.3). No negative fixture lost a dimension.

The WSL/Linux primitive script (`WSL_PRIMITIVE_SCRIPT`, skipped on Windows
without `WORKSTACK_TEST_WSL_DISTRO`) now fills its `PRODUCT`/`PROTOCOL` from the
engine it loads, for the same reason.

`make_artifact` gained `manifest_product`, `manifest_protocol`,
`sidecar_product`, `sidecar_protocol`, and `archive_name` keyword overrides, all
defaulting to the current identity. Every existing call site is unchanged. The
overrides let a negative case differ in exactly one identity field while every
hash, size, and canonical form stays correct, so a refusal isolates the identity
gate rather than incidental corruption.

### 4.2 Drift enforcement (the gap that let this happen)

`ReleaseIdentityTests` now fails when the constants drift:

- `test_engine_identity_equals_the_source_release` — `LINUX.PRODUCT` equals
  `workstack.__version__` and `LINUX.PROTOCOL` equals
  `workstack.REMOTE_PROTOCOL_VERSION`.
- `test_admission_module_reads_the_leaf_identity` — `MODULE.PRODUCT is
  LINUX.PRODUCT`, and the admission source contains no `PRODUCT = ` /
  `PROTOCOL = ` assignment, so the single definition site cannot be
  reintroduced as a duplicate.
- `test_expected_archive_name_is_derived_from_that_identity`.
- `test_staged_package_import_gate_uses_the_same_identity` — the `smoke_imports`
  equality (which needs a real dirfd-anchored stage, Linux only) is asserted at
  the source, so it cannot quietly become a presence check.

### 4.3 Accepted / refused

- `test_current_release_artifact_is_admitted_and_installs` — the current
  release's artifact is admitted by `_admit_artifact` and installs through the
  existing synthetic `_install_with_operations` path; the success document and
  the install receipt both report `workstack.__version__`.
- `test_sidecar_stating_the_superseded_release_is_refused` — `1.0.8` in both
  documents → `REMOTE_ARTIFACT_INVALID`.
- `test_manifest_stating_another_release_is_refused` — sidecar correct, its
  manifest digest computed over these very bytes, only the manifest's
  `product_version` differing → `REMOTE_ARTIFACT_INVALID`.
- `test_protocol_mismatch_is_refused_on_both_documents` — `PROTOCOL + 1` on both
  documents and on the manifest alone → `REMOTE_ARTIFACT_INVALID`.
- `test_archive_named_for_another_release_is_refused` —
  `WorkStack-Linux-1.0.8-…zip` → `REMOTE_ARTIFACT_INVALID`.
- `test_post_install_probe_must_report_the_same_release` — the current identity
  is accepted by `_accept_probe`; the superseded product and a wrong protocol
  each → `REMOTE_SMOKE_FAILED`.

## 5. Evidence

Canonical backend launcher, unique result roots, no live SSH, no company system,
no live SSOT, no installer execution against a real host, no dependency install.

| Run | Pattern | Result |
|---|---|---|
| `--result-root .../wsri1` | `test_remote_provision_*.py` | `Ran 192 tests … OK (skipped=2)` |
| `--result-root .../wsri2` | `test_linux_*.py` | `Ran 31 tests … OK` |

The `test_linux_*` run is the load-bearing one for acceptance:
`test_linux_artifact_installer_composition.py` runs the **real builder** over a
temporary git source and wheelhouse at `INSTALLER.PRODUCT`, asserts
`INSTALLER.ARCHIVE_NAME == archive.name`, and feeds the produced bytes verbatim
into the **real** `_admit_artifact`. It passes at `1.0.13`, i.e. the artifact the
current source produces is now admitted end-to-end. That test already derived its
version from `INSTALLER.PRODUCT` before this change; it was passing only because
it was building a `1.0.8` artifact to match the stale engine.

**Negative control.** Reverting the leaf constant to `1.0.8` and re-running the
focused suite fails immediately, naming the drift:

```
FAIL: test_engine_identity_equals_the_source_release
AssertionError: '1.0.13' != '1.0.8'
FAIL: test_current_release_artifact_is_admitted_and_installs
AssertionError: '1.0.13' != '1.0.8'
FAIL: test_expected_archive_name_is_derived_from_that_identity
FAIL: test_archive_named_for_another_release_is_refused
```

The constant was restored immediately afterwards; the committed tree carries
`1.0.13`.

Two skips in the installer suite are the pre-existing Linux/WSL opt-in gates
(`sys.platform.startswith("linux") or WORKSTACK_TEST_WSL_DISTRO`), unchanged by
this work.

Not run here: the repository quality gate and the frontend suite — outside the
focused-tests instruction for this packet.

## 6. Follow-up (not done here, deliberately)

1. **The last remaining duplication.** `PRODUCT` in the leaf must still be
   hand-edited to match `workstack/__init__.py` at each release. The test in
   §4.2 makes that omission a loud failure rather than a silent one, but it does
   not remove the edit. Two ways to close it, both outside this packet's
   ownership: (a) have `scripts/build_linux_remote_artifact.py` verify the
   engine's constant against the version it is stamping and refuse on mismatch,
   which is the same class of guard as its existing `SOURCE_UNPINNED` /
   `LOCK_BYPASS` refusals; or (b) add the two constants to the release checklist
   alongside `workstack/__init__.py`.
2. **`REPORT-REMOTE-UPGRADE-PATH.md` R1, R3–R7 remain open.** In particular R1
   (proof that `frontend/dist` is reproducible from the artifact's
   `source_commit`) is still unaddressed, and per §4 of that report the working
   tree's `dist` is likely older than HEAD's `frontend/src`. **Building and
   shipping an artifact on the strength of this repair alone would produce a
   remote whose Python is `1.0.13` and whose UI may not be** — a second cause
   indistinguishable from the original symptom. This packet unblocks the version
   gate; it does not make the artifact safe to ship.
3. **R3 (the install driver) is still absent.** No product code calls
   `build_ssh_provision_install_command`. The engine can now accept a current
   artifact, but nothing in the repository drives an SSH install.
